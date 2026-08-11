#Requires -Version 5.1
#
# Set up one Windows station for cite-cli, in one shot:
#
#   1. create the cite-automation account (+ "Log on as a batch job")
#   2. set the CITE_ALERT_* variables machine-wide
#   3. install uv and create .cite\logs for BOTH accounts
#   4. auto-login as cite-automation, and lock the session right after
#   5. register the four scheduled tasks: clean, sync, renew, lock-on-logon
#
# It also warns, without changing anything, when fast user switching is off, or
# when the third-party PPMS-RT-Client task is not scoped to the User account -
# neither of those may interfere with the auto-login session.
#
# Run ONCE per station, from an ELEVATED PowerShell ("Administrator: Windows
# PowerShell" in the title bar), logged in as the admin account that should own
# the `cite clean` task:
#
#     powershell -ExecutionPolicy Bypass -File .\setup-station.ps1
#
# It asks for three passwords, each typed twice: cite-automation's, this admin
# account's, and the Gmail App Password. The first two are checked against
# Windows as soon as they are typed, so a mistake only costs re-typing that one
# prompt. Re-running is safe - accounts are reused, tasks replaced. Only the
# reboot test is left to do by hand afterwards.

# --- edit only if a station differs ---------------------------------------- #
$AutomationAccount = 'cite-automation'
$Email = 'citeathms@gmail.com'    # renewal form, and alert sender + recipient
$FullName = 'Federico Gasparoli'  # renewal form
$CleanDays = 25                   # cite clean -d
$RepoUrl = 'git+https://github.com/CITE-HMS/cite-cli'
$PpmsTask = 'PPMS-RT-Client'      # third-party task, only checked, never touched
$PpmsUser = 'User'                # the only account it may start for
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
    # much later, and a wrong auto-login password fails silently at reboot.
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
Write-Host "  clean runs as      : $AdminAccount"
Write-Host "  sync/renew run as  : $AutomationAccount"

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

$SmtpPassword = Read-PasswordTwice -StripSpaces 'Gmail App Password (16 chars, not the account password, e.g. xxxx xxxx xxxx xxxx)'

# --- 1. the cite-automation account ---------------------------------------- #
Phase '1/5  cite-automation account'

if ($automationExisted) {
    Ok "account already exists"
}
else {
    New-LocalUser -Name $AutomationAccount -Password $AutomationPassword -PasswordNeverExpires `
        -AccountNeverExpires -FullName 'CITE automation' | Out-Null
    Ok "account created"
}
# PasswordNeverExpires matters: an expired password silently kills auto-login,
# and the sync leg goes quiet until someone notices.
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
Phase '2/5  email alert variables'

# Machine scope is the whole point (this is `setx /M`): clean runs as one
# account and sync/renew as the other. A per-user value leaves one of them
# unable to email anything, and it fails silently.
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
Phase '3/5  uv and log folders'

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
# controls the auto-login account run code as the admin one.
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
    # Screen-saver backstop: the only lock layer Windows enforces continuously.
    $desk = 'HKCU:\Control Panel\Desktop'
    Set-ItemProperty $desk -Name ScreenSaveActive -Value '1'
    Set-ItemProperty $desk -Name ScreenSaverIsSecure -Value '1'
    Set-ItemProperty $desk -Name ScreenSaveTimeOut -Value '60'
    Set-ItemProperty $desk -Name 'SCRNSAVE.EXE' -Value "$env:SystemRoot\System32\scrnsave.scr"
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
# step 1/5, the same mechanism the real 'cite-cli renew' task relies on below,
# and it is what reliably creates the profile on a first run.
$bootTaskName = 'cite-cli-bootstrap-temp'
Unregister-ScheduledTask -TaskName $bootTaskName -Confirm:$false -ErrorAction SilentlyContinue
$bootAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $bootstrapPath + '"')
$bootSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $bootTaskName -Action $bootAction -Settings $bootSettings `
    -User $automationUser -Password (Get-Plain $AutomationPassword) -RunLevel Highest | Out-Null

Write-Host "  running as $AutomationAccount (first run also builds its profile, ~1 min)"
Start-ScheduledTask -TaskName $bootTaskName
$deadline = (Get-Date).AddMinutes(3)
do {
    Start-Sleep -Seconds 2
    $state = (Get-ScheduledTask -TaskName $bootTaskName).State
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

# --- 4. auto-login --------------------------------------------------------- #
Phase '4/5  auto-login'

# Stores the password the way netplwiz and Sysinternals Autologon do: as an LSA
# private secret, never as a plaintext registry value.
if (-not ('CiteLsa' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CiteLsa
{
    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_OBJECT_ATTRIBUTES
    {
        public int Length; public IntPtr RootDirectory; public IntPtr ObjectName;
        public uint Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService;
    }
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint LsaOpenPolicy(IntPtr SystemName, ref LSA_OBJECT_ATTRIBUTES ObjectAttributes,
                                             uint DesiredAccess, out IntPtr PolicyHandle);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint LsaStorePrivateData(IntPtr PolicyHandle, ref LSA_UNICODE_STRING KeyName,
                                                   ref LSA_UNICODE_STRING PrivateData);
    [DllImport("advapi32.dll")] private static extern uint LsaClose(IntPtr ObjectHandle);
    [DllImport("advapi32.dll")] private static extern int LsaNtStatusToWinError(uint Status);

    private static LSA_UNICODE_STRING Str(string s)
    {
        LSA_UNICODE_STRING u = new LSA_UNICODE_STRING();
        u.Buffer = Marshal.StringToHGlobalUni(s);
        u.Length = (ushort)(s.Length * 2);
        u.MaximumLength = (ushort)(u.Length + 2);
        return u;
    }

    public static void Store(string key, string data)
    {
        LSA_OBJECT_ATTRIBUTES attrs = new LSA_OBJECT_ATTRIBUTES();
        attrs.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
        IntPtr policy;
        uint st = LsaOpenPolicy(IntPtr.Zero, ref attrs, 0x00000024, out policy);
        if (st != 0) throw new Exception("LsaOpenPolicy failed, win32 error " + LsaNtStatusToWinError(st));
        LSA_UNICODE_STRING k = Str(key);
        LSA_UNICODE_STRING d = Str(data);
        try
        {
            st = LsaStorePrivateData(policy, ref k, ref d);
            if (st != 0) throw new Exception("LsaStorePrivateData failed, win32 error " + LsaNtStatusToWinError(st));
        }
        finally { Marshal.FreeHGlobal(k.Buffer); Marshal.ZeroFreeGlobalAllocUnicode(d.Buffer); LsaClose(policy); }
    }
}
'@
}
[CiteLsa]::Store('DefaultPassword', (Get-Plain $AutomationPassword))

$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $winlogon -Name AutoAdminLogon -Value '1' -Type String
Set-ItemProperty $winlogon -Name DefaultUserName -Value $AutomationAccount -Type String
Set-ItemProperty $winlogon -Name DefaultDomainName -Value $env:COMPUTERNAME -Type String
# Never leave a plaintext password next to the LSA secret.
Remove-ItemProperty $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
Remove-ItemProperty $winlogon -Name AutoLogonCount -ErrorAction SilentlyContinue
# Unhide the netplwiz checkbox, so this can be inspected or undone by hand.
$passwordless = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
if (-not (Test-Path $passwordless)) { New-Item -Path $passwordless -Force | Out-Null }
Set-ItemProperty $passwordless -Name DevicePasswordLessBuildVersion -Value 0 -Type DWord
Ok "auto-login enabled (password kept as an LSA secret)"

# Fast user switching has to stay available: with it off, signing in as another
# user logs $AutomationAccount OFF instead of leaving its session parked, and
# `cite sync` then has no interactive session until the next reboot. Reported
# only - on a domain-joined station a GPO would undo a local change anyway.
# Both values mean "enabled" when absent, so only an explicit setting is wrong.
$sysPolicy = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
$hide = (Get-ItemProperty $sysPolicy -Name HideFastUserSwitching -ErrorAction SilentlyContinue).HideFastUserSwitching
$multiSession = (Get-ItemProperty $winlogon -Name AllowMultipleTSSessions -ErrorAction SilentlyContinue).AllowMultipleTSSessions
$fusOk = $true
if ($hide) {
    Warn "fast user switching is hidden (HideFastUserSwitching = $hide)"
    Note "signing in as another user would log $AutomationAccount off, leaving cite sync"
    Note 'without a session until the next reboot. To enable it:'
    Note '  1. Press Win + R, type gpedit.msc, press Enter'
    Note '  2. Go to Computer Configuration > Administrative Templates > System > Logon'
    Note '  3. Double-click "Hide entry points for Fast User Switching"'
    Note '  4. Set it to Not Configured, click OK'
    Note '  5. Restart the PC'
    Note '  on Windows Home there is no gpedit.msc: set HideFastUserSwitching to 0'
    Note "  under $sysPolicy instead"
    $fusOk = $false
}
if ($multiSession -eq 0) {
    Warn 'fast user switching is off (AllowMultipleTSSessions = 0)'
    Note 'To enable it:'
    Note '  1. Press Win + R, type regedit, press Enter'
    Note '  2. Go to HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Note '  3. Double-click AllowMultipleTSSessions and set Value data to 1'
    Note '  4. Restart the PC'
    Note '  or, from this elevated PowerShell:'
    Note "     Set-ItemProperty '$winlogon' -Name AllowMultipleTSSessions -Value 1"
    $fusOk = $false
}
if ($fusOk) { Ok 'fast user switching available' }

# --- 5. the four scheduled tasks ------------------------------------------- #
Phase '5/5  scheduled tasks'

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

function New-LogonTrigger {
    param($user, $delay)
    $t = New-ScheduledTaskTrigger -AtLogOn -User $user
    $t.Delay = $delay
    $t
}

function Add-CiteTask {
    # -Force replaces a task of the same name without asking. Task names are
    # unique per folder, so there is no "keep both" - and replacing is what
    # makes re-running this script the way to repair a half-built station.
    param($name, $action, $trigger, $settings, $user, $password, $principal)
    $existed = [bool](Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
    try {
        if ($principal) {
            Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
                -Settings $settings -Principal $principal -Force | Out-Null
        }
        else {
            Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
                -Settings $settings -User $user -Password $password -RunLevel Highest -Force | Out-Null
        }
        Ok "task '$name' $(if ($existed) { '(replaced the existing one)' } else { 'created' })"
    }
    catch {
        Problem "task '$name': $($_.Exception.Message)"
    }
}

# lock-on-logon: three staggered triggers, because locking too early can
# silently do nothing. Locking an already-locked session is a harmless no-op.
Add-CiteTask -name 'cite-cli lock-on-logon' `
    -action (New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\rundll32.exe" -Argument 'user32.dll,LockWorkStation') `
    -trigger @(
    (New-LogonTrigger $automationUser 'PT5S'),
    (New-LogonTrigger $automationUser 'PT10S'),
    (New-LogonTrigger $automationUser 'PT15S')) `
    -settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries) `
    -principal (New-ScheduledTaskPrincipal -UserId $automationUser -LogonType Interactive -RunLevel Limited)

# clean: stays on the admin account, which has the rights to delete other
# users' acquisition data. Runs at midnight sharp, ahead of sync/renew.
Add-CiteTask -name 'cite-cli clean' -user $adminUser -password (Get-Plain $AdminPassword) `
    -action (New-CiteAction $adminUv "clean -d $CleanDays -f") `
    -trigger (New-ScheduledTaskTrigger -Daily -At $midnight) `
    -settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4))

# sync: needs a logged-on session for the GUI-only License Manager. 1:00 AM
# sharp - no random delay, it has to stay ahead of renew - plus a catch-up
# trigger 2 minutes after logon.
Add-CiteTask -name 'cite-cli sync' `
    -action (New-CiteAction $automationUv 'sync') `
    -trigger @(
    (New-ScheduledTaskTrigger -Daily -At $midnight.AddHours(1)),
    (New-LogonTrigger $automationUser 'PT2M')) `
    -settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)) `
    -principal (New-ScheduledTaskPrincipal -UserId $automationUser -LogonType Interactive -RunLevel Highest)

# renew: headless, so it stores the password and needs the batch logon right.
# 1:15-2:15 AM, after sync.
Add-CiteTask -name 'cite-cli renew' -user $automationUser -password (Get-Plain $AutomationPassword) `
    -action (New-CiteAction $automationUv "renew --email $Email --full-name `"$FullName`" --url nikon") `
    -trigger (New-ScheduledTaskTrigger -Daily -At $midnight.AddMinutes(75) -RandomDelay (New-TimeSpan -Hours 1)) `
    -settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1))

# Registering replaces the task at that path only, and task names are unique
# per folder - so an older copy in a subfolder, or one whose name differs by a
# stray space, keeps running alongside the four above.
$ours = 'cite-cli clean', 'cite-cli sync', 'cite-cli renew', 'cite-cli lock-on-logon'
$strays = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
        $_.TaskName -like '*cite*' -and -not ($_.TaskPath -eq '\' -and $ours -contains $_.TaskName)
    })
foreach ($s in $strays) {
    Warn "leftover task '$($s.TaskPath)$($s.TaskName)' will run alongside ours"
    Note "remove it with:"
    Note "  Unregister-ScheduledTask -TaskName '$($s.TaskName)' -TaskPath '$($s.TaskPath)' -Confirm:`$false"
}

# --- PPMS-RT-Client -------------------------------------------------------- #
Phase 'PPMS-RT-Client'

# Not one of our tasks, but it must never run in the auto-login session: it is
# meant to start at logon of the $PpmsUser account only. A logon trigger left on
# "any user" would launch it as $AutomationAccount too, once per repetition.
# Scan every folder, not just the root: `Get-ScheduledTask -TaskName x` misses a
# task registered under a subfolder. Fall back to a loose match so a task that is
# merely named differently is still found and reported.
$allTasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue)
$found = @($allTasks | Where-Object { $_.TaskName -eq $PpmsTask })
if (-not $found) {
    $found = @($allTasks | Where-Object { $_.TaskName -like '*PPMS*' })
    if ($found) {
        Warn "no task named exactly '$PpmsTask', but found: $(($found | ForEach-Object { $_.TaskPath + $_.TaskName }) -join ', ')"
    }
}

if (-not $found) {
    Ok "no '$PpmsTask' task on this station - nothing to check"
}
foreach ($ppms in $found) {
    $name = "$($ppms.TaskPath)$($ppms.TaskName)"
    $logon = @($ppms.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
    $other = @($ppms.Triggers | Where-Object { $_.CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger' })

    if (-not $logon) { Warn "'$name' has no logon trigger" }
    foreach ($t in $logon) {
        if (-not $t.UserId) {
            Warn "'$name' triggers at logon of ANY user - it will also start in the $AutomationAccount session"
        }
        elseif (($t.UserId -split '\\')[-1] -ne $PpmsUser) {
            Warn "'$name' triggers at logon of '$($t.UserId)', not '$PpmsUser'"
        }
        else { Ok "'$name' triggers at logon of '$($t.UserId)'" }
    }
    if ($other) {
        Warn "'$name' has $($other.Count) trigger(s) that are not 'at log on' - it should only start at logon of '$PpmsUser'"
    }

    # A group principal (typically BUILTIN\Users) is fine: the logon trigger is
    # what decides who it actually runs as.
    $runsAs = if ($ppms.Principal.UserId) { $ppms.Principal.UserId } else { $ppms.Principal.GroupId }
    if ($ppms.Principal.UserId -and ($ppms.Principal.UserId -split '\\')[-1] -ne $PpmsUser) {
        Warn "'$name' runs as '$runsAs', not '$PpmsUser'"
    }
    else { Ok "'$name' runs as '$runsAs'" }
}

# --- done ------------------------------------------------------------------ #
Write-Host @"

Left to do by hand:
  1. Make sure that in the Public user there is a folder named `NIS_Elements` that contains the `licmgr_s.exe` file.
  1. Log in to the `CITE Automation` user and lock it (Win + L or switch user).
  2. In the `AdminAccount` account, run each task once in Task Scheduler (renew first, sync after) and check the log files in `C:\Users\cite-automation\.cite\logs` and `C:\Users\<AdminAccount>\.cite\logs` to ensure they completed successfully.
  3. Check an alert email arrives under BOTH accounts:
     uvx --from "$RepoUrl" cite test-alert
  4. Reboot and do not touch it: the station must sign in on its own and land
     on the lock screen within ~15 seconds.
  2. Check an alert email arrives under BOTH accounts:
     uvx --from "$RepoUrl" cite test-alert
  3. Reboot and do not touch it: the station must sign in on its own and land
     on the lock screen within ~15 seconds.
"@

# No `exit` here on purpose: it would close the window when this script is run
# straight from the web with `irm ... | iex`.
if ($Problems) {
    Write-Host "Finished with $Problems problem(s) - see the red lines above.`n" -ForegroundColor Red
}
else {
    Write-Host "Station setup complete.`n" -ForegroundColor Green
}
