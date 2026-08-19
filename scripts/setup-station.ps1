#Requires -Version 5.1
#
# Set up one Windows station for cite-cli, in one shot:
#
#   1. set the CITE_ALERT_* variables machine-wide
#   2. install uv and create .cite\logs for this account
#   3. register the two scheduled tasks (clean, renew) - both as THIS
#      account, both running whether or not anyone is signed in
#
# There is only one account involved: whichever admin account you run this
# from. Both tasks run headless, so neither needs an interactive session.
# `cite sync` (which drives the GUI-only NIS-Elements License Manager) is NOT
# scheduled here: sign in to this station and run it by hand whenever a
# submitted renewal needs to be applied.
#
# Run ONCE per station, from an ELEVATED PowerShell ("Administrator: Windows
# PowerShell" in the title bar), logged in as the account that should own
# both tasks:
#
#     powershell -ExecutionPolicy Bypass -File .\setup-station.ps1
#
# It asks for two passwords, each typed twice: this account's, and the Gmail
# App Password. The account password is checked against Windows as soon as
# it is typed, so a mistake only costs re-typing that prompt - it is needed
# every run because Windows can verify a guess but never return the real
# password, and "run whether logged on or not" tasks must have it even for
# the account you are already using. The Gmail App Password is different: it
# is just the machine env var this script set last time, so it is only asked
# again if it is not set yet, or if you choose to replace it. Re-running is
# safe - tasks are replaced in place.

# --- edit only if a station differs ---------------------------------------- #
$Email = 'citeathms@gmail.com'    # renewal form, and alert sender + recipient
$FullName = 'Federico Gasparoli'  # renewal form
$CleanDays = 25                   # cite clean -d
$RepoUrl = 'git+https://github.com/CITE-HMS/cite-cli'
# --------------------------------------------------------------------------- #

$ErrorActionPreference = 'Stop'
$AdminAccount = $env:USERNAME
$Problems = 0

function Phase { param($m) Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok { param($m) Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "  [!!] $m" -ForegroundColor Yellow }
function Problem { param($m) Write-Host "  [!!] $m" -ForegroundColor Red; $script:Problems++ }
function Note { param($m) Write-Host "       $m" -ForegroundColor Yellow }

function Get-Plain {
    param([securestring]$s)
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

function Read-PasswordTwice {
    # Typed twice: a typo is otherwise only found much later, when the batch
    # task it belongs to fails silently overnight. -StripSpaces ignores
    # spacing differences between the two entries - Gmail shows its app
    # password in groups of four, and how it was typed or pasted should not
    # cause a false mismatch.
    param($prompt, [switch]$StripSpaces)
    for ($i = 1; $i -le 3; $i++) {
        $first = Read-Host -AsSecureString $prompt
        $again = Read-Host -AsSecureString "$prompt - type it again"
        $a = Get-Plain $first
        $b = Get-Plain $again
        if ($StripSpaces) { $a = $a -replace '\s', ''; $b = $b -replace '\s', '' }
        if ($a -ceq $b) {
            if ($StripSpaces) { return (ConvertTo-SecureString -String $a -AsPlainText -Force) }
            return $first
        }
        Warn 'the two entries do not match, try again'
    }
    throw 'Password confirmation failed three times.'
}

function Test-AccountPassword {
    param($account, $plainPassword)
    Add-Type -AssemblyName System.DirectoryServices.AccountManagement
    $ctx = New-Object DirectoryServices.AccountManagement.PrincipalContext(
        [DirectoryServices.AccountManagement.ContextType]::Machine, $env:COMPUTERNAME)
    try { $ctx.ValidateCredentials($account, $plainPassword) }
    finally { $ctx.Dispose() }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "This window is not elevated. Right-click PowerShell -> 'Run as administrator'."
}

Write-Host "cite-cli station setup on $env:COMPUTERNAME" -ForegroundColor White
Write-Host "  clean and renew both run as: $AdminAccount"

# The account password is checked as soon as it is typed, so a mistake only
# costs re-typing that one prompt, never the Gmail one after it.
$AdminPassword = $null
for ($attempt = 1; $attempt -le 3; $attempt++) {
    $pw = Read-PasswordTwice "Password for '$AdminAccount' (both tasks need it)"
    if (Test-AccountPassword $AdminAccount (Get-Plain $pw)) { $AdminPassword = $pw; break }
    Warn "that is not $AdminAccount's current Windows password - try again"
}
if (-not $AdminPassword) { throw "Could not verify $AdminAccount's password after 3 attempts." }

$SmtpPrompt = 'Gmail App Password (16 chars, not the account password, e.g. xxxx xxxx xxxx xxxx)'
# Unlike the account password above, this one is not re-verified against
# anything external - it is just the machine env var this script set last
# time, so (unlike it) it CAN be read back and reused instead of re-typed.
$existingSmtpPassword = [Environment]::GetEnvironmentVariable('CITE_ALERT_SMTP_PASSWORD', 'Machine')
if ($existingSmtpPassword) {
    $keep = Read-Host "Gmail App Password is already set for $Email - keep it? (Y/n)"
    if ($keep -match '^[Nn]') {
        $SmtpPassword = Read-PasswordTwice -StripSpaces $SmtpPrompt
    }
    else {
        $SmtpPassword = ConvertTo-SecureString -String $existingSmtpPassword -AsPlainText -Force
        Ok 'keeping the existing Gmail App Password'
    }
}
else {
    $SmtpPassword = Read-PasswordTwice -StripSpaces $SmtpPrompt
}

# --- 1. email alert variables ------------------------------------------------#
Phase '1/3  email alert variables'

# Machine scope means it survives regardless of which account ends up running
# these tasks - this is `setx /M`. Google shows the app password in groups of
# four; the spaces are only for reading. Strip them, so it does not matter how
# it was typed or pasted.
foreach ($v in @{ CITE_ALERT_SMTP_USER = $Email
        CITE_ALERT_SMTP_PASSWORD       = ((Get-Plain $SmtpPassword) -replace '\s', '')
        CITE_ALERT_TO                  = $Email
    }.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($v.Key, $v.Value, 'Machine')
    Set-Item -Path "Env:$($v.Key)" -Value $v.Value
    Ok "$($v.Key) set machine-wide"
}

# --- 2. uv + log folder ------------------------------------------------------#
Phase '2/3  uv and log folder'

if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    Warn 'git not found - `uv tool run --from git+...` needs it: https://git-scm.com/downloads'
}

$adminUv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (-not (Test-Path $adminUv)) {
    $onPath = Get-Command uv.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { $adminUv = $onPath.Source }
    else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
    }
}
if (Test-Path $adminUv) { Ok "uv for $AdminAccount : $adminUv" }
else { Problem "uv not installed for $AdminAccount" }
New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE '.cite\logs') | Out-Null

# --- 3. the two scheduled tasks ----------------------------------------------#
Phase '3/3  scheduled tasks'

$adminUser = "$env:USERDOMAIN\$AdminAccount"
$cmdExe = "$env:SystemRoot\System32\cmd.exe"
$midnight = [datetime]::Today

function New-CiteAction {
    # Skip everything while NIS-Elements is open: `findstr` exits 0 when it
    # finds nis_ar.exe, and `||` then runs nothing. The redirect catches
    # failures that happen before Python starts, so the internal log is empty.
    param($uv, $citeArgs)
    New-ScheduledTaskAction -Execute $cmdExe -Argument (
        '/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || ' +
        "`"$uv`" tool run --refresh --from $RepoUrl cite $citeArgs" +
        ' > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"')
}

function Add-CiteTask {
    # -Force replaces a task of the same name without asking. Task names are
    # unique per folder, so there is no "keep both" - and replacing is what
    # makes re-running this script the way to repair a half-built station.
    param($name, $action, $trigger, $settings)
    $existed = [bool](Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
    try {
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
            -Settings $settings -User $adminUser -Password (Get-Plain $AdminPassword) -RunLevel Highest -Force | Out-Null
        Ok "task '$name' $(if ($existed) { '(replaced the existing one)' } else { 'created' })"
    }
    catch {
        Problem "task '$name': $($_.Exception.Message)"
    }
}

# Both tasks run "whether user is logged on or not" (a batch logon), which
# Administrators hold by default - $AdminAccount is guaranteed to be one,
# since running this at all required an elevated prompt.
Add-CiteTask -name 'cite-cli clean' `
    -action (New-CiteAction $adminUv "clean -d $CleanDays -f") `
    -trigger (New-ScheduledTaskTrigger -Daily -At $midnight) `
    -settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4))

# renew: fully headless (no --sync). It submits and detects renewals on its
# own; applying a pending one still needs `cite sync` run by hand, signed in
# to this same account (renew and sync share %USERPROFILE%\.cite\renew_state.json).
Add-CiteTask -name 'cite-cli renew' `
    -action (New-CiteAction $adminUv "renew --email $Email --full-name `"$FullName`" --url nikon") `
    -trigger (New-ScheduledTaskTrigger -Daily -At $midnight.AddMinutes(75) -RandomDelay (New-TimeSpan -Hours 1)) `
    -settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1))

# Registering replaces the task at that path only, and task names are unique
# per folder - so an older copy in a subfolder, or one whose name differs by a
# stray space, keeps running alongside the two above. This also catches
# leftovers from the old cite-automation setup (`cite-cli sync`,
# `cite-cli lock-on-logon`) on a station that has not run cleanup-station.ps1.
$ours = 'cite-cli clean', 'cite-cli renew'
$strays = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
        $_.TaskName -like '*cite*' -and -not ($_.TaskPath -eq '\' -and $ours -contains $_.TaskName)
    })
foreach ($s in $strays) {
    Warn "leftover task '$($s.TaskPath)$($s.TaskName)' will run alongside ours"
    Note "remove it with:"
    Note "  Unregister-ScheduledTask -TaskName '$($s.TaskName)' -TaskPath '$($s.TaskPath)' -Confirm:`$false"
}

# --- done ------------------------------------------------------------------ #
Write-Host @"

Left to do by hand:
  1. Make sure the Public user has a folder named NIS_Elements containing
     licmgr_s.exe - cite sync needs it when you run it by hand below.
  2. Run each task once in Task Scheduler (renew first, clean after) and
     check the logs under C:\Users\$AdminAccount\.cite\logs
  3. Check an alert email arrives:
     uvx --from "$RepoUrl" cite test-alert

When Nikon replies to a submitted renewal, sign in to $AdminAccount on this
station and run:
     uvx --from "$RepoUrl" cite sync
cite renew already sends an alert if a pending submission goes 4+ days
without a recorded sync attempt, so a forgotten station does not go quiet.
"@

# No `exit` here on purpose: it would close the window when this script is run
# straight from the web with `irm ... | iex`.
if ($Problems) {
    Write-Host "Finished with $Problems problem(s) - see the red lines above.`n" -ForegroundColor Red
}
else {
    Write-Host "Station setup complete.`n" -ForegroundColor Green
}
