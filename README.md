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

### The scheduled commands

| Command | Runs as | When | Purpose |
| --- | --- | --- | --- |
| `cite clean` | your admin account | 12:00–1:00 AM | Delete old files |
| `cite renew` | your admin account | 1:15–2:15 AM | Submit renewals, detect applied ones |

Both run headless — whether or not anyone is signed in to the station — as the same admin account. `cite sync`, which applies a pending renewal once Nikon has approved it, is **not** scheduled: it drives a GUI-only dialog in NIS-Elements' License Manager, so it only works in an interactive session, and is run by hand instead — see [Applying a pending renewal](#applying-a-pending-renewal).

### Why the renewal is split in two

`cite renew` does everything that works headlessly: it checks the dongle's expiration via ACC, submits a renewal request to Nikon when one is due, and detects when a previously-submitted one has actually landed. `cite sync` is the one piece that needs a live desktop — it drives NIS-Elements' GUI-only License Manager dialog to apply a submission Nikon has already approved.

`cite renew` tracks pending submissions on its own and alerts by email if one goes 4+ days without a recorded `cite sync` attempt, so a submission nobody has applied yet does not go unnoticed. `cite renew` and `cite sync` share `%USERPROFILE%\.cite\renew_state.json`, so **both must run as the same Windows user** — the admin account both scheduled tasks already use.

### One Windows account

Everything runs as a single admin account — whichever one you set the station up from (`Admin`, or whatever it's called locally). It needs admin rights anyway, since `cite clean` deletes other users' acquisition data; `cite renew` and `cite sync` don't need the privilege, they just run as the same account for simplicity and because they share state.

### Applying a pending renewal

When Nikon replies to a submitted renewal, sign in to that admin account on the station — an ordinary interactive sign-in — and run:

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite sync
```

`cite renew` keeps checking for it automatically afterward and sends the confirmation email once ACC shows the new expiration date.

### Requirements

1. Windows 10/11.
2. [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — installed for the admin account. Note where `uv.exe` lands (e.g. `C:\Users\Admin\.local\bin\uv.exe`).
3. [`git`](https://git-scm.com/install/) — check with `git --version`. `uv tool run --from git+…` needs it.
4. Make sure the NIS Elements `licmgr_s.exe` file exists under the Public Windows User in a folder named `NIS_Elements` — `cite sync` needs it.

---

## 2. Station setup with the script

[`scripts/setup-station.ps1`](./scripts/setup-station.ps1) does the whole setup in one shot, as whichever admin account you run it from: sets the alert variables machine-wide, installs `uv` and creates the log folder, and registers the two scheduled tasks (`clean`, `renew`).

> **Station already has the old `cite-automation` setup?** Run [`scripts/cleanup-station.ps1`](./scripts/cleanup-station.ps1) first. It removes the `cite-cli sync` and `cite-cli lock-on-logon` tasks, disables auto-login, removes the lock watchdog, and removes the `cite-automation` account — without touching `cite-cli clean` or the alert variables. Then run the setup script below, signed in as whichever admin account should own both tasks from now on.
>
> ```powershell
> irm https://raw.githubusercontent.com/CITE-HMS/cite-cli/main/scripts/cleanup-station.ps1 | iex
> ```

### Before you start

Have these two ready — [doing it by hand](#3-station-setup-by-hand) needs both every time. The script needs your admin account's password on every run (Windows can check a password you type but never hand back the real one, so there's no way to skip re-typing it — it's needed even though you're already signed in as that account, because "run whether logged on or not" tasks always store it). The Gmail App Password is the exception: the script only asks for it if one isn't already set machine-wide, or if you choose to replace it — so keep it ready for the first run, or any run where you want to rotate it.

| Value | How to get it |
| --- | --- |
| **Gmail App Password** | Generate at <https://myaccount.google.com/apppasswords>, label it "cite-cli" |
| Admin account password | The one you logged in with |
| `licmgr_s.exe` | Make sure it exists under the Public Windows User in a folder named `NIS_Elements` |

The Gmail App Password is a 16-character string, and it requires 2-Step Verification to be enabled on that account. It is what lets a station email you when a renewal lands or a command fails.

Google displays it as four groups of four (`abcd efgh ijkl mnop`). Those spaces are only for readability and are **not** part of the password — at the script's prompt, type or paste it either way, it strips them.

### Run it

- [ ] Log in as the admin account that should own both tasks.
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

Either way it asks for your admin account's password every time, and the Gmail App Password only if one isn't already set or you choose to replace it, then prints what it did.

**Re-running is safe.** Tasks are replaced, so a run that failed halfway is fixed by running it again. A task of the same name is replaced **without asking** (task names are unique, so there is no "keep both"); each line says which happened — `created` or `replaced the existing one`.

### What it does

The three steps it prints, in order:

1. Sets some needed `VARIABLES` machine-wide.
2. Installs `uv` and creates `%USERPROFILE%\.cite\logs`.
3. Registers the two scheduled tasks (`clean`, `renew`), both as this account, and reports each one.

Re-running it on a station that is already set up is safe and is the normal way to
apply an update: tasks are replaced in place.

If a station still has old `cite-cli sync` or `cite-cli lock-on-logon` tasks — left over from the old `cite-automation` setup — the script warns about them as leftover tasks rather than touching them; run [`cleanup-station.ps1`](./scripts/cleanup-station.ps1) to remove them.

If one station needs different values — another retention window, a different email — edit the constants at the top of the script before running it:

```powershell
$Email      = 'email.@email.com'
$FullName   = 'Name'
$CleanDays  = 25
$RepoUrl    = 'git+https://github.com/CITE-HMS/cite-cli'
```

### Verify the station

This part is yours, whichever way you set the station up.

- [ ] Run each task in Task Scheduler → **Run** (`renew` first, `clean` after).
- [ ] Check the logs: `C:\Users\<admin account>\.cite\logs\`.
- [ ] Confirm the license reads correctly:

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite license
```

It should print an expiration date and a HASP ID.

- [ ] Confirm an alert email arrives:

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert
```

- [ ] Confirm `cite sync` runs cleanly (it needs `licmgr_s.exe`; see [Requirements](#requirements)):

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite sync
```

- [ ] The next morning, confirm `cite renew` ran overnight: check `C:\Users\<admin account>\.cite\logs\cite.log` and its Last Run Result in Task Scheduler.

---

## 3. Station setup by hand

The same result, phase by phase. Use this when a station is unusual, when the script fails, or to understand what it did. Follow the phases in order — several steps depend on earlier ones. Collect the two values in [Before you start](#before-you-start) first, and finish with [Verify the station](#verify-the-station).

### Phase 1 — Set the email alert variables (machine-wide)

Log in as the admin account that should own both tasks. Open PowerShell **as administrator** — right-click the PowerShell icon → **Run as administrator**.

Being logged in *as* an administrator is **not** the same as running an *elevated* prompt. Without elevation, registering a "Run whether user is logged on or not" task later fails. Two quick tells: an elevated window's title bar reads `Administrator: Windows PowerShell`, and it opens in `C:\Windows\system32` rather than your home folder. To be certain:

```powershell
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

It must print `True` before you continue.

- [ ] Set all three with `/M` so the value survives regardless of which account ends up running these tasks:

```powershell
setx /M CITE_ALERT_SMTP_USER "email@email.com"
setx /M CITE_ALERT_SMTP_PASSWORD "xxxx xxxx xxxx xxxx"
setx /M CITE_ALERT_TO "email@email.com"
```

**The `/M` is the whole point.** Plain `setx` writes to the current account only, and `clean`/`renew` run whether or not you are signed in when they fire — a per-user value can leave them unable to email anything, and it fails **silently**, with no error in any log.

Type the app password without spaces here: `setx` stores exactly what you give it, and the spaces are not part of the password.

- [ ] Close and reopen PowerShell, then verify:

```powershell
echo $env:CITE_ALERT_SMTP_USER
```

### Phase 2 — Install `uv`

- [ ] Install `uv`: <https://docs.astral.sh/uv/getting-started/installation/>
- [ ] Get the `uv` path, it will be needed in Phase 3:

```powershell
where.exe uv
```

- [ ] Create the log folder:

```powershell
mkdir "$env:USERPROFILE\.cite\logs" -Force
```

Do not skip the `mkdir` step or the tasks won't run.

- [ ] Install `git` if it is not already present: <https://git-scm.com/install/> (check with `git --version`).

### Phase 3 — Create the two scheduled tasks

Both tasks use the same **Action**: program `C:\Windows\System32\cmd.exe` with the arguments below, and both run as the same account — whichever admin account you are setting up as. Replace `<uv>` with the `uv.exe` path from Phase 2. Neither task needs an interactive session — both are set to **Run whether user is logged on or not**.

Common settings unless stated otherwise:

- **Conditions**: uncheck everything
- **Settings**: **Allow task to be run on demand**, **Stop the task if it runs longer than: 1 hour**, **If the running task does not end when requested, force it to stop**

#### 3a — `cite-cli clean`

- **General**: your admin account · **Run whether user is logged on or not** · **Run with highest privileges**
- **Trigger**: Daily, **12:00:00 AM**, recur every 1 day, **random delay: 1 hour**
- **Settings**: **Stop the task if it runs longer than: 4 hours**
- **Arguments**:

```text
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<uv>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite clean -d 25 -f > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

`-d 25` deletes files older than 25 days; `-f` skips the confirmation prompt (required unattended).

#### 3b — `cite-cli renew`

- **General**: your admin account · **Run whether user is logged on or not** · **Run with highest privileges**
- **Trigger**: Daily, **1:15:00 AM**, recur every 1 day, **random delay: 1 hour**
- **Arguments** (one line — change the name, keep the quotes):

```text
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<uv>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite renew --email citeathms@gmail.com --full-name "Federico Gasparoli" --url nikon > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

Always use <citeathms@gmail.com> as the `--email` value.

Saving a "Run whether user is logged on or not" task asks for that account's password, even though it is the one you are already using — Windows needs it to start the task in the background whether or not you are signed in at the time. Administrators hold **Log on as a batch job** by default, so this should just work; if saving instead fails with *"This task requires that the user account specified has Log on as batch job rights"*, see [Troubleshooting](#7-troubleshooting).

**Resulting schedule:**

| Task | Account | Runs |
| --- | --- | --- |
| `cite clean` | your admin account | 12:00–1:00 AM |
| `cite renew` | your admin account | 1:15–2:15 AM |

Neither task ever needs anyone signed in. When Nikon replies to a submitted renewal, sign in to that admin account and run `cite sync` by hand; see [Applying a pending renewal](#applying-a-pending-renewal).

Now go through [Verify the station](#verify-the-station).

### Undoing it

**Remove the tasks:** delete `cite-cli clean` and `cite-cli renew` from Task Scheduler.

If the station still carries the *old* `cite-automation` setup — auto-login, the lock watchdog, and a separate account — use [`cleanup-station.ps1`](./scripts/cleanup-station.ps1) instead of undoing that by hand; it removes all of it, including the account, in one run.

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

All three log under the same folder, since all three run as the same account: `C:\Users\<admin account>\.cite\logs\`.

### Email alerts

`cite clean`, `cite renew`, `cite sync` and `cite notify-renewal` send a failure email when they exit non-zero or raise an uncaught exception. If the environment variables are absent, alerting silently no-ops.

| Variable | Value |
| --- | --- |
| `CITE_ALERT_SMTP_USER` | the Gmail account that sends |
| `CITE_ALERT_SMTP_PASSWORD` | its [App Password](https://myaccount.google.com/apppasswords), **not** the real password |
| `CITE_ALERT_TO` | where alerts go |

On a station set up as described above these are set **machine-wide** (`setx /M`, or the script), so the value survives regardless of which account ends up running these tasks. To use a non-Gmail SMTP server, also set `CITE_ALERT_SMTP_HOST` (default `smtp.gmail.com`) and `CITE_ALERT_SMTP_PORT` (default `587`, STARTTLS).

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
- ⚠️ a renewal is pending and `cite sync` has not run for 4 days — a reminder that nobody has signed in to apply it yet
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

**Step 3 — watch the sync leg:** `cite sync` needs an interactive session, so on a station where it is run by hand (our default — see [Applying a pending renewal](#applying-a-pending-renewal)) rather than scheduled, nothing runs it on its own at all; on a station where it *is* scheduled, Task Scheduler silently skips it whenever nobody is signed in. Either way it cannot report its own failure to apply. While a submission is pending, `renew` checks the synchronization-attempt timestamp instead and, once no attempt has been recorded for 4 days (two missed 2-day retry windows), sends a failure alert naming the likely cause. Throttled to one per 48 h. Because a submission normally lands ~14 days before expiry, this surfaces a stalled sync leg around 10 days out instead of leaving it silent until the 4-day urgency alerts.

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

Configure this task with **Run whether user is logged on or not**. Our default setup runs `cite sync` by hand rather than scheduling it (see [Applying a pending renewal](#applying-a-pending-renewal)) — `renew` still detects and confirms a renewal applied that way on its next run. If a station also schedules `cite sync` (see below), schedule `renew` *after* it (e.g. 01:15 against sync's 01:00) so it can pick up what sync just applied and send the confirmation email the same night. The command line does not need a `--no-sync` flag; headless mode is the default. `--sync` remains available as an explicit legacy override.

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

### `cite sync` — apply a pending renewal

Beginning 48 hours after a real Nikon submission, starts License Manager invisibly and invokes its Synchronize action. It exits successfully without opening License Manager when no synchronization is due. A real attempt records its timestamp so retries occur every two days.

The vendor does not expose Synchronize as a native command-line switch, so this command requires an interactive Windows desktop — it cannot be made to run headlessly. **Our default setup does not schedule it**: sign in to the station's admin account and run it by hand whenever Nikon has replied to a pending renewal (see [Applying a pending renewal](#applying-a-pending-renewal)); `cite renew`'s step 3 above alerts you if a pending submission goes 4+ days without a recorded attempt.

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite sync
```

If a station's routine makes automating it worthwhile instead — someone signs in to it regularly, or a session is kept open for it — it can still be scheduled: **Run only when user is logged on**, same Windows account as `cite renew`, daily trigger with no random delay, scheduled *before* the daily `cite renew` (e.g. 01:00 against renew's 01:15) so a renewal it applies gets picked up and confirmed the same night. Locking the workstation is supported; signing out is not. `cite sync` only sends the confirmation email when its own synchronization advanced ACC; a renewal applied by any other route is reported by `cite renew` instead.

**Task Scheduler arguments**, if scheduled (runs daily, e.g. 01:00):

```bat
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite sync > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

The automatic action uses `%PUBLIC%\NIS_Elements\licmgr_s.exe` by default. Set `CITE_LICENSE_MANAGER_EXE` to a different full path when needed. ACC — not the GUI status — is the success gate. A launch failure, confirmed License Manager failure, or post-sync ACC verification failure exits non-zero and sends the standard failure alert when SMTP alerts are configured. Successful-looking syncs that leave ACC unchanged remain pending; after 6+ days they are escalated as stalled.

A task configured **Run only when user is logged on** does not fail when nobody is signed in — Task Scheduler simply never starts it, so no exit code and no alert are produced. That silent gap is exactly why our default setup runs `cite sync` by hand instead: `cite renew`'s step 3 above is what actually notices a pending renewal going unapplied.

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

### `Access denied` setting variables or registering a task

**Cause:** the PowerShell window is not **elevated**. Being logged in *as* an administrator is not the same thing — UAC still requires an explicitly elevated prompt.

**Tells:** an elevated window's title bar reads `Administrator: Windows PowerShell`, and it opens in `C:\Windows\system32` rather than `C:\Users\<you>`. If your prompt shows your home folder, it is not elevated.

**Fix:** right-click PowerShell → **Run as administrator**. Verify with:

```powershell
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

It must print `True`. If it prints `True` and access is *still* denied, confirm the account is in the local admins group with `net localgroup Administrators`.

### "This task requires that the user account specified has Log on as batch job rights."

**Cause:** saving a task set to **Run whether user is logged on or not** starts it as a *batch logon*, and by default only Administrators and Backup Operators hold that right. Administrators hold it automatically, so this is rare — it means Group Policy manages (and has stripped) that right on a domain-joined station.

**Fix:** `secpol.msc` → **Local Policies** → **User Rights Assignment** → **Log on as a batch job** → **Add User or Group…** → your admin account. Then save the task again (it re-asks for the password).

On Windows **Home** there is no `secpol.msc` — add the account to the local **Backup Operators** group instead, which already holds the right.

### No emails ever arrive

**Cause:** almost always the alert variables were set with plain `setx` (current account only) instead of `setx /M` (machine-wide). Alerting then **silently no-ops** — there is no error in any log.

**Fix:** redo [Phase 1](#phase-1--set-the-email-alert-variables-machine-wide) with `/M` from an elevated prompt, then verify:

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert
```

### `uvx` is not recognised

**Cause:** `uv` installs into the profile of whoever ran the installer. Either it was never installed for this account, or the current PowerShell session hasn't picked up the updated `PATH` yet.

**Fix:** install `uv` for this account if missing ([Phase 2](#phase-2--install-uv)), then close and reopen PowerShell.

### A task shows Last Run Result `0x1`

The command ran and failed. Open `C:\Users\<admin account>\.cite\logs\cite.log` and read the last entries. A failure email should also have been sent; if not, see "No emails ever arrive".
