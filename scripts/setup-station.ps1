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
# It asks for up to three passwords, each typed twice: cite-automation's, this
# admin account's, and the Gmail App Password. The first two are checked
# against Windows as soon as they are typed, so a mistake only costs
# re-typing that one prompt - and they are required every run, account
# creation or not, because Windows can verify a guess but never return the
# real password, and the script needs the real one for the LSA secret and the
# batch-logon tasks. The Gmail App Password is different: it is just the
# machine env var this script set last time, so it is only asked again if it
# is not set yet, or if you choose to replace it. Re-running is safe -
# accounts are reused, tasks replaced. Only the reboot test is left to do by
# hand afterwards.

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
    # Primary lock. Explorer runs the Run key as part of shell startup, so it
    # cannot fire before the session is ready, and it cannot miss an event the
    # way Task Scheduler's "at log on" trigger does when the service is still
    # coming up during an auto-login - the failure three staggered triggers
    # could never fix, because all three sat inside that same early window.
    # Never `New-Item -Force` this key. On the registry provider that recreates
    # an existing key and drops the values inside it - and Run always exists,
    # often holding other software's startup entries. Only create it if absent.
    $run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if (-not (Test-Path $run)) { New-Item -Path $run -Force | Out-Null }
    $watchdog = "$env:ProgramData\cite-cli\cite-lock-watchdog.ps1"
    Set-ItemProperty $run -Name CiteLock -Value (
        'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass ' +
        "-File `"$watchdog`"")
    $result.uv = $uv
    $result.ok = (Test-Path $uv)
}
catch { $result.error = $_.Exception.Message }
finally { $result | ConvertTo-Json | Set-Content (Join-Path $cite 'setup-result.json') -Encoding UTF8 }
'@
$bootstrapPath = "$env:ProgramData\cite-cli\setup-user-bootstrap.ps1"
New-Item -ItemType Directory -Force -Path (Split-Path $bootstrapPath) | Out-Null
Set-Content -Path $bootstrapPath -Value $bootstrap -Encoding UTF8

# The lock itself is the same LockWorkStation call the old rundll32 action made.
# What this adds is everything around it: rundll32 discards the return value, so
# a refused lock was silent and unrecoverable. This checks it, keeps retrying
# until it takes, re-locks if the desktop is ever found open again, and leaves a
# log - nobody is meant to be using this desktop, so re-locking is always right.
$lockWatchdog = @'
$ErrorActionPreference = 'SilentlyContinue'
Add-Type @"
using System.Runtime.InteropServices;
public static class CiteLock {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool LockWorkStation();
}
"@

$logDir = Join-Path $env:USERPROFILE '.cite\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'lock.log'
# Announce startup unconditionally. The lock-on-logon task can win the race and
# lock first, leaving this watchdog with nothing to do and nothing to log - and
# an empty log would then read as "it never started", which is the opposite
# diagnosis. One line at launch keeps the two cases apart.
"$(Get-Date -f s) watchdog started (pid $PID)" | Add-Content $log

# Maintenance escape hatch. Signing in as cite-automation with this running
# would re-lock the desktop every two seconds, so from the admin account, before
# switching over:
#     New-Item C:\ProgramData\cite-cli\lock-paused
# The pause expires two hours after the file was last written - a forgotten file
# can never leave the station sitting open. Touch it again to extend it, or
# delete it to resume locking at once. LastWriteTime, not CreationTime: NTFS
# file tunneling can hand a recreated file the original creation stamp, which
# would make a fresh pause look expired the moment it was made.
$pausePath = Join-Path $env:ProgramData 'cite-cli\lock-paused'
$wasPaused = $false

$fails = 0
while ($true) {
    $pause = Get-Item $pausePath -ErrorAction SilentlyContinue
    if ($pause -and $pause.LastWriteTime -gt (Get-Date).AddHours(-2)) {
        if (-not $wasPaused) {
            "$(Get-Date -f s) paused for maintenance" | Add-Content $log
            $wasPaused = $true
        }
        Start-Sleep -Seconds 5
        continue
    }
    if ($wasPaused) {
        "$(Get-Date -f s) pause ended, locking resumed" | Add-Content $log
        $wasPaused = $false
    }

    # LogonUI.exe exists exactly while the lock screen is up. Its absence means
    # this desktop is sitting open, whatever the lock task believes it did.
    if (Get-Process LogonUI -ErrorAction SilentlyContinue) {
        $fails = 0
        Start-Sleep -Seconds 5
        continue
    }
    if ([CiteLock]::LockWorkStation()) {
        "$(Get-Date -f s) locked" | Add-Content $log
        $fails = 0
        # Give LogonUI a moment to appear, so one lock is not logged twice.
        Start-Sleep -Seconds 5
    }
    else {
        # First failure, then every 30th: a permanently refused lock must not
        # fill the disk at one line every two seconds.
        if ($fails % 30 -eq 0) {
            "$(Get-Date -f s) LockWorkStation returned false (attempt $($fails + 1))" |
                Add-Content $log
        }
        $fails++
        Start-Sleep -Seconds 2
    }
}
'@
$lockWatchdogPath = "$env:ProgramData\cite-cli\cite-lock-watchdog.ps1"
Set-Content -Path $lockWatchdogPath -Value $lockWatchdog -Encoding UTF8

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
# -Force: a run interrupted before reaching the cleanup Unregister-ScheduledTask
# below (further down this phase) leaves this exact task registered, and the
# plain Unregister above can silently fail to clear it - notably while an
# instance from that earlier run is still executing. Without -Force that
# leftover makes this Register fail with "Cannot create a file when that file
# already exists" instead of just replacing it, the same reason Add-CiteTask
# uses -Force for the four real tasks below.
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

# lock-on-logon: five staggered triggers, because locking too early can
# silently do nothing. Locking an already-locked session is a harmless no-op.
# The later ones only help the "session was not ready yet" case: a trigger delay
# is counted from when Task Scheduler observes the logon, so if the service was
# still starting and missed the event, no delay recovers it. That gap is what
# the watchdog covers - this task is only the independent second launcher.
Add-CiteTask -name 'cite-cli lock-on-logon' `
    -action (New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\rundll32.exe" -Argument 'user32.dll,LockWorkStation') `
    -trigger @(
    (New-LogonTrigger $automationUser 'PT5S'),
    (New-LogonTrigger $automationUser 'PT10S'),
    (New-LogonTrigger $automationUser 'PT15S'),
    (New-LogonTrigger $automationUser 'PT30S'),
    (New-LogonTrigger $automationUser 'PT45S')) `
    -settings (New-ScheduledTaskSettingsSet -MultipleInstances Parallel -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 1)) `
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

# --- desktop lock ---------------------------------------------------------- #
Phase 'desktop lock'

Ok "lock watchdog written to $lockWatchdogPath"
Note "it starts from a Run key in $AutomationAccount's profile at every logon,"
Note 'retries until the lock takes, and re-locks the desktop if it is ever'
Note "found open. Its record is $($env:SystemDrive)\Users\$AutomationAccount\.cite\logs\lock.log"

# A watchdog already running in a live session keeps executing the copy of the
# script it started with. Re-running this cannot reach into that session to
# restart it, so say so rather than let a stale one look like a failed update.
$running = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*cite-lock-watchdog*' })
if ($running) {
    Note "$($running.Count) watchdog(s) already running - each keeps the script it"
    Note 'started with until the next logon. Nothing to do; a reboot picks this up.'
}

# Warn, never delete: the pause may belong to whoever is mid-maintenance right
# now, and silently re-arming the lock under them would be worse than a stale
# file - which expires on its own in two hours regardless.
$pauseFile = "$env:ProgramData\cite-cli\lock-paused"
if (Test-Path $pauseFile) {
    $expires = (Get-Item $pauseFile).LastWriteTime.AddHours(2)
    if ($expires -gt (Get-Date)) {
        Warn "locking is PAUSED until $expires"
        Note "delete $pauseFile to re-arm it now"
    }
    else { Ok "an expired pause file is present and is being ignored" }
}
else { Ok 'locking is armed (no pause file)' }

# --- done ------------------------------------------------------------------ #
Write-Host @"

Left to do by hand:
  1. Make sure the Public user has a folder named NIS_Elements containing
     licmgr_s.exe.
  2. In the $AdminAccount account, run each task once in Task Scheduler (renew
     first, sync after) and check the logs under
     C:\Users\$AutomationAccount\.cite\logs and C:\Users\$AdminAccount\.cite\logs
  3. Check an alert email arrives under BOTH accounts:
     uvx --from "$RepoUrl" cite test-alert
  4. Reboot and do not touch it. The station must sign in on its own and land on
     the lock screen within ~15 seconds, and
     C:\Users\$AutomationAccount\.cite\logs\lock.log must gain a locked line.
     That log is the check that matters - if the lock ever fails, it says
     whether nothing ran at all or LockWorkStation itself was refused.

To work inside the $AutomationAccount desktop later, pause the lock FIRST, from
this account:
     New-Item $env:ProgramData\cite-cli\lock-paused
then switch user. Leave with Switch user or Win+L - never Sign out, which
destroys the session cite sync needs. Then:
     Remove-Item $env:ProgramData\cite-cli\lock-paused
The pause expires by itself after 2 hours if you forget.
"@

# No `exit` here on purpose: it would close the window when this script is run
# straight from the web with `irm ... | iex`.
if ($Problems) {
    Write-Host "Finished with $Problems problem(s) - see the red lines above.`n" -ForegroundColor Red
}
else {
    Write-Host "Station setup complete.`n" -ForegroundColor Green
}
