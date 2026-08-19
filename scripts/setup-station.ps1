#Requires -Version 5.1
#
# Set up one Windows station for cite-cli, in one shot:
#
#   1. create the cite-automation account (+ "Log on as a batch job")
#   2. set the CITE_ALERT_* variables machine-wide
#   3. install uv and create .cite\logs for BOTH accounts
#   4. register the two scheduled tasks: clean, renew
#
# Both tasks run headless, whether or not anyone is logged on to the station -
# neither needs an interactive Windows session. `cite sync` (which drives the
# GUI-only NIS-Elements License Manager) is NOT scheduled here: run it by hand,
# logged on to the station, whenever a submitted renewal needs to be applied.
#
# Run ONCE per station, from an ELEVATED PowerShell ("Administrator: Windows
# PowerShell" in the title bar), logged in as the admin account that should own
# the `cite clean` task:
#
#     powershell -ExecutionPolicy Bypass -File .\setup-station.ps1
#
# It asks for up to three passwords, each typed twice: cite-automation's, this
# admin account's, and the Gmail App Password. The first two are checked
# against Windows as soon as they are typed, so a mistake only costs
# re-typing that one prompt - and they are required every run, account
# creation or not, because Windows can verify a guess but never return the
# real password, and the script needs the real one for the batch-logon tasks.
# The Gmail App Password is different: it is just the machine env var this
# script set last time, so it is only asked again if it is not set yet, or if
# you choose to replace it. Re-running is safe - the account is reused and
# tasks are replaced.

# --- edit only if a station differs ---------------------------------------- #
$AutomationAccount = 'cite-automation'
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
    # Typed twice: a typo in either account password is otherwise only found
    # much later, when the batch task it belongs to fails silently overnight.
    # -StripSpaces ignores spacing differences between the two entries - Gmail
    # shows its app password in groups of four, and how it was typed or
    # pasted should not cause a false mismatch.
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

function Get-ProfilePath {
    param($name)
    try {
        $sid = (New-Object Security.Principal.NTAccount($name)).Translate(
            [Security.Principal.SecurityIdentifier]).Value
        $key = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid"
        return (Get-ItemProperty $key -Name ProfileImagePath).ProfileImagePath
    }
    catch { return "$env:SystemDrive\Users\$name" }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "This window is not elevated. Right-click PowerShell -> 'Run as administrator'."
}

Write-Host "cite-cli station setup on $env:COMPUTERNAME" -ForegroundColor White
Write-Host "  clean runs as : $AdminAccount"
Write-Host "  renew runs as : $AutomationAccount"

# Each password is checked as soon as it is typed, not after all three are in -
# so a mistake on one only costs re-typing that one, never the ones after it
# (in particular, never the 16-char Gmail app password).
$automationExisted = [bool](Get-LocalUser -Name $AutomationAccount -ErrorAction SilentlyContinue)
$AutomationPassword = $null
for ($attempt = 1; $attempt -le 3; $attempt++) {
    $pw = Read-PasswordTwice "Password for '$AutomationAccount' (the standard CITE one)"
    if (-not $automationExisted -or (Test-AccountPassword $AutomationAccount (Get-Plain $pw))) {
        $AutomationPassword = $pw
        break
    }
    Warn "that is not $AutomationAccount's current Windows password - it already exists with a different one"
    Note "(to reset it instead: Set-LocalUser -Name $AutomationAccount -Password (Read-Host -AsSecureString))"
}
if (-not $AutomationPassword) { throw "Could not verify $AutomationAccount's password after 3 attempts." }

$AdminPassword = $null
for ($attempt = 1; $attempt -le 3; $attempt++) {
    $pw = Read-PasswordTwice "Password for '$AdminAccount' (the clean task needs it)"
    if (Test-AccountPassword $AdminAccount (Get-Plain $pw)) { $AdminPassword = $pw; break }
    Warn "that is not $AdminAccount's current Windows password - try again"
}
if (-not $AdminPassword) { throw "Could not verify $AdminAccount's password after 3 attempts." }

$SmtpPrompt = 'Gmail App Password (16 chars, not the account password, e.g. xxxx xxxx xxxx xxxx)'
# Unlike the two account passwords above, this one is not re-verified against
# anything external - it is just the machine env var this script set last
# time, so (unlike them) it CAN be read back and reused instead of re-typed.
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

# --- 1. the cite-automation account ---------------------------------------- #
Phase '1/4  cite-automation account'

if ($automationExisted) {
    Ok "account already exists"
}
else {
    New-LocalUser -Name $AutomationAccount -Password $AutomationPassword -PasswordNeverExpires `
        -AccountNeverExpires -FullName 'CITE automation' | Out-Null
    Ok "account created"
}
# PasswordNeverExpires matters: an expired password silently kills the batch
# logon, and renew goes quiet until someone notices.
Set-LocalUser -Name $AutomationAccount -PasswordNeverExpires $true
try { Add-LocalGroupMember -Group 'Users' -Member $AutomationAccount -ErrorAction Stop } catch {}

# "Log on as a batch job": the renew task runs whether logged on or not, which
# Windows starts as a batch logon. Only Administrators hold that right by default.
$sid = (New-Object Security.Principal.NTAccount($AutomationAccount)).Translate(
    [Security.Principal.SecurityIdentifier]).Value
$inf = "$env:TEMP\cite-rights.inf"
$new = "$env:TEMP\cite-rights-new.inf"
$db = "$env:TEMP\cite-rights.sdb"
& secedit.exe /export /areas USER_RIGHTS /cfg $inf | Out-Null
$lines = Get-Content $inf
$batch = $lines | Where-Object { $_ -match '^\s*SeBatchLogonRight\s*=' } | Select-Object -First 1
if ($batch -and $batch -match [regex]::Escape($sid)) {
    Ok "'Log on as a batch job' already granted"
}
else {
    if ($batch) { $lines = $lines | ForEach-Object { if ($_ -eq $batch) { "$($_.TrimEnd()),*$sid" } else { $_ } } }
    else { $lines = $lines | ForEach-Object { $_; if ($_ -match '^\s*\[Privilege Rights\]') { "SeBatchLogonRight = *$sid" } } }
    Set-Content -Path $new -Value $lines -Encoding Unicode
    & secedit.exe /configure /db $db /cfg $new /areas USER_RIGHTS | Out-Null
    Ok "'Log on as a batch job' granted"
}
Remove-Item $inf, $new, $db -Force -ErrorAction SilentlyContinue

# --- 2. email alert variables ---------------------------------------------- #
Phase '2/4  email alert variables'

# Machine scope is the whole point (this is `setx /M`): clean runs as one
# account and renew as the other. A per-user value leaves one of them unable
# to email anything, and it fails silently.
# Google shows the app password in groups of four; the spaces are only for
# reading. Strip them, so it does not matter how it was typed or pasted.
foreach ($v in @{ CITE_ALERT_SMTP_USER = $Email
        CITE_ALERT_SMTP_PASSWORD       = ((Get-Plain $SmtpPassword) -replace '\s', '')
        CITE_ALERT_TO                  = $Email
    }.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($v.Key, $v.Value, 'Machine')
    Set-Item -Path "Env:$($v.Key)" -Value $v.Value
    Ok "$($v.Key) set machine-wide"
}

# --- 3. uv + log folders, for both accounts -------------------------------- #
Phase '3/4  uv and log folders'

$automationUser = "$env:COMPUTERNAME\$AutomationAccount"

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

# uv installs per profile, so the second account needs its own copy - and its
# own log folder. Running this as that user also creates its profile, which
# saves logging into it by hand. Never point a task at the other account's
# uv.exe: besides not being readable across profiles, it would let whoever
# controls the cite-automation account run code as the admin one.
$bootstrap = @'
$ErrorActionPreference = 'Stop'
$cite = Join-Path $env:USERPROFILE '.cite'
New-Item -ItemType Directory -Force -Path (Join-Path $cite 'logs') | Out-Null
$result = @{ ok = $false; uv = ''; git = ''; error = '' }
try {
    # uv shells out to git.exe to clone the repo. This account has its own PATH,
    # so a git installed "for me only" under the admin account is invisible here.
    $g = Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($g) { $result.git = $g.Source }
    $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (-not (Test-Path $uv)) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    }
    $result.uv = $uv
    $result.ok = (Test-Path $uv)
}
catch { $result.error = $_.Exception.Message }
finally { $result | ConvertTo-Json | Set-Content (Join-Path $cite 'setup-result.json') -Encoding UTF8 }
'@
$bootstrapPath = "$env:ProgramData\cite-cli\setup-user-bootstrap.ps1"
New-Item -ItemType Directory -Force -Path (Split-Path $bootstrapPath) | Out-Null
Set-Content -Path $bootstrapPath -Value $bootstrap -Encoding UTF8

$automationUv = Join-Path (Get-ProfilePath $AutomationAccount) '.local\bin\uv.exe'
# Drop any result from an earlier run, so a failure here cannot read as success.
Remove-Item (Join-Path (Get-ProfilePath $AutomationAccount) '.cite\setup-result.json') `
    -Force -ErrorAction SilentlyContinue

# Run the bootstrap as a one-shot scheduled task, not `Start-Process -Credential`.
# The latter does an interactive-style logon (CreateProcessWithLogonW) that can
# fail silently for reasons unrelated to the account being fine - Secondary
# Logon service state, UAC/token quirks, local policy - with no error to catch.
# A scheduled task uses the batch logon right granted to $AutomationAccount in
# step 1/4, the same mechanism the real 'cite-cli renew' task relies on below,
# and it is what reliably creates the profile on a first run.
$bootTaskName = 'cite-cli-bootstrap-temp'
Unregister-ScheduledTask -TaskName $bootTaskName -Confirm:$false -ErrorAction SilentlyContinue
$bootAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $bootstrapPath + '"')
$bootSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
# -Force: a run interrupted before reaching the cleanup Unregister-ScheduledTask
# below (further down this phase) leaves this exact task registered, and the
# plain Unregister above can silently fail to clear it - notably while an
# instance from that earlier run is still executing. Without -Force that
# leftover makes this Register fail with "Cannot create a file when that file
# already exists" instead of just replacing it, the same reason Add-CiteTask
# uses -Force for the two real tasks below.
Register-ScheduledTask -TaskName $bootTaskName -Action $bootAction -Settings $bootSettings `
    -User $automationUser -Password (Get-Plain $AutomationPassword) -RunLevel Highest -Force | Out-Null

Write-Host "  running as $AutomationAccount (first run also builds its profile, ~1 min)"
Start-ScheduledTask -TaskName $bootTaskName
$deadline = (Get-Date).AddMinutes(3)
do {
    Start-Sleep -Seconds 2
    # Right after Start-ScheduledTask, Get-ScheduledTask can throw "the system
    # cannot find the file specified" for a beat - a known CIM provider timing
    # glitch, not the task actually vanishing (nothing removes it before the
    # explicit Unregister-ScheduledTask below). Treat a failed read as still
    # running and keep polling, rather than letting a transient hiccup abort
    # the whole script under $ErrorActionPreference = 'Stop'.
    $task = Get-ScheduledTask -TaskName $bootTaskName -ErrorAction SilentlyContinue
    $state = if ($task) { $task.State } else { 'Running' }
} while ($state -eq 'Running' -and (Get-Date) -lt $deadline)
# Grab the exit code before the task is gone - it is the only clue left once
# it is unregistered, and it is what actually distinguishes "wrong password"
# from every other way this can fail.
$lastResult = (Get-ScheduledTaskInfo -TaskName $bootTaskName -ErrorAction SilentlyContinue).LastTaskResult
Unregister-ScheduledTask -TaskName $bootTaskName -Confirm:$false -ErrorAction SilentlyContinue
if ($state -eq 'Running') { Warn "$bootTaskName did not finish within 3 minutes - continuing anyway" }

$resultFile = Join-Path (Get-ProfilePath $AutomationAccount) '.cite\setup-result.json'
if (Test-Path $resultFile) {
    $result = Get-Content $resultFile -Raw | ConvertFrom-Json
    if ($result.ok) { $automationUv = $result.uv; Ok "uv for $AutomationAccount : $automationUv" }
    else { Problem "uv install failed for $AutomationAccount : $($result.error)" }
    if ($result.git) {
        Ok "git for $AutomationAccount : $($result.git)"
    }
    else {
        Problem "git is not on $AutomationAccount's PATH - the tasks cannot clone the repo"
        Warn 'install Git for Windows "for all users", or add C:\Program Files\Git\cmd to the MACHINE Path'
    }
}
else {
    Problem "could not run as $AutomationAccount - the bootstrap task did not produce a result"
    if ($null -ne $lastResult -and $lastResult -ne 0) {
        Note ('Task Scheduler last result: 0x{0:X8}' -f $lastResult)
        if ($lastResult -eq 0x8007052E) {
            Note "that code means logon failure (bad username/password) - unexpected here, since"
            Note 'the password was already checked against Windows above. Reset it and retry:'
            Note "  Set-LocalUser -Name $AutomationAccount -Password (Read-Host -AsSecureString)"
        }
    }
    Note "look up that code, or watch Task Scheduler's History tab live next time - the"
    Note "temporary task ('$bootTaskName') is removed automatically as soon as it finishes."
    Note 'Re-running this script is safe.'
}

# --- 4. the two scheduled tasks ---------------------------------------------#
Phase '4/4  scheduled tasks'

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
    param($name, $action, $trigger, $settings, $user, $password)
    $existed = [bool](Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
    try {
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
            -Settings $settings -User $user -Password $password -RunLevel Highest -Force | Out-Null
        Ok "task '$name' $(if ($existed) { '(replaced the existing one)' } else { 'created' })"
    }
    catch {
        Problem "task '$name': $($_.Exception.Message)"
    }
}

# clean: stays on the admin account, which has the rights to delete other
# users' acquisition data. Runs at midnight sharp, ahead of renew.
Add-CiteTask -name 'cite-cli clean' -user $adminUser -password (Get-Plain $AdminPassword) `
    -action (New-CiteAction $adminUv "clean -d $CleanDays -f") `
    -trigger (New-ScheduledTaskTrigger -Daily -At $midnight) `
    -settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4))

# renew: fully headless (no --sync), so it stores the password and needs the
# batch logon right. It submits and detects renewals on its own; applying a
# pending one still needs `cite sync` run by hand in an interactive session.
Add-CiteTask -name 'cite-cli renew' -user $automationUser -password (Get-Plain $AutomationPassword) `
    -action (New-CiteAction $automationUv "renew --email $Email --full-name `"$FullName`" --url nikon") `
    -trigger (New-ScheduledTaskTrigger -Daily -At $midnight.AddMinutes(75) -RandomDelay (New-TimeSpan -Hours 1)) `
    -settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1))

# Registering replaces the task at that path only, and task names are unique
# per folder - so an older copy in a subfolder, or one whose name differs by a
# stray space, keeps running alongside the two above. This also catches
# leftovers from the old auto-login setup (`cite-cli sync`,
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
  2. In the $AdminAccount account, run each task once in Task Scheduler (renew
     first, clean after) and check the logs under
     C:\Users\$AutomationAccount\.cite\logs and C:\Users\$AdminAccount\.cite\logs
  3. Check an alert email arrives under BOTH accounts:
     uvx --from "$RepoUrl" cite test-alert

When Nikon replies to a submitted renewal, log on to $AutomationAccount on this
station (a normal interactive sign-in - nothing to pause or arm) and run:
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
