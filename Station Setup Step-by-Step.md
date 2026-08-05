# Station Setup — Step by Step

A linear checklist for setting up **one** Windows station from scratch. Follow
the phases in order — several steps depend on earlier ones.

This document is the **how**. For the **why** behind each decision (why a
separate account, why sync runs before renew, what each email means), see
[Auto-Update of Element License.md](./Auto-Update%20of%20Element%20License.md).

**Time required:** ~30 minutes, plus one reboot at the end.

---

## Phase 0 — Collect what you need

Fill this in before you start. You will paste these values repeatedly.

| Value | How to get it | Yours |
| --- | --- | --- |
| Station name | e.g. `Station 2` | |
| Computer name | `hostname` in PowerShell | |
| Gmail App Password | <https://myaccount.google.com/apppasswords>, label it "cite-cli" | |
| `cite-automation` password | invent a strong one, record it in the lab password store | |
| `uv.exe` path (Admin) | filled in at Phase 3 | |
| `uv.exe` path (cite-automation) | filled in at Phase 3 | |

⚠️ The Gmail App Password is a 16-character string, **not** the real password of
the <citeathms@gmail.com> account. It requires 2-Step Verification to be enabled.

☐ **Check BitLocker before going further.** In an **admin** PowerShell:

```
manage-bde -status C:
```

Under **Key Protectors**, `TPM` alone (or BitLocker being off) is fine. If it
says **`TPM And PIN`**, stop — the station asks for a PIN before Windows even
starts loading, so auto-login cannot work and the whole `cite sync` leg will
only run when a human is physically present. Resolve that first.

---

## Phase 1 — Create the `cite-automation` account

Log in as **`Admin`**. Open PowerShell **as administrator** — right-click the
PowerShell icon → **Run as administrator**.

⚠️ Being logged in *as* an administrator is **not** the same as running an
*elevated* prompt. Without elevation the next command fails with
`New-LocalUser : Access denied.` Two quick tells: an elevated window's title bar
reads `Administrator: Windows PowerShell`, and it opens in
`C:\Windows\system32` rather than your home folder. To be certain:

```
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

It must print `True` before you continue.

☐ Create the account:

```
New-LocalUser -Name "cite-automation" -Password (Read-Host -AsSecureString "Password") -PasswordNeverExpires -AccountNeverExpires -FullName "CITE automation"
Add-LocalGroupMember -Group "Users" -Member "cite-automation"
```

☐ Confirm it exists:

```
Get-LocalUser cite-automation
```

⚠️ `-PasswordNeverExpires` matters. If this password ever expires or is changed,
auto-login silently stops and the sync leg goes quiet until someone notices.

☐ **Grant "Log on as a batch job".** The `cite renew` task runs with *Run
whether user is logged on or not*, which Windows starts as a **batch logon**. By
default only Administrators and Backup Operators hold that right, so a standard
account must be given it explicitly:

1. `Win+R` → `secpol.msc`
2. **Local Policies** → **User Rights Assignment**
3. Double-click **Log on as a batch job**
4. **Add User or Group…** → type `cite-automation` → **Check Names** → **OK**

⚠️ Skip this and saving the `cite-cli renew` task in Phase 7 fails with
*"This task requires that the user account specified has Log on as batch job
rights."* Task Scheduler usually grants the right on its own, but cannot when
Group Policy manages it — which is why doing it up front is safer.

ℹ️ `secpol.msc` does not exist on Windows **Home** editions. There, either add
`cite-automation` to the local **Backup Operators** group (which already holds
the right) or grant it with `secedit` export/import.

ℹ️ It starts as a **standard user** (the `Users` group). Phase 4 tests whether
that is enough; if not, you will promote it there.

---

## Phase 2 — Set the email alert variables (machine-wide)

Still as **`Admin`**, in the **elevated** PowerShell.

☐ Set all three with `/M` so **both** accounts inherit them:

```
setx /M CITE_ALERT_SMTP_USER "citeathms@gmail.com"
setx /M CITE_ALERT_SMTP_PASSWORD "xxxx xxxx xxxx xxxx"
setx /M CITE_ALERT_TO "citeathms@gmail.com"
```

⚠️ **The `/M` is the whole point.** Plain `setx` writes to the current account
only. Since `clean` runs as `Admin` and `renew`/`sync` run as `cite-automation`,
a per-user variable leaves one of them unable to email anything — and it fails
**silently**, with no error in any log.

☐ Close and reopen PowerShell, then verify:

```
echo $env:CITE_ALERT_SMTP_USER
```

---

## Phase 3 — Install `uv` for both accounts

`uv` installs into the profile of whoever runs the installer, so do it **twice** —
once per account. You are logging into both accounts during this setup anyway.

☐ As **`Admin`**, install `uv`: <https://docs.astral.sh/uv/getting-started/installation/>

☐ Record the path — write it into the Phase 0 table:

```
where.exe uv
```

☐ Sign out. Log in as **`cite-automation`** (first login takes a minute while
Windows builds the profile).

☐ Install `uv` again, and record this account's path too:

```
where.exe uv
```

ℹ️ The two paths differ (`C:\Users\Admin\.local\bin\uv.exe` vs
`C:\Users\cite-automation\.local\bin\uv.exe`). That is fine and intentional —
each account runs its own copy, and neither can tamper with the other's.

⚠️ **Never point a task at the other account's `uv.exe`.** Besides simply not
being readable across profiles, putting `cite-automation`'s path into the
`Admin` `clean` task would let anyone who compromised the auto-login account
swap that binary and run code as `Admin`. Each task uses its own account's path.

ℹ️ The uv **cache** is per-account too (`%LOCALAPPDATA%\uv\cache`) — leave it
that way. The tasks can overlap, and two accounts sharing one cache directory
causes lock and permission conflicts.

☐ Install `git` if it is not already present: <https://git-scm.com/install/>
(check with `git --version`).

---

## Phase 4 — Decide: standard user or administrator?

Still logged in as **`cite-automation`**.

☐ Confirm alerting reaches this account:

```
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert
```

An email must arrive. If it says the env vars are not set, Phase 2 was done
without `/M` — go back and redo it.

☐ Test whether License Manager works **without** administrator rights:

```
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite sync-license
```

This drives the real License Manager but touches no renewal state and sends no
email, so it is safe to run repeatedly.

**Read the result:**

| What happens | What it means | What to do |
| --- | --- | --- |
| `Synchronization status: Completed` | A standard user is enough | Nothing — leave the account as-is ✅ |
| A **UAC credential prompt** appears, or it hangs then times out | License Manager needs elevation | Promote the account (below) |
| `Automatic license synchronization requires Windows.` | You are not on Windows | You are running this on the wrong machine |

☐ **Only if elevation is required**, from an **admin** PowerShell:

```
Add-LocalGroupMember -Group "Administrators" -Member "cite-automation"
```

Then re-run `cite sync-license` to confirm it now completes.

⚠️ Promoting the account means the auto-login credential is an administrator
one. That is a real security downgrade — but the account is still purpose-made
and local, so it remains better than auto-logging-in your everyday `Admin`.

---

## Phase 5 — Enable auto-login for `cite-automation`

☐ As **`Admin`** in an **elevated** PowerShell, unhide the auto-login checkbox
(needed on current Windows 10/11):

```
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device" -Name DevicePasswordLessBuildVersion -Value 0
```

☐ Press `Win+R`, type `netplwiz`, press **Enter**.

☐ Select **`cite-automation`** in the list.

☐ Uncheck **Users must enter a user name and password to use this computer**.

☐ Click **Apply**, enter that account's password twice, click **OK**.

**If the checkbox still is not there** (happens on some domain-joined machines),
use [Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon)
instead: run `Autologon64.exe` as administrator, enter Username
`cite-automation`, Domain = the **computer name**, the password, and click
**Enable**. Both methods are equally secure — they store the password as the
same encrypted LSA secret.

⚠️ Do **not** use the `regedit` → `DefaultPassword` method found in most web
guides. That one stores the password in the registry in **plain text**.

---

## Phase 6 — Lock the session immediately after login

Without this, the station boots to an unlocked desktop.

☐ In **Task Scheduler**, create a task:

- **General**
    - Name: **cite-cli lock-on-logon**
    - **Run only when user is logged on**, user = `cite-automation`
    - ⚠️ do **not** check "Run with highest privileges"
- **Triggers** — add **two**:
    - **At log on** → Specific user `cite-automation` → **Delay: 5 seconds**
    - **At log on** → Specific user `cite-automation` → **Delay: 15 seconds**
- **Actions**
    - Start a program → `C:\Windows\System32\rundll32.exe`
    - Arguments: `user32.dll,LockWorkStation` (no space after the comma)
- **Conditions**: uncheck everything
- **Settings**: only **Allow task to be run on demand**

Or from an **admin** PowerShell:

```
$U  = "$env:COMPUTERNAME\cite-automation"
$A  = New-ScheduledTaskAction -Execute "rundll32.exe" -Argument "user32.dll,LockWorkStation"
$T1 = New-ScheduledTaskTrigger -AtLogOn -User $U
$T1.Delay = "PT5S"
$T2 = New-ScheduledTaskTrigger -AtLogOn -User $U
$T2.Delay = "PT15S"
$P  = New-ScheduledTaskPrincipal -UserId $U -LogonType Interactive
Register-ScheduledTask -TaskName "cite-cli lock-on-logon" -Action $A -Trigger $T1,$T2 -Principal $P
```

ℹ️ Two triggers because locking too early can silently do nothing. The second is
the safety net; locking an already-locked session is a harmless no-op.

☐ **Screen saver safety net.** Logged in as `cite-automation`, run (`Win+R`)
`control desk.cpl,,@screensaver` and set **Screen saver: Blank**, **Wait: 1
minute**, and check **On resume, display logon screen**. This is the only layer
that Windows enforces continuously, so it catches a station that somehow missed
both triggers.

---

## Phase 7 — Create the three scheduled tasks

Every task uses the same **Action**: program `C:\Windows\System32\cmd.exe` with
the arguments below. Replace `<uv>` with **that task's own account** `uv.exe`
path from Phase 3 — `Admin`'s path for `clean`, `cite-automation`'s for `sync`
and `renew`.

Common settings unless stated otherwise:

- **Conditions**: uncheck everything
- **Settings**: **Allow task to be run on demand**, **Stop the task if it runs longer than: 1 hour**, **If the running task does not end when requested, force it to stop**

### 7a — `cite-cli clean` (account: `Admin`)

- **General**: `Admin` · **Run whether user is logged on or not** · **Run with highest privileges**
- **Trigger**: Daily, **12:00:00 AM**, recur every 1 day, **random delay: 1 hour**
- **Arguments**:

```
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<uv>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite clean -d 25 -f > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

ℹ️ `-d 25` deletes files older than 25 days; `-f` skips the confirmation prompt
(required unattended). This task stays on `Admin` because it needs rights to
delete other users' acquisition data.

### 7b — `cite-cli sync` (account: `cite-automation`)

- **General**: `cite-automation` · **Run only when user is logged on** · **Run with highest privileges**
- **Triggers** — add **two**:
    - Daily, **1:00:00 AM**, recur every 1 day. ⚠️ **no random delay**
    - **At log on**, same user, **delay 2 minutes** (catch-up after a reboot)
- **Settings**: also check **Run task as soon as possible after a scheduled start is missed**, set **Stop the task if it runs longer than: 10 minutes**, and set **If the task is already running** → **Do not start a new instance**
- **Arguments**:

```
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<uv>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite sync > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"

```

⚠️ The **no random delay** is not optional — `sync` must stay ahead of `renew`.

### 7c — `cite-cli renew` (account: `cite-automation`)

- **General**: `cite-automation` · **Run whether user is logged on or not** · **Run with highest privileges**
- **Trigger**: Daily, **1:15:00 AM**, recur every 1 day, **random delay: 1 hour**
- **Arguments** (one line — change the name, keep the quotes):

```
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<uv>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite renew --email citeathms@gmail.com --full-name "Federico Gasparoli" --url nikon > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

⚠️ Always use <citeathms@gmail.com> as the `--email` value.

ℹ️ Saving a "Run whether user is logged on or not" task asks for that account's
password. That is expected — Windows needs it to start the task in the
background.

⚠️ If saving fails with *"This task requires that the user account specified has
Log on as batch job rights"*, the `secpol.msc` step in **Phase 1** was skipped.
Grant the right, then save again.

**Resulting schedule:**

| Task | Account | Runs |
| --- | --- | --- |
| `cite clean` | `Admin` | 12:00–1:00 AM |
| `cite sync` | `cite-automation` | 1:00 AM sharp |
| `cite renew` | `cite-automation` | 1:15–2:15 AM |

---

## Phase 8 — Verify

☐ Run each task manually: select it in Task Scheduler → **Run** (▶️).

☐ Check **Last Run Result** for each: **`0x0`** = success, `0x1` = failed.

☐ Check the logs — remember there are **two** folders:

| Task | Log folder |
| --- | --- |
| `renew`, `sync` | `C:\Users\cite-automation\.cite\logs\` |
| `clean` | `C:\Users\Admin\.cite\logs\` |

☐ Confirm the license reads correctly, as `cite-automation`:

```
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite license
```

It should print an expiration date and a HASP ID.

☐ **Lock test**: with `cite-automation` logged in, press `Win+L`, then from
Task Scheduler on another session (or via the `Admin` account) run the
`cite-cli sync` task and confirm it still completes. This is the behaviour the
whole auto-login design depends on.

---

## Phase 9 — The reboot test

This is the one that proves the setup.

☐ **Restart the PC and do not touch it.**

☐ It should sign in on its own, show the desktop briefly, and land on the
**lock screen** within ~15 seconds.

⚠️ If it stays on the desktop, the lock task failed — fix that before leaving
the station. If it stops at the sign-in screen, auto-login failed — redo Phase 5.

☐ The next morning, confirm `cite sync` and `cite renew` both ran overnight:
check `C:\Users\cite-automation\.cite\logs\cite.log` and the Last Run Result of
each task.

---

## Done — what to expect from here

On a normal day **nothing happens and no email arrives**. That is correct.

The station will email you when:

- ✅ the license is renewed (`NIS-Elements license renewed on <Station>`)
- ⚠️ a renewal is pending and `cite sync` has not run for 4 days — usually means auto-login broke
- ⚠️ expiry is within 4 days and Nikon has not replied
- ❌ any command fails

See the [email reference](./Auto-Update%20of%20Element%20License.md) for the full
list and what each one means.

---

## Troubleshooting

Problems seen on real stations, with the fix. Most are one-time per PC.

### `New-LocalUser : Access denied.`

**Cause:** the PowerShell window is not **elevated**. Being logged in *as* an
administrator is not the same thing — UAC still requires an explicitly elevated
prompt.

**Tells:** an elevated window's title bar reads `Administrator: Windows
PowerShell`, and it opens in `C:\Windows\system32` rather than
`C:\Users\<you>`. If your prompt shows your home folder, it is not elevated.

**Fix:** right-click PowerShell → **Run as administrator**. Verify with:

```
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

It must print `True`. If it prints `True` and access is *still* denied, confirm
the account is in the local admins group with `net localgroup Administrators`;
on a domain-joined station, policy can also block local account creation.

### "This task requires that the user account specified has Log on as batch job rights."

**Cause:** saving a task set to **Run whether user is logged on or not** starts
it as a *batch logon*, and by default only Administrators and Backup Operators
hold that right. Task Scheduler normally grants it automatically, but cannot
when Group Policy manages it.

**Affects:** the `cite-cli renew` task only. `cite-cli sync` uses an
*interactive* logon, and `cite-cli clean` runs as `Admin`, who already has the
right.

**Fix:** `secpol.msc` → **Local Policies** → **User Rights Assignment** →
**Log on as a batch job** → **Add User or Group…** → `cite-automation`. Then
save the task again (it re-asks for the password). This is Phase 1's grant step.

ℹ️ On Windows **Home** there is no `secpol.msc` — add `cite-automation` to the
local **Backup Operators** group instead, which already holds the right.

### The `netplwiz` auto-login checkbox is missing

**Cause:** current Windows 10/11 hide it by default.

**Fix:** run the Phase 5 `DevicePasswordLessBuildVersion` command, then close
and reopen `netplwiz`. If it is still absent (some domain-joined
configurations), use [Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon)
instead — equally secure, same underlying mechanism.

### The station stops at the BitLocker PIN screen after a reboot

**Cause:** BitLocker in **TPM + PIN** mode asks for a PIN before Windows starts
loading. Auto-login only answers the *Windows* sign-in screen and cannot help.

**Fix:** there is no software workaround — either move that station to TPM-only
unlock, or accept that `cite sync` runs only when someone is physically present.
Check the mode with `manage-bde -status C:` (Phase 0).

### No emails ever arrive

**Cause:** almost always the alert variables were set with plain `setx` (current
account only) instead of `setx /M` (machine-wide). Alerting then **silently
no-ops** — there is no error in any log.

**Fix:** redo Phase 2 with `/M` from an elevated prompt, then verify from
**both** accounts:

```
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert
```

An email must arrive under `Admin` *and* under `cite-automation`.

### `uvx` is not recognised under one of the accounts

**Cause:** `uv` installs per profile. It was installed for one account only.

**Fix:** log in as the account that is missing it and install `uv` again
(Phase 3). Each account has its own `uv.exe` path — give every task **its own
account's** path, never the other's.

### A task shows Last Run Result `0x1`

The command ran and failed. Open that account's log — `renew`/`sync` under
`C:\Users\cite-automation\.cite\logs\`, `clean` under
`C:\Users\Admin\.cite\logs\` — and read the last entries in `cite.log`. A
failure email should also have been sent; if not, see "No emails ever arrive".

### The station is on the desktop, not the lock screen, after a reboot

**Cause:** the `cite-cli lock-on-logon` task did not fire, or fired before the
session was ready.

**Fix:** confirm the task exists with **both** triggers (5 s and 15 s) and that
"Run with highest privileges" is **un**checked. Raise both delays if the station
is slow. The Phase 6 screen-saver setting is the backstop — verify **On resume,
display logon screen** is checked.

---

## Undoing it

**Disable auto-login:** `netplwiz` → re-check **Users must enter a user name and
password to use this computer**. (Or run Sysinternals Autologon and click
**Disable**.)

**Remove the tasks:** delete `cite-cli clean`, `cite-cli renew`, `cite-cli sync`
and `cite-cli lock-on-logon` from Task Scheduler.

**Remove the account:** `Remove-LocalUser -Name "cite-automation"` from an
elevated PowerShell. Its profile folder under `C:\Users\` must be deleted
separately if you want the logs and state gone too.
