# cite-cli

[![CI](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml)

Command line tools for CITE@HMS: **auto-clean drives** and **auto-update the NIS-Elements license** on Windows microscopy stations.

## Contents

- [1. What cite-cli is](#1-what-cite-cli-is)
- [2. Station setup with the script](#2-station-setup-with-the-script)
- [3. Station setup by hand](#3-station-setup-by-hand)
- [4. How the scheduled tasks work](#4-how-the-scheduled-tasks-work)
- [5. The scheduled commands in detail](#5-the-scheduled-commands-in-detail)
- [6. CLI reference](#6-cli-reference)
- [7. Troubleshooting](#7-troubleshooting)

---

## 1. What cite-cli is

A single command, `cite`, run unattended from Windows Task Scheduler on each station. It does two jobs:

- **Clean the drives** — delete acquisition data older than N days.
- **Keep the NIS-Elements license alive** — watch the dongle's expiration, submit renewal requests to Nikon before it lapses, apply them, and send an email only when something happened or something needs a human.

### The three scheduled commands

| Command | Runs as | When | Purpose |
| --- | --- | --- | --- |
| `cite clean` | `Admin` | 12:00–1:00 AM | Delete old files |
| `cite sync` | `cite-automation` | 1:00 AM sharp | Apply pending renewals |
| `cite renew` | `cite-automation` | 1:15–2:15 AM | Submit renewals, detect applied ones |

`cite clean` stays on the admin account because it needs the rights to delete other users' acquisition data. `cite sync` drives the GUI-only License Manager, so it needs a logged-on session, while `cite renew` also watches the sync leg and reports when it stops running.

Locking that desktop is handled separately, by two independent mechanisms so a
single failure cannot leave the station open — see [Locking the automation desktop](#locking-the-automation-desktop).

### Why the renewal is split in two

`cite sync` drives a GUI-only dialog, so it only runs when a Windows session is logged on. `cite renew` does everything that works headlessly. Splitting them means the headless half still runs on a station where nobody signs in, and the interactive half can be scheduled just ahead of it.

Pending submissions are synchronized automatically: the first attempt runs two days after submission and retries every two days until the local ACC expiration date advances. Applying by hand remains available as a fallback. `cite renew` and `cite sync` share `%USERPROFILE%\.cite\renew_state.json`, so **both tasks must run as the same Windows user**.

### The two Windows accounts

| Account | Why it exists |
| --- | --- |
| `Admin` (or whatever the station's admin is called) | Runs `cite clean`, which needs privileges over other users' data. |
| `cite-automation` | An unprivileged account that logs in automatically at boot, so `cite sync` always has a session to work in. Its desktop is locked within seconds and kept locked. |

Each account installs its own `uv`, and each task points at **its own account's** `uv.exe`. Never cross them: besides not being readable across profiles, putting `cite-automation`'s path into the `Admin` task would let anyone who compromised the auto-login account run code as `Admin`.

### Locking the automation desktop

`cite-automation` is signed in permanently, so its desktop must never sit open.
Three mechanisms do that, chosen so that no two of them fail for the same reason:

| Layer | What starts it | Covers |
| --- | --- | --- |
| **Lock watchdog** | a Run key in `cite-automation`'s profile, executed by Explorer at shell startup | the real work — retries until the lock takes, and re-locks the desktop any time it is found open |
| **`cite-cli lock-on-logon`** | Task Scheduler, three staggered logon triggers | a second, independent launcher if Explorer never processes the Run key |
| **Screen saver**, 1 min, *On resume, display logon screen* | the per-user screen saver settings | an unattended desktop that somehow escaped both |

The watchdog is the one that matters. The other two fire once and hope; it keeps
checking, and it writes `C:\Users\cite-automation\.cite\logs\lock.log`, which is
the only place that says *why* a lock failed rather than just that the station
was found unlocked.

Only the screen saver touches user settings, and it is written to
`cite-automation`'s profile alone — your own account and your microscopists'
accounts are never given a forced lock.

#### Working inside the `cite-automation` desktop

The watchdog re-locks every two seconds, so signing in without disarming it first
is unusable. It is already signed in, so you are unlocking its session, not
starting one.

1. **From your admin account**, arm the pause:

   ```powershell
   New-Item C:\ProgramData\cite-cli\lock-paused
   ```

2. **Switch user** — Start menu → your account picture → **Switch user**, or `Win+L` and pick `cite-automation` from the lock screen. Enter its password.
3. Do your work.
4. Leave with **Switch user** or `Win+L`. ⚠️ **Never Sign out** — that destroys the session `cite sync` needs, and it will not come back until the next reboot.
5. **Back in admin**, re-arm:

   ```powershell
   Remove-Item C:\ProgramData\cite-cli\lock-paused
   ```

The pause expires on its own **two hours** after the file was last written, so
forgetting step 5 cannot leave a station open indefinitely. Touch the file again
to extend it. Both the pause and the resume are recorded in `lock.log`, so any
gap in the locking record has a visible reason beside it.

If you get caught out — signed into `cite-automation` without pausing first —
stop the watchdog from your admin account instead of fighting the lock:

```powershell
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -like '*cite-lock-watchdog*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

It matches on the command line, so nothing else running as that account is
affected, and it comes back at the next logon.

### Requirements

1. Windows 10/11.
2. [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — installed per account. Note where `uv.exe` lands (e.g. `C:\Users\Admin\.local\bin\uv.exe`).
3. [`git`](https://git-scm.com/install/) — check with `git --version`. `uv tool run --from git+…` needs it.
4. Make sure the NIS Elements `licmgr_s.exe` file exists under the Public Windows User in a folder named `NIS_Elements`.

---

## 2. Station setup with the script

[`scripts/setup-station.ps1`](./scripts/setup-station.ps1) does the whole setup in one shot: creates the `cite-automation` account and grants it the batch-logon right, sets the alert variables machine-wide, installs `uv` and the log folder for *both* accounts, enables auto-login with the lock task, and registers all four scheduled tasks.

### Before you start

Have these three ready — [doing it by hand](#3-station-setup-by-hand) needs all three every time. The script needs the `cite-automation` and `Admin` passwords on every run too (Windows can check a password you type but never hand back the real one, so there's no way to skip re-typing them). The Gmail App Password is the exception: the script only asks for it if one isn't already set machine-wide, or if you choose to replace it — so keep it ready for the first run, or any run where you want to rotate it.

| Value | How to get it |
| --- | --- |
| **Gmail App Password** | Generate at <https://myaccount.google.com/apppasswords>, label it "cite-cli" |
| `cite-automation` password | The one you set during the setup |
| `Admin` password | The one you logged in with |
| `licmgr_s.exe` | Make sure it exists under the Public Windows User in a folder named `NIS_Elements` |

The Gmail App Password is a 16-character string, and it requires 2-Step Verification to be enabled on that account. It is what lets a station email you when a renewal lands or a command fails.

Google displays it as four groups of four (`abcd efgh ijkl mnop`). Those spaces are only for readability and are **not** part of the password — at the script's prompt, type or paste it either way, it strips them.

### Run it

- [ ] Log in as `Admin`.
- [ ] Open PowerShell **as administrator** — right-click the PowerShell icon → **Run as administrator**. The title bar must read `Administrator: Windows PowerShell`.
- [ ] Run it straight from GitHub — nothing is saved to disk:

```powershell
irm https://raw.githubusercontent.com/CITE-HMS/cite-cli/main/scripts/setup-station.ps1 | iex
```

`irm` fetches the script as text and `iex` runs it in the current session. Because no file lands on disk, this also sidesteps the two things that block a downloaded `.ps1`: the "downloaded from the internet" mark and the execution policy.

If you would rather keep a copy on the station — handy if you expect to re-run it — download it instead:

```powershell
cd $env:USERPROFILE\Downloads
irm https://raw.githubusercontent.com/CITE-HMS/cite-cli/main/scripts/setup-station.ps1 -OutFile setup-station.ps1
Unblock-File .\setup-station.ps1
powershell -ExecutionPolicy Bypass -File .\setup-station.ps1
```

Here `Unblock-File` clears the "downloaded from the internet" mark and `-ExecutionPolicy Bypass` gets past the default policy, which otherwise refuses with *"cannot be loaded because running scripts is disabled on this system"*.

Either way it asks for the `cite-automation` and `Admin` passwords every time, and the Gmail App Password only if one isn't already set or you choose to replace it, then prints what it did.

**Re-running is safe.** Existing accounts are reused and tasks are replaced, so a run that failed halfway is fixed by running it again. A task of the same name is replaced **without asking** (task names are unique, so there is no "keep both"); each line says which happened — `created` or `replaced the existing one`.

### What it does

The five steps it prints, in order:

1. Creates `cite-automation` (password never expires) and grants it **Log on as a batch job**.
2. Sets some needed `VARIABLES` machine-wide, so both accounts inherit them.
3. Installs `uv` for both accounts and creates both `%USERPROFILE%\.cite\logs` folders. It runs as `cite-automation` to do the second half, which also builds that account's profile — so you never have to log into it.
4. Enables auto-login for `cite-automation`, storing the password as an LSA secret.
5. Installs the lock watchdog and its Run key, then registers the four tasks and reports each one.

Re-running it on a station that is already set up is safe and is the normal way to
apply an update: the account is reused rather than recreated, tasks are replaced in
place, and registry keys are written without being cleared. The only thing it will
not do is restart a watchdog that is already running in a live session — that one
picks up the new script at the next logon, and the script says so when it happens.

It also warns — without changing anything — if **fast user switching** is off. With it hidden, signing in as another user logs `cite-automation` off instead of parking its session, and `cite sync` then has no session to work in until the next reboot.

It then checks the third-party `PPMS-RT-Client` task, if the station has one. That task is expected to start at logon of the `User` account only — a trigger left on *any user* would also launch it inside the `cite-automation` auto-login session, once per repetition interval. The script only warns; it never modifies that task.

If one station needs different values — another retention window, a different account name — edit the constants at the top of the script before running it:

```powershell
$AutomationAccount = 'account name'
$Email             = 'email.@email.com'
$FullName          = 'Name'
$CleanDays         = 25
$RepoUrl           = 'git+https://github.com/CITE-HMS/cite-cli'
```

### Verify the station

This part is yours, whichever way you set the station up.

- [ ] Make sure the cli-automation user account is logged in (Win + L or switch account).
- [ ] Run each task from the `Admin` user account: select it in Task Scheduler → **Run** (`renew` first, `sync` after).
- [ ] Check the logs — remember there are **two** folders:

| Task | Log folder |
| --- | --- |
| `renew`, `sync` | `C:\Users\cite-automation\.cite\logs\` |
| `clean` | `C:\Users\Admin\.cite\logs\` |

- [ ] Confirm the license reads correctly, as `cite-automation`:

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite license
```

It should print an expiration date and a HASP ID.

- [ ] Confirm an alert email arrives under **both** accounts:

```power shell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert
```

#### The reboot test

This is the one that proves the setup.

- [ ] **Restart the PC and do not touch it.**
- [ ] It should sign in on its own, show the desktop briefly, and land on the **lock screen** within ~15 seconds.
- [ ] Check `C:\Users\cite-automation\.cite\logs\lock.log`. It must have gained a `locked` line.

If it stops at the sign-in screen, auto-login failed. If it stays on the desktop, `lock.log` says which half broke, which is the whole reason it exists:

| `lock.log` after a reboot | Meaning |
| --- | --- |
| `... watchdog started` then `... locked` | Working — the watchdog locked it. |
| `... watchdog started` and nothing else | Also working. The `lock-on-logon` task won the race and locked first, so the watchdog found the desktop already locked and had nothing to do. |
| `... LockWorkStation returned false (attempt N)`, repeating | The watchdog is running and Windows is refusing the lock. A different problem entirely — not a timing one, and no number of extra triggers will help. |
| empty or missing | The watchdog never started. Check that the `CiteLock` value exists under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` **while signed in as `cite-automation`**, and that `C:\ProgramData\cite-cli\cite-lock-watchdog.ps1` is present. |
| `... paused for maintenance` | A `lock-paused` file is in force. Delete `C:\ProgramData\cite-cli\lock-paused`. |

- [ ] The next morning, confirm `cite sync` and `cite renew` both ran overnight: check `C:\Users\cite-automation\.cite\logs\cite.log` and the Last Run Result of each task.

---

## 3. Station setup by hand

The same result, phase by phase. Use this when a station is unusual, when the script fails, or to understand what it did. Follow the phases in order — several steps depend on earlier ones. Collect the three values in [Before you start](#before-you-start) first, and finish with [Verify the station](#verify-the-station).

### Phase 1 — Create the `cite-automation` account

Log in as `Admin`. Open PowerShell **as administrator** — right-click the PowerShell icon → **Run as administrator**.

Being logged in *as* an administrator is **not** the same as running an *elevated* prompt. Without elevation the next command fails with `New-LocalUser : Access denied.` Two quick tells: an elevated window's title bar reads `Administrator: Windows PowerShell`, and it opens in `C:\Windows\system32` rather than your home folder. To be certain:

```powershell
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

It must print `True` before you continue.

- [ ] Create the account. It will ask for a password — use the standard NIC one.

```powershell
New-LocalUser -Name "cite-automation" -Password (Read-Host -AsSecureString "Password") -PasswordNeverExpires -AccountNeverExpires -FullName "CITE automation"
Add-LocalGroupMember -Group "Users" -Member "cite-automation"
```

- [ ] Confirm it exists:

```powershell
Get-LocalUser cite-automation
```

`-PasswordNeverExpires` matters. If this password ever expires or is changed, auto-login silently stops and the sync leg goes quiet until someone notices.

- [ ] Grant **Log on as a batch job**. The `cite renew` task runs with *Run whether user is logged on or not*, which Windows starts as a **batch logon**. By default only Administrators and Backup Operators hold that right, so a standard account must be given it explicitly:

1. Win+R → `secpol.msc`
2. **Local Policies** → **User Rights Assignment**
3. Double-click **Log on as a batch job**
4. **Add User or Group…** → type `cite-automation` in *Enter the object names to select* → **Check Names** → **OK**

Skipping this is what produces *"This task requires that the user account specified has Log on as batch job rights"* in Phase 6.

### Phase 2 — Set the email alert variables (machine-wide)

Still as `Admin`, in the **elevated** PowerShell.

- [ ] Set all three with `/M` so **both** accounts inherit them:

```powershell
setx /M CITE_ALERT_SMTP_USER "email@email.com"
setx /M CITE_ALERT_SMTP_PASSWORD "xxxx xxxx xxxx xxxx"
setx /M CITE_ALERT_TO "email@email.com"
```

**The `/M` is the whole point.** Plain `setx` writes to the current account only. Since `clean` runs as `Admin` and `renew`/`sync` run as `cite-automation`, a per-user variable leaves one of them unable to email anything — and it fails **silently**, with no error in any log.

Type the app password without spaces here: `setx` stores exactly what you give it, and the spaces are not part of the password.

- [ ] Close and reopen PowerShell, then verify:

```powershell
echo $env:CITE_ALERT_SMTP_USER
```

### Phase 3 — Install `uv` for both accounts

`uv` installs into the profile of whoever runs the installer, so do it **twice** — once per account.

- [ ] As `Admin`, install `uv`: <https://docs.astral.sh/uv/getting-started/installation/>
- [ ] Get the `uv` path and copy it somewhere, it will be needed in Phase 6:

```powershell
where.exe uv
```

- [ ] Create this account's log folder:

```powershell
mkdir "$env:USERPROFILE\.cite\logs" -Force
```

- [ ] Sign out. Log in as `cite-automation` (first login takes a minute while Windows builds the profile).
- [ ] Install `uv` again, and record this account's path too:

```powershell
where.exe uv
```

- [ ] Create this account's log folder:

```powershell
mkdir "$env:USERPROFILE\.cite\logs" -Force
```

Do not skip the two `mkdir` steps or the tasks won't run.

The two paths differ (`C:\Users\Admin\.local\bin\uv.exe` vs `C:\Users\cite-automation\.local\bin\uv.exe`). That is fine and intentional — each account runs its own copy, and neither can tamper with the other's. The uv **cache** is per-account too (`%LOCALAPPDATA%\uv\cache`); leave it that way, because the tasks can overlap and two accounts sharing one cache directory causes lock and permission conflicts.

- [ ] Install `git` if it is not already present: <https://git-scm.com/install/> (check with `git --version`).

### Phase 4 — Enable auto-login for `cite-automation`

- [ ] Log in as `Admin`, elevated PowerShell.
- [ ] Unhide the auto-login checkbox (needed on current Windows 10/11):

```powershell
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device" -Name DevicePasswordLessBuildVersion -Value 0
```

- [ ] Press `Win+R`, type `netplwiz`, press **Enter**.
- [ ] Select `cite-automation` in the list.
- [ ] Uncheck **Users must enter a user name and password to use this computer**.
- [ ] Click **Apply**, enter that account's password twice, click **OK**.

**If the checkbox still is not there** (happens on some domain-joined machines), use [Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon) instead: run `Autologon64.exe` as administrator, enter Username `cite-automation`, Domain = the **computer name**, the password, and click **Enable**. Both methods are equally secure — they store the password as the same encrypted LSA secret.

### Phase 5 — Lock the session immediately after login

Without this, the station boots to an unlocked desktop. Three layers, because
each one fails for a different reason.

#### 5a. The lock watchdog (primary)

- [ ] Save this as `C:\ProgramData\cite-cli\cite-lock-watchdog.ps1`:

```powershell
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

    if (Get-Process LogonUI -ErrorAction SilentlyContinue) {
        $fails = 0
        Start-Sleep -Seconds 5
        continue
    }
    if ([CiteLock]::LockWorkStation()) {
        "$(Get-Date -f s) locked" | Add-Content $log
        $fails = 0
        Start-Sleep -Seconds 5
    }
    else {
        if ($fails % 30 -eq 0) {
            "$(Get-Date -f s) LockWorkStation returned false (attempt $($fails + 1))" |
                Add-Content $log
        }
        $fails++
        Start-Sleep -Seconds 2
    }
}
```

It calls the same `LockWorkStation` that `rundll32` does. What it adds is that
`rundll32` **throws the return value away** — a refused lock was silent and
unrecoverable. This checks it, retries every 2 s until it takes, re-locks if the
desktop is ever found open again, and leaves a log. Nobody is meant to be using
this desktop, so re-locking is always the right answer.

- [ ] Signed in **as `cite-automation`**, add the Run key that starts it:

```powershell
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name CiteLock `
  -Value 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\ProgramData\cite-cli\cite-lock-watchdog.ps1"'
```

A Run key is run by Explorer as part of shell startup, so it cannot fire before
the session is ready, and it cannot miss an event the way a Task Scheduler **at
log on** trigger does when the service is still starting during an auto-login.
That last failure is why staggered triggers never helped: all of them sat inside
the same early window and shared one cause.

#### 5b. The lock task (independent second launcher)

- [ ] In **Task Scheduler**, create a task:

- **General**
  - Name: **cite-cli lock-on-logon**
  - **Run only when user is logged on**, user = `cite-automation`
  - ⚠️ do **not** check "Run with highest privileges"
- **Triggers** — **five**: **At log on** → Specific user `cite-automation`, with **Delay: 5**, **10**, **15**, **30**, and **45 seconds**
- **Actions**
  - Start a program → `C:\Windows\System32\rundll32.exe`
  - Arguments: `user32.dll,LockWorkStation` (no space after the comma)
- **Conditions**: uncheck everything
- **Settings**: **Allow task to be run on demand**, **Stop the task if it runs longer than: 1 hour**, and under *If the task is already running* choose **Run a new instance in parallel**

That last setting matters more than it looks. The default, **Do not start a new
instance**, silently discards every later trigger whenever an earlier one is
still running — so all the backup attempts vanished in exactly the case they
existed for.

The later delays only help if the session simply was not ready yet. A trigger
delay is counted from when Task Scheduler *observes* the logon, so if the service
was still starting during the auto-login and missed the event outright, no number
of triggers at any delay will fire. That is the gap the watchdog covers. Keep this task on `rundll32` rather than the watchdog: it is then a
genuinely independent mechanism, not a second copy competing for the same log.

#### 5c. Screen saver (backstop)

- [ ] Logged in as `cite-automation`, run (`Win+R`) `control desk.cpl,,@screensaver` and set **Screen saver: Blank**, **Wait: 1 minute**, and check **On resume, display logon screen**.

This only fires after a minute of no input, so it cannot help while somebody is
actively at an unlocked desktop — but it covers the unattended case, and it is
per-user, so it never affects your microscopists' accounts. (A machine-wide
inactivity lock, `InactivityTimeoutSecs`, is deliberately **not** used here: it
would lock every user on the station, including someone watching a long
acquisition without touching the mouse.)

### Phase 6 — Create the three scheduled tasks

Every task uses the same **Action**: program `C:\Windows\System32\cmd.exe` with the arguments below. Replace `<uv>` with **that task's own account** `uv.exe` path from Phase 3 — `Admin`'s for `clean`, `cite-automation`'s for `sync` and `renew`.

Common settings unless stated otherwise:

- **Conditions**: uncheck everything
- **Settings**: **Allow task to be run on demand**, **Stop the task if it runs longer than: 1 hour**, **If the running task does not end when requested, force it to stop**

#### 6a — `cite-cli clean` (account: `Admin`)

- **General**: `Admin` · **Run whether user is logged on or not** · **Run with highest privileges**
- **Trigger**: Daily, **12:00:00 AM**, recur every 1 day, **random delay: 1 hour**
- **Settings**: **Stop the task if it runs longer than: 4 hours**
- **Arguments**:

```text
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<uv>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite clean -d 25 -f > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

`-d 25` deletes files older than 25 days; `-f` skips the confirmation prompt (required unattended).

#### 6b — `cite-cli sync` (account: `cite-automation`)

- **General**: `cite-automation` · **Run only when user is logged on** · **Run with highest privileges**
- **Triggers** — **two**:
  - Daily, **1:00:00 AM**, recur every 1 day. ⚠️ **no random delay**
  - **At log on**, `cite-automation` user, **delay 2 minutes** (catch-up after a reboot)
- **Settings**: also check **Run task as soon as possible after a scheduled start is missed**, and set **If the task is already running** → **Do not start a new instance**
- **Arguments**:

```text
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<uv>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite sync > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

The **no random delay** is not optional — `sync` must stay ahead of `renew`.

#### 6c — `cite-cli renew` (account: `cite-automation`)

- **General**: `cite-automation` · **Run whether user is logged on or not** · **Run with highest privileges**
- **Trigger**: Daily, **1:15:00 AM**, recur every 1 day, **random delay: 1 hour**
- **Arguments** (one line — change the name, keep the quotes):

```text
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<uv>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite renew --email citeathms@gmail.com --full-name "Federico Gasparoli" --url nikon > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

Always use <citeathms@gmail.com> as the `--email` value.

Saving a "Run whether user is logged on or not" task asks for that account's password. That is expected — Windows needs it to start the task in the background. If saving fails with *"This task requires that the user account specified has Log on as batch job rights"*, the `secpol.msc` step in Phase 1 was skipped. Grant the right, then save again.

**Resulting schedule:**

| Task | Account | Runs |
| --- | --- | --- |
| `cite clean` | `Admin` | 12:00–1:00 AM |
| `cite sync` | `cite-automation` | 1:00 AM sharp |
| `cite renew` | `cite-automation` | 1:15–2:15 AM |

Now go through [Verify the station](#verify-the-station).

### Undoing it

**Disable auto-login:** `netplwiz` → re-check **Users must enter a user name and password to use this computer**. (Or run Sysinternals Autologon and click **Disable**.)

**Remove the tasks:** delete `cite-cli clean`, `cite-cli renew`, `cite-cli sync` and `cite-cli lock-on-logon` from Task Scheduler.

**Remove the lock watchdog:** delete the `CiteLock` value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` while signed in as `cite-automation`, then delete `C:\ProgramData\cite-cli\cite-lock-watchdog.ps1`. A watchdog already running keeps going until that session ends.

**Remove the account:** `Remove-LocalUser -Name "cite-automation"` from an elevated PowerShell. Its profile folder under `C:\Users\` must be deleted separately if you want the logs and state gone too.

---

## 4. How the scheduled tasks work

Details that apply to every task, however it was created.

### Why `--refresh`

Every task's arguments include `--refresh` on the `uv tool run` line. This tells `uv` to re-fetch the latest commit from GitHub on every invocation instead of using its cached build. Tradeoff: ~one extra small fetch per machine per day (well under a second on a normal network); benefit: any bug fix or feature you push is picked up automatically on every machine the next time the task fires — no manual cache invalidation, no logging into each PC.

To pin a station to a tested version instead, remove `--refresh` and append `@v1.2.3` (or a commit SHA) to the repo URL: `git+https://github.com/CITE-HMS/cite-cli@v1.2.3`.

### Skipping when NIS-Elements is open

The tasks check whether `nis_ar.exe` is running before doing anything. If NIS-Elements is open, the task exits immediately without touching the dongle or the log file, so license operations never interfere with an active microscopy session.

The check is the `||` idiom at the front of every arguments line:

```bat
tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run ...
```

`tasklist` lists all running processes; `findstr /I nis_ar.exe` exits 0 if found, non-zero if not. `||` runs the right-hand side only on failure — i.e. only when NIS-Elements is **not** running. The check itself is silent (`> nul 2>&1`).

> **Why not `tasklist /FI "IMAGENAME eq nis_ar.exe"`?** That form needs inner double quotes, which conflict with the outer `/c "..."` wrapping in Task Scheduler's arguments field and break the command silently. The plain `tasklist | findstr` form avoids all quoting issues.

### Logging

Every `cite` command writes its full output to a rotating log at `%USERPROFILE%\.cite\logs\cite.log` (1 MB × 5 backups). You never need to redirect output yourself — run `cite log` to open that folder.

The `> bootstrap.log 2>&1` redirect in the task arguments covers the rare case where `uvx` itself fails before Python starts (GitHub unreachable, dependency conflict). No Python code runs in that case, so the internal logger never gets a chance. The bootstrap file lives in the same `.cite\logs\` folder.

Remember there is one folder per account: `renew`/`sync` log under `C:\Users\cite-automation\`, `clean` under `C:\Users\Admin\`.

### Email alerts

`cite clean`, `cite renew`, `cite sync` and `cite notify-renewal` send a failure email when they exit non-zero or raise an uncaught exception. If the environment variables are absent, alerting silently no-ops.

| Variable | Value |
| --- | --- |
| `CITE_ALERT_SMTP_USER` | the Gmail account that sends |
| `CITE_ALERT_SMTP_PASSWORD` | its [App Password](https://myaccount.google.com/apppasswords), **not** the real password |
| `CITE_ALERT_TO` | where alerts go |

On a station set up as described above these are set **machine-wide** (`setx /M`, or the script), because the tasks are split across two accounts. To use a non-Gmail SMTP server, also set `CITE_ALERT_SMTP_HOST` (default `smtp.gmail.com`) and `CITE_ALERT_SMTP_PORT` (default `587`, STARTTLS).

**Station names in subjects.** When the HASP ID is recognised, subjects and bodies use the station name instead of the hostname. The mapping lives in `src/cite/_renew.py` (`HASP_ID_TO_STATIONS_MAP`). A renewal on HASP ID `09882A98` produces:

```
Subject: [cite-cli] NIS-Elements license renewed on Station 2
Body:    Station:     Station 2
```

If the HASP ID is not in the map, the subject falls back to the machine hostname.

**Verify with a test email:**

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert
```

You should see `Test alert sent to ...` and an email within seconds. The subject reads `[cite-cli] test-alert failed on <hostname>` — the word "failed" is intentional, it uses the same template as real failures. If it fails, the command prints the most common causes (wrong App Password, 2FA not enabled, port 587 blocked).

### What to expect day to day

On a normal day **nothing happens and no email arrives**. That is correct.

The station emails you when:

- ✅ the license was renewed (`NIS-Elements license renewed on <Station>`)
- ⚠️ a renewal is pending and `cite sync` has not run for 4 days — usually means auto-login broke
- ⚠️ expiry is within 4 days and Nikon has not replied
- ❌ any command failed

---

## 5. The scheduled commands in detail

### `cite clean` — delete old files on a schedule

Deletes files older than N days from one or more directories. When no directory is given, it cleans all default paths found on the machine (`D:/User_Data`, `E:/User_Data`, etc.). Sends a failure alert email if it crashes.

**Task Scheduler arguments** (runs daily):

```bat
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite clean -d 25 -f > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

- `-d 25` — delete files older than 25 days (adjust as needed).
- `-f` — skip the confirmation prompt (required for unattended runs).

To clean a specific directory instead of the defaults, add the path as the first argument:

```bat
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite clean D:\MyData -d 30 -f > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

### `cite renew` — headless monitor, submit, and detection task

Runs the headless part of the renewal loop daily, whether the Windows user is logged on or not:

**Step 1 — detect a completed renewal:** if the dongle's expiration advanced since the last recorded baseline, it sends the confirmation email (`[cite-cli] NIS-Elements license renewed on <Station>`). The scheduled Apps Script in [`scripts/sheet_tracker.gs`](./scripts/sheet_tracker.gs) reads that email, appends the renewal to the tracking Sheet, and creates one recurring all-day event in the account's default Google Calendar. Its three weekly occurrences fall 14 days before, 7 days before, and on the new expiration date. Any stale pending-submission state is cleared.

**Step 2 — submit:** reads the dongle's expiration via ACC, checks the renewal window (default 14 days), and submits a fresh `.c2l` to Nikon if needed. While a submission is pending and the license is within 4 days of expiry, it still sends an URGENT reminder email (throttled to one per 20 h).

**Step 3 — watch the sync leg:** `cite sync` needs a logged-on Windows session, so on a station where nobody signs in Task Scheduler never starts it — and it therefore cannot report its own failure. While a submission is pending, `renew` checks the synchronization-attempt timestamp and, once no attempt has been recorded for 4 days (two missed retry windows), sends a failure alert naming the likely cause. Throttled to one per 48 h. Because a submission normally lands ~14 days before expiry, this surfaces a dead sync leg around 10 days out instead of leaving it silent until the 4-day urgency alerts.

**Details:**

- Expiration is read live from the local Sentinel HASP dongle via ACC at `http://localhost:1947`.
- The `.c2l` is auto-generated by running `nis_hasp_update.exe -r` (discovered under `C:\Program Files\NIS-Elements*\HASP\`).
- The submission note includes the HASP ID (e.g. `09882A98`) so Nikon's staff can identify the dongle.
- **Idempotent**: once submitted for a given expiration date, it won't re-submit until the renewal is applied. `%USERPROFILE%\.cite\renew_state.json` stores the submission and synchronization-attempt timestamps shared with `cite sync`.
- The renewal-detection baseline (`%USERPROFILE%\.cite\last_notified_renewal.json`) auto-seeds on the first run of a fresh machine — no setup step needed.
- Alert throttles live in `%USERPROFILE%\.cite\last_urgency_alert.json` (20 h) and `%USERPROFILE%\.cite\last_sync_alert.json` (48 h). A send that fails does not consume the interval, so it retries on the next run.

**Task Scheduler arguments** (runs daily):

```bat
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite renew --email you@example.com --full-name "Your Name" --url nikon > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

Configure this task with **Run whether user is logged on or not** and schedule it *after* `cite sync` (e.g. 01:15 against sync's 01:00). Running it second lets it pick up a renewal that sync just applied — or that was applied by hand — and send the confirmation email the same night. The command line does not need a `--no-sync` flag; headless mode is the default. `--sync` remains available as an explicit legacy override.

On most days all steps exit cleanly — no renewal detected and the license is not yet within the 14-day window — so the net effect is a quick log line and exit 0.

**Optional overrides:**

- If `nis_hasp_update.exe` is not auto-discovered, set its path:

  ```powershell
  setx CITE_RUS_EXE "C:\custom\path\to\nis_hasp_update.exe"
  ```

- To supply a pre-generated `.c2l` instead of auto-generating one:

  ```bat
  ... cite renew --email ... --full-name ... --url nikon --c2l-file C:\path\to\file.c2l
  ```

**Dry-run (no side effects):**

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite renew `
    --email you@example.com --full-name "Your Name" --url nikon `
    --force --dry-run
```

`--force` bypasses the renewal-window check; `--dry-run` skips the POST and the `.c2l` generation. Nothing is submitted or written.

### `cite sync` — interactive synchronization task

Beginning 48 hours after a real Nikon submission, starts License Manager invisibly and invokes its Synchronize action. It exits successfully without opening License Manager when no synchronization is due. A real attempt records its timestamp so retries occur every two days.

The vendor does not expose Synchronize as a native command-line switch, so this command requires an interactive Windows desktop. Configure the task with **Run only when user is logged on**, using the same Windows account as `cite renew`. Locking the workstation is supported; signing out is not. Give the task a daily trigger and, optionally, an **At log on** trigger delayed by 1–2 minutes.

Schedule it *before* the daily `cite renew`, with no random delay so that ordering always holds. `cite sync` only sends the confirmation email when its own synchronization advanced ACC; a renewal applied by any other route is reported by `cite renew`, so running renew second keeps that detection within the same night.

**Task Scheduler arguments** (runs daily, e.g. 01:00):

```bat
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite sync > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

The automatic action uses `%PUBLIC%\NIS_Elements\licmgr_s.exe` by default. Set `CITE_LICENSE_MANAGER_EXE` to a different full path when needed. ACC — not the GUI status — is the success gate. A launch failure, confirmed License Manager failure, or post-sync ACC verification failure exits non-zero and sends the standard failure alert when SMTP alerts are configured. Successful-looking syncs that leave ACC unchanged remain pending; after 6+ days they are escalated as stalled.

Note that a task configured **Run only when user is logged on** does not fail when nobody is signed in — Task Scheduler simply never starts it, so no exit code and no alert are produced. `cite renew` covers that blind spot: see step 3 above.

### `cite notify-renewal` — manual renewal check (optional)

Runs the same renewal-detection check as `cite renew` step 1 (a confirmation email when the expiry advanced), as a standalone command. The scheduled Apps Script consumes that email to update the tracking Sheet and create the Google Calendar reminder series. **You do not need to schedule this if `cite renew` is scheduled** — the check runs there daily. It exists for manual, one-off use, e.g. right after applying an update by hand when you don't want to wait for the next scheduled run.

**No duplicate emails:** the check only fires when the current expiry is *newer* than what's recorded in `%USERPROFILE%\.cite\last_notified_renewal.json`; once notified, re-running is a no-op. The baseline auto-seeds on first run; `--seed` re-baselines explicitly without sending an email.

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite notify-renewal
```

---

## 6. CLI reference

All commands follow the pattern:

```
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite <command> [options]
```

Or, if installed locally:

```
cite <command> [options]
```

### `cite clean`

Delete files older than N days from a directory (or all default paths if none is given).

```
cite clean [DIRECTORY] [OPTIONS]
```

| Argument / Option | Short | Default | Env var | Description |
| --- | --- | --- | --- | --- |
| `DIRECTORY` |  | _(none)_ |  | Directory to clean. Accepts a local path or `smb://` URL. If omitted, all default paths found on the machine are cleaned. For SMB: username defaults to `Admin`; set password via `CITE_PASSWORD`. |
| `--days` | `-d` | `30` |  | Delete files older than this many days. |
| `--dry-run` | `-n` | `False` |  | Print what would be deleted without deleting anything. |
| `--force` | `-f` | `False` |  | Delete without asking for confirmation. Required for unattended runs. |
| `--delete-empty-dirs` |  | `True` |  | Also remove empty directories after file deletion. |
| `--skip` |  | `"delete"` |  | Skip files whose path contains this string. |

### `cite renew`

Monitor the NIS-Elements Time-DEMO license, submit to Nikon within the renewal window, and send the success email only after ACC reports a later expiration. GUI synchronization is disabled by default so the command can run without a logged-on Windows session. Also alerts when the separate `cite sync` task has not run for 4 days while a submission is pending.

```
cite renew --email EMAIL --full-name NAME --url TARGET [OPTIONS]
```

| Option | Short | Default | Env var | Description |
| --- | --- | --- | --- | --- |
| `--email` |  | _(required)_ | `CITE_LICENSE_EMAIL` | Email address to put in the renewal form. |
| `--full-name` |  | _(required)_ | `CITE_LICENSE_FULL_NAME` | Full name to put in the renewal form. |
| `--url` |  | _(required)_ | `CITE_LICENSE_URL` | Renewal target: `nikon` (real endpoint) or `test` (local mock at `http://127.0.0.1:8765/`). |
| `--c2l-file` |  | _(auto-generate)_ | `CITE_LICENSE_C2L_FILE` | Path to a pre-generated `.c2l` file. If omitted, generates one via `nis_hasp_update.exe`. Use `mock` to use the bundled test file (for `--url test`). |
| `--note` |  | `"CITE @ Harvard Medical School"` | `CITE_LICENSE_NOTE` | Free-text note included with the submission. The HASP ID is always appended automatically. |
| `--days-before` |  | `14` |  | Submit only when the license expires within this many days. |
| `--dry-run` | `-n` | `False` |  | Print what would be submitted without making any HTTP request or generating a `.c2l`. |
| `--force` | `-f` | `False` |  | Submit even if the license is outside the renewal window or was already submitted this cycle. |
| `--sync` / `--no-sync` |  | `--no-sync` |  | Opt into the legacy combined workflow that also runs a due GUI synchronization. Prefer a separate scheduled `cite sync`. |

### `cite sync`

Synchronize a pending Nikon renewal when its two-day retry interval is due. Reads and updates `%USERPROFILE%\.cite\renew_state.json`; otherwise exits as a successful no-op. Requires Windows and an interactive logged-on session.

```text
cite sync
```

### `cite notify-renewal`

Run the renewal-detection check (a confirmation email when the dongle's expiration has advanced since the last notification). The scheduled Apps Script consumes the email to update the tracking Sheet and create the Google Calendar reminder series. Idempotent — re-running with no change is a no-op. The same check runs automatically on every `cite renew`.

```
cite notify-renewal [OPTIONS]
```

| Option | Default | Description |
| --- | --- | --- |
| `--seed` | `False` | Record the current dongle state as the baseline without sending an email. Only needed to re-baseline explicitly — the baseline auto-seeds on first run. |

**State file:** `%USERPROFILE%\.cite\last_notified_renewal.json` — written atomically; contains `hasp_id`, `expiration_date`, and `notified_at`.

**Edge cases handled automatically:**

- No baseline yet (fresh machine): seeds silently without sending an email.
- HASP ID changed (dongle replaced): updates baseline silently without sending an email.
- SMTP configured but delivery fails: tracking file is **not** updated, so the next run retries.
- SMTP not configured: tracking file advances silently (no email, no error).

### `cite license`

Read the license expiration date and HASP ID from the local Sentinel HASP dongle.

```
cite license [OPTIONS]
```

| Option | Default | Description |
| --- | --- | --- |
| `--raw` | `False` | Dump the unfiltered ACC features feed (useful for troubleshooting why a date didn't parse). |

Example output:

```text
[2026-05-14 ...] License expires 2026-06-05 (22 days left).
HASP ID: 159918744
```

### `cite sync-license`

Manually force the hidden License Manager Synchronize action. Useful for testing the adapter on a given machine/HASP scope without waiting for a pending submission to become due — unlike `cite sync`, it doesn't touch `renew_state.json` and doesn't send alert emails.

```
cite sync-license [OPTIONS]
```

| Option | Default | Description |
| --- | --- | --- |
| `--hasp-id` | _(local ACC)_ | HASP key ID to synchronize. If omitted, reads the current one from the local ACC. |
| `--timeout` | `180` | Seconds to wait for the License Manager Synchronize dialog. |

Requires Windows and `%PUBLIC%\NIS_Elements\licmgr_s.exe` (or `CITE_LICENSE_MANAGER_EXE`). Prints the raw synchronization status/message and exits non-zero on failure, so it's safe to run once per station to confirm the adapter works there:

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite sync-license
```

### `cite request-file`

Manually generate a fresh `.c2l` renewal request file by running `nis_hasp_update.exe -r`.

```
cite request-file [OPTIONS]
```

| Option | Short | Default | Description |
| --- | --- | --- | --- |
| `--output` | `-o` | `%USERPROFILE%\.cite\generated_request.c2l` | Where to write the `.c2l` file. |

If `nis_hasp_update.exe` is not auto-discovered, set `CITE_RUS_EXE` to its full path.

### `cite test-alert`

Send a one-off test failure email to verify SMTP configuration. No failure is needed.

```
cite test-alert
```

No options. Requires `CITE_ALERT_SMTP_USER`, `CITE_ALERT_SMTP_PASSWORD` and `CITE_ALERT_TO` to be set. Prints the most common failure causes if sending fails.

### `cite update`

Update `cite-cli` itself to the latest version from GitHub.

```
cite update
```

No options.

### `cite log`

Open the `~/.cite/logs/` folder in the system file manager (Explorer on Windows, Finder on macOS). The folder contains:

- **`cite.log`** — rotating log of all `cite` command output (1 MB × 5 backups). Written automatically by every command.
- **`bootstrap.log`** — the Task Scheduler redirect target, covering the rare case where `uvx` fails before Python starts.

```
cite log
```

No options.

### Global options

| Option | Short | Description |
| --- | --- | --- |
| `--version` | `-v` | Print the installed version and exit. |
| `--help` |  | Show help for any command. |

---

## 7. Troubleshooting

Problems seen on real stations, with the fix. Most are one-time per PC.

### `New-LocalUser : Access denied.`

**Cause:** the PowerShell window is not **elevated**. Being logged in *as* an administrator is not the same thing — UAC still requires an explicitly elevated prompt.

**Tells:** an elevated window's title bar reads `Administrator: Windows PowerShell`, and it opens in `C:\Windows\system32` rather than `C:\Users\<you>`. If your prompt shows your home folder, it is not elevated.

**Fix:** right-click PowerShell → **Run as administrator**. Verify with:

```powershell
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

It must print `True`. If it prints `True` and access is *still* denied, confirm the account is in the local admins group with `net localgroup Administrators`; on a domain-joined station, policy can also block local account creation.

### "This task requires that the user account specified has Log on as batch job rights."

**Cause:** saving a task set to **Run whether user is logged on or not** starts it as a *batch logon*, and by default only Administrators and Backup Operators hold that right. Task Scheduler normally grants it automatically, but cannot when Group Policy manages it.

**Affects:** the `cite-cli renew` task only. `cite-cli sync` uses an *interactive* logon, and `cite-cli clean` runs as `Admin`, who already has the right.

**Fix:** `secpol.msc` → **Local Policies** → **User Rights Assignment** → **Log on as a batch job** → **Add User or Group…** → `cite-automation`. Then save the task again (it re-asks for the password). This is Phase 1's grant step; the setup script does it with `secedit`.

On Windows **Home** there is no `secpol.msc` — add `cite-automation` to the local **Backup Operators** group instead, which already holds the right.

### The `netplwiz` auto-login checkbox is missing

**Cause:** current Windows 10/11 hide it by default.

**Fix:** run the `DevicePasswordLessBuildVersion` command from [Phase 4](#phase-4--enable-auto-login-for-cite-automation), then close and reopen `netplwiz`. If it is still absent (some domain-joined configurations), use [Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon) instead — equally secure, same underlying mechanism.

### The station stops at the BitLocker PIN screen after a reboot

**Cause:** BitLocker in **TPM + PIN** mode asks for a PIN before Windows starts loading. Auto-login only answers the *Windows* sign-in screen and cannot help.

**Fix:** there is no software workaround — either move that station to TPM-only unlock, or accept that `cite sync` runs only when someone is physically present. Check the mode with `manage-bde -status C:`.

### No emails ever arrive

**Cause:** almost always the alert variables were set with plain `setx` (current account only) instead of `setx /M` (machine-wide). Alerting then **silently no-ops** — there is no error in any log.

**Fix:** redo [Phase 2](#phase-2--set-the-email-alert-variables-machine-wide) with `/M` from an elevated prompt, then verify from **both** accounts:

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert
```

An email must arrive under `Admin` *and* under `cite-automation`.

### `uvx` is not recognised under one of the accounts

**Cause:** `uv` installs per profile. It was installed for one account only.

**Fix:** log in as the account that is missing it and install `uv` again ([Phase 3](#phase-3--install-uv-for-both-accounts)). Each account has its own `uv.exe` path — give every task **its own account's** path, never the other's.

### A task shows Last Run Result `0x1`

The command ran and failed. Open that account's log — `renew`/`sync` under `C:\Users\cite-automation\.cite\logs\`, `clean` under `C:\Users\Admin\.cite\logs\` — and read the last entries in `cite.log`. A failure email should also have been sent; if not, see "No emails ever arrive".

### The station is on the desktop, not the lock screen, after a reboot

**Read `C:\Users\cite-automation\.cite\logs\lock.log` first.** It distinguishes the two causes, which need opposite fixes — see the table under [The reboot test](#the-reboot-test).

**If the log is empty or missing,** the watchdog never started. Signed in as `cite-automation`, confirm the `CiteLock` value exists under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` and that `C:\ProgramData\cite-cli\cite-lock-watchdog.ps1` is present and readable by that account.

**If it repeats `LockWorkStation returned false`,** the launcher is fine and Windows is refusing the lock itself. Adding triggers or raising delays will not help. Check for a policy disabling **Lock Computer** (`DisableLockWorkstation` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Policies\System`), and confirm the task's "Run with highest privileges" is **un**checked.

**If it says `paused for maintenance`,** somebody left a pause file behind. Delete `C:\ProgramData\cite-cli\lock-paused`. It would have expired by itself two hours after it was last written.
