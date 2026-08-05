# Auto-Update of Element License

This page explains how to configure **Task Scheduler** to run [**cite cli**](https://github.com/CITE-HMS/cite-cli) commands to **semi-automate** the **renewal** of the **Elements** license and to **keep track of the changes** in this [Google Sheet](https://docs.google.com/spreadsheets/d/1AkgeEjVUKQfCCow-crwRODXnvROrxp8GMcWObHJ0Oao/edit?gid=0#gid=0).

**Three commands are scheduled** in a typical deployment, split across **two
Windows accounts**:

- **`cite-automation`** — a dedicated account that runs the two license tasks
  (`renew` and `sync`) and is configured to **log in automatically**. Keeping
  the auto-login credential on a purpose-made account instead of `Admin` limits
  what an exposed session can reach.
- **`Admin`** — keeps running `cite clean`, which needs rights to delete other
  users' acquisition data. It shares no state with the license workflow, so
  there is nothing to gain by moving it.

- [**cite cli**](https://github.com/CITE-HMS/cite-cli) `clean`: delete old files on a schedule. Runs as **`Admin`**.
- [**cite cli**](https://github.com/CITE-HMS/cite-cli) `renew`: the headless daily license command — detects a completed renewal, sends a confirmation email, and submits a fresh `.c2l` to Nikon within the 14-day window. Runs as **`cite-automation`** with **Run whether user is logged on or not**:
    - Expiration is read live from the local Sentinel HASP dongle via ACC at `http://localhost:1947`.
    - Within 14 days of expiry, it generates the `.c2l` file and sends it to Nikon (using the <citeathms@gmail.com> email address).
    - Once the expiration advances, a confirmation email is sent and the [Google Sheet](https://docs.google.com/spreadsheets/d/1AkgeEjVUKQfCCow-crwRODXnvROrxp8GMcWObHJ0Oao/edit?usp=sharing) is updated via the scheduled Apps Script, which also creates Google Calendar reminders on the new expiration date, 1 week before, and 2 weeks before.
    - If Nikon does not confirm within 4 days of expiry, an URGENT reminder email is sent (throttled to one per 20 h).
    - It also **watches the `cite sync` task**: if a renewal is pending and no synchronization has been attempted for 4 days, it emails a warning (throttled to one per 48 h). See the note below on why this matters.
- [**cite cli**](https://github.com/CITE-HMS/cite-cli) `sync`: the interactive synchronization command. Runs as **`cite-automation`** with **Run only when user is logged on**:
    - Beginning 48 hours after submission, it starts License Manager invisibly and invokes its Synchronize action every two days until ACC reports a later expiration.
    - If no Nikon renewal is pending or due, it exits successfully without opening License Manager.
    - The Windows account may be locked, but it must remain logged in because License Manager exposes only a GUI.

⚠️ **IMPORTANT — if nobody is logged on, this task does not fail, it simply
never runs.** Task Scheduler does not start a **Run only when user is logged on**
task when nobody is signed in, so there is no process, no exit code, and **no
failure email**. Without help, the whole synchronization leg would go silently
dead and the license would never be applied. That is why `cite renew` (which
always runs) watches the sync timestamp and emails after 4 days of inactivity —
the warning arrives roughly **10 days before expiry** instead of leaving you with
only the 4-day URGENT notices.

✅ **The fix for the underlying problem** is to configure `cite-automation` to
**log in automatically and lock itself immediately**, so the station is always in
a logged-on state even after a reboot. See
[Auto-login the cite-automation account](#-auto-login-the-cite-automation-account-required-for-cite-sync).
The `renew` watchdog above then becomes the safety net for when that setup
breaks (a changed password, a disabled task), rather than the primary defence.

`cite renew` and `cite sync` must run as the **same Windows user**
(`cite-automation`) because they share `%USERPROFILE%\.cite\renew_state.json`.
That file lives in the running account's own profile, so splitting the two tasks
across accounts would silently break the workflow — each would read a different
file.

**Schedule overview** (details in each section below):

| Task | Account | Start | Random delay | Actually runs | Logged on? |
| --- | --- | --- | --- | --- | --- |
| `cite clean` | `Admin` | 12:00 AM | up to 1 h | 12:00–1:00 AM | not required |
| `cite sync` | `cite-automation` | 1:00 AM | **none** | 1:00 AM | **required** |
| `cite renew` | `cite-automation` | 1:15 AM | up to 1 h | 1:15–2:15 AM | not required |

`cite sync` runs **before** `cite renew` on purpose, and gets no random delay so
that ordering always holds. `cite clean` is independent of the license workflow —
it may overlap the others harmlessly.

[**cite cli**](https://github.com/CITE-HMS/cite-cli) `notify-renewal` is also available as a standalone command if you want to trigger the renewal-detection check manually (e.g., right after applying an update by hand). **You do not need to schedule this if `cite renew` is already scheduled.**

# Prerequisites

1. Install `uv`: <https://docs.astral.sh/uv/getting-started/installation/> and note where `uv.exe` lands. You can run `where.exe uv` in PowerShell to see the path (e.g. `C:\Users\User\.local\bin\uv.exe`).
2. Install `git`: <https://git-scm.com/install/>. You can check if it installed with `git --version` in PowerShell.

⚠️ NOTE: `uv` installs into the **profile of the account that runs the
installer** (`C:\Users\<account>\.local\bin\uv.exe`). Since the tasks are split
across two accounts, install it **once per account** — as `Admin` for `cite
clean`, and as `cite-automation` for `cite renew` / `cite sync` — and use each
account's own path in its task definitions. Run `where.exe uv` while logged in
as that account to get the right one.

# Why `--refresh`?

All Task Scheduler arguments below include `--refresh` on the `uv tool run` line. This tells `uv` to re-fetch the latest commit from GitHub on every invocation, instead of using its cached build. Any bug fix or feature pushed to GitHub gets picked up automatically on every machine the next time the task fires — no manual cache invalidation, no logging into each PC.

# Email alerts on failure

The [**cite cli**](https://github.com/CITE-HMS/cite-cli) sends **emails** when a command exits with an error. If the env vars are absent, alerting **silently no-ops** — no error, no warning, just permanent silence.

⚠️ **IMPORTANT — plain `setx` is per-user.** It writes to that account's own
environment only. Because the tasks are split across **two** accounts
(`Admin` for `clean`, `cite-automation` for `renew`/`sync`), setting the
variables under one account leaves the other one silently unable to email
anything — including the sync watchdog. Use **`setx /M`**, which writes them
machine-wide so both accounts inherit them.

**One-time setup — run in an *elevated* (Run as administrator) PowerShell:**

1. Generate a **Gmail App Password** at <https://myaccount.google.com/apppasswords> (requires 2-Step Verification). Label it "cite-cli". Use the 16-character string as the password — **not** your real Gmail password.

2. Set the env vars **machine-wide** (note the `/M`):

```
setx /M CITE_ALERT_SMTP_USER "citeathms@gmail.com"
setx /M CITE_ALERT_SMTP_PASSWORD "xxxx xxxx xxxx xxxx"
setx /M CITE_ALERT_TO "citeathms@gmail.com"
```

⚠️ NOTE: Close and reopen PowerShell and run `echo $env:CITE_ALERT_SMTP_USER` to verify.

⚠️ NOTE: verify it from **both** accounts. Log in as `cite-automation` and run
`uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert`, then do
the same as `Admin`. An email must arrive both times. This is the single easiest
thing to get wrong in the two-account setup, and it fails silently.

ℹ️ NOTE: the same applies to any other `CITE_*` variable you set —
`CITE_RUS_EXE`, `CITE_LICENSE_MANAGER_EXE`, `CITE_PASSWORD`. Use `/M` for those
too.

ℹ️ NOTE: the **Gmail App Password** `CITE_ALERT_SMTP_PASSWORD` is generated with the <citeathms@gmail.com> account at <https://myaccount.google.com/apppasswords> (requires 2-Step Verification).

ℹ️ NOTE: to test if the alerts work, run in PowerShell: `uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert`

# Emails you can receive

All emails go to `CITE_ALERT_TO`. Subjects use the **station name** when the HASP ID is recognised, otherwise the machine hostname.

**✅ Success — the only "good news" email:**

| Subject | Sent by | When |
| --- | --- | --- |
| `[cite-cli] NIS-Elements license renewed on <Station>` | `renew`, `sync`, `notify-renewal` | ACC reports a **later** expiration date than the last recorded one. This is the email the Apps Script consumes to update the Google Sheet and create the Calendar reminders. |

Note that submitting to Nikon does **not** send an email. Only a confirmed, ACC-verified date change does — a successful License Manager dialog is never treated as proof on its own.

**⚠️ Warning — renewal is late:**

| Subject | Sent by | When |
| --- | --- | --- |
| `[cite-cli] URGENT: NIS-Elements license expiring in N day(s), no Nikon reply yet on <Station>` | `renew` | A submission is pending and expiry is ≤ 4 days away. Throttled to one per 20 h. Means: Nikon has not returned the license yet — chase it manually. |
| `[cite-cli] renew synchronization failed on <Station>` — body says *"License Manager synchronization has never run"* | `renew` | A renewal is pending but `cite sync` has not attempted a synchronization in **4 days**. Throttled to one per 48 h. Means: **`cite-automation` is probably not logged on** (auto-login broken?), or the `cite-cli sync` task is disabled. Log in and the task catches up on its own. |

**❌ Failure — something broke:**

| Subject | Sent by | When |
| --- | --- | --- |
| `[cite-cli] clean failed on <host>` | `clean` | The cleanup command errored out. |
| `[cite-cli] renew failed on <Station>` | `renew` | ACC unreachable, `.c2l` generation failed, or the POST to Nikon failed. |
| `[cite-cli] sync failed on <Station>` | `sync` | License Manager could not start, reported a failure, or ACC could not be re-read afterward. |
| `[cite-cli] renew synchronization failed on <Station>` | `renew --sync` | Same causes, from the legacy combined workflow. |
| `[cite-cli] notify-renewal failed on <host>` | `notify-renewal` | ACC or SMTP error during a manual check. |

**Silence is the normal case.** On a typical day nothing is sent: the license is outside the 14-day window, no synchronization is due, and no date changed.

ℹ️ NOTE: the two `renew` warnings above tell you **different** things. *"synchronization has never run"* means the automation is not even trying — go log in. *"URGENT … no Nikon reply yet"* means it is trying but Nikon has not returned the license — chase Nikon. If `cite-automation` is never logged on you will get the first one at roughly 10 days out, then the second from 4 days out.

⚠️ **One failure worth knowing about**: if License Manager keeps reporting *success* but ACC never shows a new date, nothing is emailed for the first few attempts (this is expected while Nikon processes the request). After **6 days** of that, a `sync failed` email is sent saying the renewal *"may need manual attention"* — that means the automation is stuck and a human should apply the license by hand.

# Task Scheduler Configuration

### ***➡*** ***CITE CLEAN COMMAND***

Create a new **Task** in **Task Scheduler** and set the Tabs as indicated below:

**1 -** **General Tab**

Leave everything as it is but:

- Name: **cite-cli clean**
- **When running the task, use the following user account**: `Admin` — this task keeps its rights to delete other users' acquisition data
- Check: **Run whether user is logged on or not**
- Check: **Run with highest privileges**

**2 - Trigger Tab**

- **Settings:**
    - Begin the task: **On a Schedule**
    - Check: **Daily**
    - Start: on the date you setup the task @ **12:00:00 AM**
    - Recur every **1** days
- **Advanced** **Settings:**
    - Only check:
        - **Stop task if it runs longer than: 1 hour**
        - **Enabled**

**3 - Action Tab**

- Action: **Start a program**
- **Settings:**
    - **Program/script**: `C:\Windows\System32\cmd.exe`
    - **Add arguments (optional)**: `/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite clean -d 25 -f > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"`
        - ⚠️ `"<path/to/uv.exe>"` replace with the `uv.exe` path (you can run `where.exe uv` in PowerShell to see the path, e.g. `C:\Users\User\.local\bin\uv.exe`).
        - ℹ️ `-d 25` deletes files older than 25 days — adjust as needed. `-f` skips the confirmation prompt (required for unattended runs).
        - ℹ️ the line `tasklist | findstr /I nis_ar.exe > nul` ensures the task runs only when Elements is not running (for safety).

**4 - Conditions Tab**

Uncheck Everything

**5 - Settings Tab**

Only check:

- **Allow task to be run on demand**
- **Stop the task if it runs longer than: 1 hour**
- **If the running task does not end when requested, force it to stop**

### ***➡*** ***CITE RENEW COMMAND***

Create a new **Task** in **Task Scheduler** and set the Tabs as indicated below:

**1 -** **General Tab**

Leave everything as it is but:

- Name: **cite-cli renew**
- **When running the task, use the following user account**: `cite-automation`
- Check: **Run whether user is logged on or not**
- Check: **Run with highest privileges**

ℹ️ NOTE: if `cite-automation` is a **standard** user, "Run with highest
privileges" does nothing — there is no administrator token to elevate into. It
is harmless to leave checked, and `renew` needs no privileges anyway: it reads
ACC over `http://localhost:1947`, writes inside its own profile, and POSTs to
Nikon.

**2 - Trigger Tab**

- **Settings:**
    - Begin the task: **On a Schedule**
    - Check: **Daily**
    - Start: on the date you setup the task @ **1:15:00 AM**
    - Recur every **1** days
- **Advanced** **Settings:**
    - Only check:
        - **Delay task for up to (random delay): 1 hour**
        - **Stop task if it runs longer than: 1 hour**
        - **Enabled**

**3 - Action Tab**

- Action: **Start a program**
- **Settings:**
    - **Program/script**: `C:\Windows\System32\cmd.exe`
    - **Add arguments (optional)**: `/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite renew --email citeathms@gmail.com --full-name "Federico Gasparoli" --url nikon > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"`
        - ⚠️ `"<path/to/uv.exe>"` replace with the `uv.exe` path (you can run `where.exe uv` in PowerShell to see the path, e.g. `C:\Users\User\.local\bin\uv.exe`).
        - ⚠️ always use <citeathms@gmail.com> as email
        - ⚠️ you can change the `full-name` flag but keep it between quotes
        - ℹ️ the line `tasklist | findstr /I nis_ar.exe > nul` ensures the task runs only when Elements is not running (for safety).

**4 - Conditions Tab**

Uncheck Everything

**5 - Settings Tab**

Only check:

- **Allow task to be run on demand**
- **Stop the task if it runs longer than: 1 hour**
- **If the running task does not end when requested, force it to stop**

The existing `cite renew` command line does not need a `--no-sync` flag.
Synchronization is disabled by default so existing renewal tasks remain
headless. The optional `--sync` flag restores the old combined behavior, but a
separate `cite sync` task is recommended.

### ***➡*** ***CITE SYNC COMMAND***

Create one additional **Task** in **Task Scheduler** under the same Windows user
as `cite renew`:

**1 - General Tab**

Leave everything as it is but:

- Name: **cite-cli sync**
- **When running the task, use the following user account**: `cite-automation` — must be the same account as `cite-cli renew`
- Check: **Run only when user is logged on**
- Check: **Run with highest privileges**

ℹ️ NOTE: "Run with highest privileges" only has an effect if `cite-automation`
is an administrator. If you kept it a standard user, confirm License Manager
still works with the `cite sync-license` test described in the auto-login
section before relying on this task.

The `cite-automation` session may be locked with `Win+L`, but must not be signed
out. If it is signed out at the scheduled time, the sync is postponed; the
separate `cite renew` task still runs normally.

➡️ To make sure the station is *always* in that logged-on state — including
after a Windows Update reboot or a power cut — set up
[auto-login + auto-lock](#-auto-login-the-cite-automation-account-required-for-cite-sync)
for the `cite-automation` account.

**2 - Trigger Tab**

Add these triggers to this one task:

- **Daily schedule:**
    - Begin the task: **On a Schedule**
    - Check: **Daily**
    - Start: **1:00:00 AM**
    - Recur every **1** day
    - Check: **Enabled**
    - ⚠️ do **not** set a random delay on this task — it must stay ahead of `cite renew`.
- **Optional catch-up trigger:**
    - Begin the task: **At log on**
    - Select the same Windows user used by both tasks
    - Delay task for: **2 minutes**
    - Check: **Enabled**

The command is state-aware, so the additional logon trigger is safe: it exits
without opening License Manager unless a synchronization is actually due.

ℹ️ NOTE: the **1:00 AM** start is deliberately **before** the `cite renew` task
(earliest 1:15 AM). `cite sync` only sends the confirmation email when its own
synchronization advanced the date; a renewal that arrived any other way is
detected and reported by `cite renew`. Running sync first keeps both cases
within the same night. This is also why sync gets no random delay and is capped
at 10 minutes — the two tasks share `renew_state.json`, so they should not run
at the same time.

**3 - Action Tab**

- Action: **Start a program**
- **Settings:**
    - **Program/script**: `C:\Windows\System32\cmd.exe`
    - **Add arguments (optional)**: `/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite sync > "%USERPROFILE%\.cite\logs\bootstrap-sync.log" 2>&1"`
        - ⚠️ `"<path/to/uv.exe>"` replace with the `uv.exe` path (you can run `where.exe uv` in PowerShell to see the path, e.g. `C:\Users\User\.local\bin\uv.exe`).
        - ℹ️ the line `tasklist | findstr /I nis_ar.exe > nul` ensures the task runs only when Elements is not running.

**4 - Conditions Tab**

Uncheck Everything

**5 - Settings Tab**

Only check:

- **Allow task to be run on demand**
- **Run task as soon as possible after a scheduled start is missed**
- **Stop the task if it runs longer than: 10 minutes**
- **If the running task does not end when requested, force it to stop**
- Set **If the task is already running** to **Do not start a new instance**

### ***➡*** ***AUTO-LOGIN THE CITE-AUTOMATION ACCOUNT (required for CITE SYNC)***

`cite sync` only runs while a Windows user is signed in. After a reboot — a
Windows Update, a power cut, someone pressing the button — the station sits at
the sign-on screen and the sync task silently stops running until a human logs
in. Configuring `cite-automation` to **sign in automatically and then lock
immediately** removes that failure mode: the session is always alive for
`cite sync`, but the screen is still locked for anyone walking up to the
microscope.

**1 - Create the `cite-automation` account**

In an **admin** PowerShell (replace the password):

```
New-LocalUser -Name "cite-automation" -Password (Read-Host -AsSecureString "Password") -PasswordNeverExpires -AccountNeverExpires -FullName "CITE automation"
Add-LocalGroupMember -Group "Users" -Member "cite-automation"
```

⚠️ Use `-PasswordNeverExpires`. If the password expires or is changed,
auto-login stops working and the whole sync leg goes quiet until you notice.

⚠️ **Standard user or administrator?** A standard user (the `Users` group above)
is the safer choice and the reason for a separate account at all. But License
Manager may require elevation — **test it before committing**: log in as
`cite-automation`, install `uv`, and run

```
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite sync-license
```

That drives the real License Manager without touching renewal state or sending
any email. If it reports `Completed` with no UAC prompt, keep the account a
standard user. If it hangs or asks for administrator credentials, add it to the
`Administrators` group instead (`Add-LocalGroupMember -Group "Administrators"
-Member "cite-automation"`) and accept that the auto-login credential is an
admin one.

ℹ️ NOTE: `uv` must be installed **for this account** — it lands in that
profile's `.local\bin`. Note the path with `where.exe uv` while logged in as
`cite-automation` and use it in both task definitions.

**2 - Enable auto-login**

There are two safe ways to do this, and they are **equally secure**: both hand
the password to Windows' built-in autologon mechanism, which stores it as an
encrypted **LSA secret**. Use whichever is more convenient.

⚠️ What to avoid is the *third* method found in most web guides: manually
writing `AutoAdminLogon` / `DefaultPassword` into
`HKLM\...\Winlogon` with `regedit`. That one stores the password in the registry
in **plain text**, readable by anything running on the machine.

**Option A — `netplwiz` (built into Windows, simplest — use this one)**

1. Press `Win+R`, type `netplwiz`, press **Enter**.
2. Select **`cite-automation`** in the list.
3. Uncheck **Users must enter a user name and password to use this computer**.
4. Click **Apply**. Windows prompts for that account's password twice — enter it and click **OK**.

⚠️ On recent Windows 10/11 that checkbox is **hidden by default**. Run this once
in an **admin** PowerShell, then close and reopen `netplwiz`:

```
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device" -Name DevicePasswordLessBuildVersion -Value 0
```

**Option B — Sysinternals Autologon (fallback)**

Use this only if the checkbox still does not appear after the tweak above (some
domain-joined configurations hide it), or if you want to script the setup across
many stations at once.

1. Download **Autologon** from <https://learn.microsoft.com/sysinternals/downloads/autologon> and unzip it.
2. Right-click `Autologon64.exe` → **Run as administrator**.
3. Fill in:
    - **Username**: `cite-automation`
    - **Domain**: the **computer name** for a local account (run `hostname` in PowerShell to get it)
    - **Password**: that account's password
4. Click **Enable**, then close.

ℹ️ NOTE: Autologon also accepts the password as a command-line argument for
scripted deployment, but that leaves it in the shell history and any transcript
logs — prefer the GUI unless you are automating many machines.

**3 - Lock the session immediately after login**

Create one more **Task** in **Task Scheduler**:

- **General Tab**
    - Name: **cite-cli lock-on-logon**
    - Check: **Run only when user is logged on**, with **`cite-automation`** as the user
    - ⚠️ do **not** check "Run with highest privileges" — locking needs the plain interactive session
- **Trigger Tab** — add **two** triggers to this one task, so a first attempt that fires too early is caught by the second:
    - **Trigger 1:** Begin the task **At log on** → **Specific user**: `cite-automation` → check **Delay task for: 5 seconds** (type it in — the dropdown only offers 30 s and up) → check **Enabled**
    - **Trigger 2:** identical, but **Delay task for: 15 seconds**
- **Action Tab**
    - Action: **Start a program**
    - **Program/script**: `C:\Windows\System32\rundll32.exe`
    - **Add arguments (optional)**: `user32.dll,LockWorkStation`
        - ⚠️ exactly as written — no space after the comma.
- **Conditions Tab**: uncheck everything
- **Settings Tab**: only **Allow task to be run on demand**

ℹ️ NOTE on the two triggers: if `LockWorkStation` runs before the session has
finished initialising it can silently do nothing — and it still reports success —
which would leave the station **unlocked indefinitely**. The 5-second trigger
keeps the exposure short in the normal case; the 15-second one is the safety net
for a slow boot. Calling `LockWorkStation` on an **already-locked** session is a
harmless no-op, so the second attempt never unlocks anything or causes a prompt.

ℹ️ NOTE on the delay values: during those first seconds the `cite-automation`
desktop is visible to whoever restarted the PC, so shorter is better — but **do
not go below ~3 seconds**. Task Scheduler's logon triggers also carry a few
seconds of their own latency, so `1 second` and `5 seconds` often behave the same
in practice. If a station regularly reaches the 15-second trigger still unlocked,
raise both values rather than chasing it down.

Or create the same task from an **admin** PowerShell:

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

**4 - Add a final safety net (optional but recommended)**

Even two attempts are still "fire and forget" — neither one checks whether the
station actually locked. A screen saver closes that hole for good, because
Windows enforces it continuously rather than once at logon. **Logged in as
`cite-automation`**, run `control desk.cpl,,@screensaver` and set:

- **Screen saver**: `Blank`
- **Wait**: `1` minute
- Check: **On resume, display logon screen**

Now even if both triggers somehow fail, the station locks itself a minute later.

**5 - Verify**

1. **Restart** the PC and do not touch it. It should sign in on its own, show the desktop briefly, and land on the **lock screen** within a few seconds. That is the goal state: *logged on, but locked*. ⚠️ If it is still on the desktop after 15 seconds, both triggers failed — fix that before leaving the station unattended.
2. Sign in and confirm the session is the auto-logged-on one, then lock again with `Win+L`.
3. Leave it locked overnight and the next morning check `%USERPROFILE%\.cite\logs\cite.log` for a `cite sync` entry at ~1:00 AM, and the task's **Last Run Result** (`0x0`).

ℹ️ NOTE: locking is **not** signing out. The `cite-cli sync` task keeps working
on a locked station because the License Manager window is driven through
Windows UI Automation, which does not need the screen to be unlocked. Signing
out **does** stop it.

⚠️ **Things that will break auto-login:**

- **Changing the `cite-automation` password** — redo the `netplwiz` step (or re-run Autologon). This is why the account is created with `-PasswordNeverExpires`.
- **BitLocker with a pre-boot PIN** — auto-login only answers the *Windows* sign-in screen. If the drive is encrypted with BitLocker in **TPM + PIN** mode, a blue screen asks for a PIN *before Windows starts loading at all*, and nothing configured inside Windows can answer it — after a power cut the station waits there forever. Check which mode a station uses in an **admin** PowerShell:

    ```
    manage-bde -status C:
    ```

    Under **Key Protectors**: `TPM` on its own unlocks the drive automatically and is fine; `TPM And PIN` is the problem case. If BitLocker is off entirely, there is nothing to worry about.
- **Signing out manually** (instead of restarting) — auto-login triggers on boot, not on sign-out. If you sign out, sign back in.

⚠️ **Security note:** an auto-login station boots straight into a live session,
and "encrypted LSA secret" means *not casually readable*, **not** protected —
anything running as SYSTEM, or anyone with physical access to the disk, can
recover the stored password. That is precisely why the credential belongs to a
purpose-made **local** `cite-automation` account and never to a domain account
or a shared admin login: it caps what an exposed session is worth. Keep the
room's normal physical access controls in place. This is the usual trade-off for
unattended lab instruments — just make it a deliberate one.

ℹ️ NOTE: once the station auto-logs-in as `cite-automation`, anyone arriving to
use the microscope should **switch users** rather than sign that session out.
Fast User Switching keeps the automation session alive so `cite sync` keeps
working; signing it out stops it until the next reboot.

### ***➡ CITE NOTIFY-RENEWAL (optional)***

`cite notify-renewal` runs the same renewal-detection check as `cite renew` does automatically every day. **You do not need to schedule this if `cite renew` is already scheduled.** It is useful for a manual one-off check right after applying a license update by hand.

To run manually in PowerShell:

`uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite notify-renewal`

⚠️ **Run it as `cite-automation`, not as `Admin`.** It shares
`%USERPROFILE%\.cite\last_notified_renewal.json` with `cite renew` — the file
that records which expiration date has already been reported. Running it under a
different account creates a *second, independent* baseline, so the same renewal
gets emailed twice and the tracking Sheet gets a duplicate row.

If you still want to schedule it, create a new **Task** in **Task Scheduler** and set the Tabs as indicated below:

**1 -** **General Tab**

Leave everything as it is but:

- Name: **cite-cli notify-renewal**
- **When running the task, use the following user account**: `cite-automation` — same account as `cite-cli renew`
- Check: **Run whether user is logged on or not**
- Check: **Run with highest privileges**

**2 - Trigger Tab**

- **Settings:**
    - Begin the task: **On a Schedule**
    - Check: **Daily**
    - Start: on the date you setup the task @ **3:00:00 AM**
    - Recur every **1** days
- **Advanced** **Settings:**
    - Only check:
        - **Delay task for up to (random delay): 1 hour**
        - **Stop task if it runs longer than: 1 hour**
        - **Enabled**

**3 - Action Tab**

- Action: **Start a program**
- **Settings:**
    - **Program/script**: `C:\Windows\System32\cmd.exe`
    - **Add arguments (optional)**: `/c ""<path/to/uv.exe>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite notify-renewal > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"`
        - ⚠️ `"<path/to/uv.exe>"` replace with the `uv.exe` path (you can run `where.exe uv` in PowerShell to see the path, e.g. `C:\Users\User\.local\bin\uv.exe`).

**4 - Conditions Tab**

Uncheck Everything

**5 - Settings Tab**

Only check:

- **Allow task to be run on demand**
- **Stop the task if it runs longer than: 1 hour**
- **If the running task does not end when requested, force it to stop**

# Logging & Testing

⚠️ **`%USERPROFILE%` means there are now TWO log folders**, one per account.
Look in the right one or you will conclude a task never ran:

| Looking for | Log folder |
| --- | --- |
| `cite renew`, `cite sync` | `C:\Users\cite-automation\.cite\logs\` |
| `cite clean` | `C:\Users\Admin\.cite\logs\` |

Each folder holds up to three files:

- **Main CLI log**: records [**cite cli**](https://github.com/CITE-HMS/cite-cli) command output. Written automatically by every command.  
  `%USERPROFILE%\.cite\logs\cite.log`
- **Task Scheduler bootstrap log**: captures early `uvx` failures, such as network or dependency issues that happen before the [**cite cli**](https://github.com/CITE-HMS/cite-cli) logger starts.  
  `%USERPROFILE%\.cite\logs\bootstrap.log`
- **Sync bootstrap log**: the same early failures for the interactive synchronization task (`cite-automation` only).  
  `%USERPROFILE%\.cite\logs\bootstrap-sync.log`

To open the log folder of whichever account you are logged in as, run:
`uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite log`

To test:

1. Click on the newly created `cite-cli renew`, `cite-cli sync`, or `cite-cli clean` task.
2. Click on the ▶️ **Run** button in the **Action** window on the right of the screen.
3. Open that task's account log folder (see the table above) and check the logs to verify that the command has been run.
4. Check the **Last Run Result** column in Task Scheduler: `0x0` means the command succeeded, `0x1` means it failed (and a failure email was sent, if alerts are configured).

Test `cite-cli sync` while `cite-automation` is logged in, then lock the
workstation with `Win+L` and test it again. `cite sync-license` remains available
as a forced manual adapter test; unlike `cite sync`, it ignores pending renewal
state and runs every time.

Stations setup: <https://docs.google.com/document/d/1ArcCG1psGd7FEdmnxpROGGjMFb2PsudCFr5thZ4xMKE/edit?tab=t.0>
