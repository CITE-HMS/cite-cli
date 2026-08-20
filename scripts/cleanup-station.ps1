#Requires -Version 5.1
#
# Undo the OLD cite-automation-based setup (auto-login, the lock watchdog, and
# the account itself) on a station that already had it applied, before
# re-running the new setup-station.ps1 - which no longer uses a separate
# account at all: `cite clean` and `cite renew` both run as your admin
# account, whether or not anyone is signed in. After Nikon replies with the
# updated license, run `cite sync` to apply it.
#
# Removes:
#   1. any active cite-automation session (it was likely auto-logged-in and
#      has been signed in since the last boot) - needed so its profile files
#      aren't still locked by the time steps 4 and 5 touch them
#   2. the 'cite-cli sync' and 'cite-cli lock-on-logon' scheduled tasks (and
#      any leftover 'cite-cli-bootstrap-temp'), and stops a running watchdog
#   3. auto-login for cite-automation (Winlogon keys + the LSA secret)
#   4. the lock watchdog: its Run key and screen-saver settings inside
#      cite-automation's profile, and its script files under ProgramData
#   5. the cite-automation account itself, including its profile folder
#
# Leaves alone: the CITE_ALERT_* machine-wide env vars and the 'cite-cli
# clean' task. The old 'cite-cli renew' task (still registered to run as
# cite-automation) is left in place too - setup-station.ps1 replaces it
# in-place to run as your admin account instead on its next run, so there is
# nothing to remove here.
#
# Run ONCE per station that has the old setup, from an ELEVATED PowerShell,
# straight from GitHub:
#
#     irm https://raw.githubusercontent.com/CITE-HMS/cite-cli/main/scripts/cleanup-station.ps1 | iex
#
# or locally:
#
#     powershell -ExecutionPolicy Bypass -File .\cleanup-station.ps1
#
# Safe to re-run: every step tolerates the thing it removes already being gone.

$AutomationAccount = 'cite-automation'
$ErrorActionPreference = 'Stop'
$Problems = 0

function Phase { param($m) Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok { param($m) Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "  [!!] $m" -ForegroundColor Yellow }
function Note { param($m) Write-Host "       $m" -ForegroundColor Yellow }

function Invoke-Native {
    # Runs a native command with stderr merged into its output, inside an
    # isolated scope with $ErrorActionPreference relaxed just for this call.
    # ANY native command that writes non-empty stderr otherwise becomes a
    # terminating exception under this script's global 'Stop' preference -
    # regardless of 2>&1 vs 2>$null, and regardless of whether the command
    # is actually failing (quser saying "no session" is not an error; it
    # still crashed the script the same way takeown hitting a locked-down
    # folder did). $LASTEXITCODE is set as usual by the native command and
    # stays readable by the caller after this returns.
    param([Parameter(Mandatory)][string]$Exe, [string[]]$ExeArgs = @())
    & { $ErrorActionPreference = 'SilentlyContinue'; & $Exe @ExeArgs 2>&1 }
}

function Invoke-NativeTimed {
    # Same purpose as Invoke-Native, but bounded. takeown /R (and icacls /T)
    # walking a real Windows profile can hit legacy backward-compat junctions
    # - "AppData\Local\Application Data" or "Local Settings" pointing back
    # into "AppData\Local" - and loop through the same tree indefinitely
    # instead of failing, a documented takeown behavior. Kills the process
    # and reports a timeout rather than blocking the whole script forever.
    # Returns $true/$false for whether it finished; $LASTEXITCODE is only
    # meaningful when it did.
    param([Parameter(Mandatory)][string]$Exe, [string[]]$ExeArgs = @(), [int]$TimeoutSeconds = 90)
    $proc = Start-Process -FilePath $Exe -ArgumentList $ExeArgs -PassThru -WindowStyle Hidden
    if ($proc.WaitForExit($TimeoutSeconds * 1000)) {
        $global:LASTEXITCODE = $proc.ExitCode
        return $true
    }
    try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch {}
    return $false
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

Write-Host "Removing the old auto-login/lock/$AutomationAccount setup on $env:COMPUTERNAME" -ForegroundColor White
Write-Host "  Leaves 'cite-cli clean' and the CITE_ALERT_* variables alone."
Write-Host "  Run setup-station.ps1 afterwards, signed in as whichever admin"
Write-Host "  account should own both tasks from now on."

# --- 1. sign out any active session -----------------------------------------#
Phase '1/5  sign out any active session'

# Auto-login meant this account has likely been signed in continuously since
# the station's last boot - stopping the watchdog process below only kills
# what was re-locking the screen, not the session itself. A live session
# keeps its profile files (NTUSER.DAT, etc.) locked, which would otherwise
# make both the registry-hive edit in step 4/5 and the profile removal in
# step 5/5 fail partway with "in use" errors.
# quser's SESSIONNAME column is blank for a disconnected session, which
# shifts the remaining columns left - match the numeric ID directly instead
# of counting whitespace-separated fields.
$loggedOff = 0
$quserLines = Invoke-Native quser @($AutomationAccount)
if ($LASTEXITCODE -eq 0) {
    foreach ($line in ($quserLines | Select-Object -Skip 1)) {
        if ($line -match '^\s*>?\S+\s+(?:\S+\s+)?(\d+)\s+\S+') {
            Invoke-Native logoff.exe @($Matches[1]) | Out-Null
            $loggedOff++
        }
    }
}
if ($loggedOff) {
    Ok "signed $AutomationAccount out of $loggedOff session(s)"
    Note 'waiting for Windows to release its profile files...'
    Start-Sleep -Seconds 5
}
else { Ok "$AutomationAccount has no active session" }

# --- 2. retired scheduled tasks ----------------------------------------------#
Phase '2/5  retired scheduled tasks'

$running = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*cite-lock-watchdog*' })
foreach ($p in $running) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
if ($running) { Ok "stopped $($running.Count) running lock watchdog process(es)" }
else { Ok 'no lock watchdog process was running' }

# Get-ScheduledTask -TaskName alone can miss a task that is genuinely there -
# the PPMS-RT-Client check further down this project's history hit the same
# thing for subfolder tasks. Enumerate every task once instead and filter by
# name client-side, then unregister using the exact TaskPath that was found,
# rather than trusting the parameterized lookup a second time.
$allTasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue)
foreach ($name in 'cite-cli sync', 'cite-cli lock-on-logon', 'cite-cli-bootstrap-temp') {
    $found = @($allTasks | Where-Object { $_.TaskName -eq $name })
    if ($found) {
        foreach ($t in $found) {
            Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Confirm:$false -ErrorAction SilentlyContinue
        }
        Ok "removed task '$name'"
    }
    else { Ok "task '$name' already gone" }
}

# --- 3. auto-login ----------------------------------------------------------#
Phase '3/5  auto-login'

$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $winlogon -Name AutoAdminLogon -Value '0' -Type String
Remove-ItemProperty $winlogon -Name DefaultUserName, DefaultDomainName, DefaultPassword, AutoLogonCount `
    -ErrorAction SilentlyContinue
Ok 'AutoAdminLogon disabled and the stored sign-in identity cleared'

# The password itself lives as an LSA private secret, never in the registry -
# the two lines above never touched it. Delete it the way it was written:
# LsaStorePrivateData with a NULL value deletes the secret it names.
# The type name is unique per run: a type added via Add-Type lives for the
# rest of the PowerShell PROCESS, not just this script - re-running via
# `irm | iex` in the same window without opening a fresh one would otherwise
# silently keep calling whatever version of this class was compiled the
# first time, since a fixed name can never be redefined once loaded.
$lsaTypeName = "CiteLsaRemove_$([guid]::NewGuid().ToString('N'))"
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class $lsaTypeName
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
    // Same native LsaStorePrivateData export as the setup script uses to write
    // the secret, but declared with an IntPtr PrivateData so IntPtr.Zero can be
    // passed for it - the documented way to delete rather than store.
    [DllImport("advapi32.dll", EntryPoint = "LsaStorePrivateData", SetLastError = true)]
    private static extern uint LsaDeletePrivateData(IntPtr PolicyHandle, ref LSA_UNICODE_STRING KeyName, IntPtr PrivateData);
    [DllImport("advapi32.dll")] private static extern uint LsaClose(IntPtr ObjectHandle);
    [DllImport("advapi32.dll")] private static extern int LsaNtStatusToWinError(uint Status);

    // Returns the win32 error (0 = success, 2 = ERROR_FILE_NOT_FOUND when
    // there was no secret to delete) instead of throwing, so the caller can
    // tell "already gone" apart from a real failure.
    public static int Remove(string key)
    {
        LSA_OBJECT_ATTRIBUTES attrs = new LSA_OBJECT_ATTRIBUTES();
        attrs.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
        IntPtr policy;
        uint st = LsaOpenPolicy(IntPtr.Zero, ref attrs, 0x00000024, out policy);
        if (st != 0) return LsaNtStatusToWinError(st);
        LSA_UNICODE_STRING k = new LSA_UNICODE_STRING();
        k.Buffer = Marshal.StringToHGlobalUni(key);
        k.Length = (ushort)(key.Length * 2);
        k.MaximumLength = (ushort)(k.Length + 2);
        try
        {
            st = LsaDeletePrivateData(policy, ref k, IntPtr.Zero);
            return LsaNtStatusToWinError(st);
        }
        finally { Marshal.FreeHGlobal(k.Buffer); LsaClose(policy); }
    }
}
"@
try {
    $lsaError = ([type]$lsaTypeName)::Remove('DefaultPassword')
    if ($lsaError -eq 0) { Ok 'auto-login LSA secret removed' }
    elseif ($lsaError -eq 2) { Ok 'no auto-login LSA secret to remove' }
    else { Warn "could not remove the LSA secret (win32 error $lsaError)" }
}
catch {
    Warn "could not remove the LSA secret: $($_.Exception.Message)"
}

# --- 4. the lock watchdog ----------------------------------------------------#
Phase '4/5  lock watchdog'

# A prior run's own reg.exe unload below can fail to fully release this
# temp-mounted hive (a lingering handle, an interrupted run, etc.) - once
# that happens it stays mounted at the OS level across every later script
# run, regardless of PowerShell session, and keeps NTUSER.DAT locked
# indefinitely with no live user session in sight. Clear it before doing
# anything else, since it is exactly what step 5/5's profile deletion needs
# released.
if (Test-Path 'Registry::HKEY_USERS\CiteCleanupHive') {
    [gc]::Collect()
    [gc]::WaitForPendingFinalizers()
    Invoke-Native reg.exe @('unload', 'HKU\CiteCleanupHive') | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok 'released a registry hive left mounted by an earlier run' }
    else { Warn 'a registry hive from an earlier run is still mounted (HKU\CiteCleanupHive) - a reboot will clear it' }
}

$sid = $null
try {
    $sid = (New-Object Security.Principal.NTAccount($AutomationAccount)).Translate(
        [Security.Principal.SecurityIdentifier]).Value
}
catch {}

if ($sid) {
    $hivePath = "Registry::HKEY_USERS\$sid"
    $loadedKey = $null
    if (-not (Test-Path $hivePath)) {
        # Not currently signed in, so its hive is not mounted at HKU\<sid> -
        # load a private copy from its NTUSER.DAT instead.
        $ntuser = Join-Path (Get-ProfilePath $AutomationAccount) 'NTUSER.DAT'
        if (Test-Path $ntuser) {
            $loadedKey = 'CiteCleanupHive'
            Invoke-Native reg.exe @('load', "HKU\$loadedKey", $ntuser) | Out-Null
            if ($LASTEXITCODE -ne 0) {
                $loadedKey = $null
                Warn "could not load $AutomationAccount's registry hive - is it signed in right now?"
                Note 'sign it out (or reboot) and re-run this script to finish removing the Run key'
            }
            else { $hivePath = "Registry::HKEY_USERS\$loadedKey" }
        }
        else {
            Ok "$AutomationAccount has no profile yet - nothing to clean up here"
        }
    }
    if (Test-Path $hivePath) {
        Remove-ItemProperty -Path (Join-Path $hivePath 'Software\Microsoft\Windows\CurrentVersion\Run') `
            -Name CiteLock -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path (Join-Path $hivePath 'Control Panel\Desktop') `
            -Name ScreenSaveActive, ScreenSaverIsSecure, ScreenSaveTimeOut, 'SCRNSAVE.EXE' `
            -ErrorAction SilentlyContinue
        Ok "removed the lock Run key and screen-saver settings from $AutomationAccount's profile"
    }
    if ($loadedKey) {
        # Release any lingering handle from the registry provider before unload -
        # a live PSDrive reference, not the hive itself, is the usual reason
        # `reg unload` refuses.
        [gc]::Collect()
        [gc]::WaitForPendingFinalizers()
        Invoke-Native reg.exe @('unload', "HKU\$loadedKey") | Out-Null
        if ($LASTEXITCODE -ne 0) { Warn 'could not unload the temporary hive copy - a reboot will clear it' }
    }
}
else { Ok "$AutomationAccount no longer exists - nothing to clean up in its registry hive" }

$removedAny = $false
foreach ($f in 'cite-lock-watchdog.ps1', 'lock-paused', 'setup-user-bootstrap.ps1') {
    $p = "$env:ProgramData\cite-cli\$f"
    if (Test-Path $p) {
        Remove-Item $p -Force -ErrorAction SilentlyContinue
        Ok "removed $p"
        $removedAny = $true
    }
}
if (-not $removedAny) { Ok 'no lock watchdog files left to remove' }

# --- 5. the cite-automation account -------------------------------------------#
Phase '5/5  cite-automation account'

if (Get-LocalUser -Name $AutomationAccount -ErrorAction SilentlyContinue) {
    # Resolve the profile path via its SID before removing the account - once
    # removed, the SID lookup can no longer resolve, and Get-ProfilePath would
    # fall back to guessing "C:\Users\<name>", which is usually but not always
    # where it actually lives.
    $userProfile = Get-ProfilePath $AutomationAccount
    # 'cite-cli renew' (if still registered) runs as this account and will
    # fail until setup-station.ps1 replaces it to run as your admin account
    # instead - expected, since that is the very next step after this script.
    Remove-LocalUser -Name $AutomationAccount
    Ok "account '$AutomationAccount' removed"
}
else {
    Ok "account '$AutomationAccount' already gone"
    # The account can be gone while its profile folder is still stuck behind
    # its now-orphaned SID's permissions (see below) - retry the folder even
    # when there's no account left to resolve its SID from, using the same
    # default path Windows uses.
    $userProfile = "$env:SystemDrive\Users\$AutomationAccount"
}
if (Test-Path $userProfile) {
    # Removing the account does not touch the folder's NTFS permissions - it
    # is still owned by (and ACL'd to) that account's now-orphaned SID, so
    # even an elevated administrator has no access to it until someone
    # explicitly takes ownership first. /A assigns ownership to the
    # Administrators group rather than just this session's user, so it stays
    # accessible regardless of who runs this script next time.
    if (Invoke-NativeTimed -Exe takeown.exe -ExeArgs @('/F', $userProfile, '/R', '/D', 'Y', '/A') -TimeoutSeconds 90) {
        Invoke-NativeTimed -Exe icacls.exe -ExeArgs @($userProfile, '/grant', '*S-1-5-32-544:(OI)(CI)F', '/T', '/C', '/Q') -TimeoutSeconds 60 | Out-Null
    }
    else {
        Warn "takeown timed out after 90s on $userProfile"
        Note 'likely a legacy junction (Application Data or Local Settings) looping back on itself - a known takeown /R hang, not this script stalling'
        Note 'the delete attempt below may still fail without ownership fixed; skip this account by hand if it keeps timing out'
    }
    Remove-Item -Recurse -Force $userProfile -ErrorAction SilentlyContinue
    if (Test-Path $userProfile) {
        Warn "could not fully remove $userProfile - some files may still be in use"
        Note 'sign the account out (or reboot) and re-run this script to finish removing it'
    }
    else { Ok "profile folder removed: $userProfile" }
}
else { Ok 'no leftover profile folder to remove' }

Write-Host "`nCleanup complete. Run setup-station.ps1 next, signed in as whichever admin" -ForegroundColor Green
Write-Host "account should own 'cite-cli clean' and 'cite-cli renew' from now on.`n" -ForegroundColor Green
