# cite-cli

[![CI](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml)

Command line tools for CITE@HMS.

---

## Part 1 — Task Scheduler Setup

This section covers everything needed to schedule `cite` commands unattended on Windows. Each subsection describes what the command does, how to configure email alerts, and the exact Task Scheduler arguments to use.

**Two commands are intended to be scheduled** in a typical deployment:

| Task | Purpose |
|---|---|
| `cite clean` | Delete old files on a schedule |
| `cite renew` | Monitor the license, submit renewal requests to Nikon, and detect applied renewals (confirmation email + Google-Calendar reminders) |

Applying Nikon's reply is a **manual step**: download the `.l2c` from the link in Nikon's email and apply it on the station via the HASP Update GUI (`nis_hasp_update.exe`). The next daily `cite renew` run detects the new expiration date automatically.

### Common prerequisites

1. Install `uv`: <https://docs.astral.sh/uv/getting-started/installation/>. Note where `uv.exe` lands (e.g. `C:\Users\User\.local\bin\uv.exe`).
2. Install `git`: `git --version`. If missing: <https://git-scm.com/install/>.

All tasks use **Start a program** in Task Scheduler with:

- **Program/script**: `C:\Windows\System32\cmd.exe`
- **Add arguments**: see each subsection below.

### Why `--refresh`?

All Task Scheduler arguments below include `--refresh` on the `uv tool run` line. This tells `uv` to re-fetch the latest commit from GitHub on every invocation, instead of using its cached build. Tradeoff: ~one extra small fetch per machine per day (well under a second on a normal network); benefit: any bug fix or feature you push gets picked up automatically on every machine the next time the task fires — no manual cache invalidation, no logging into each PC. If you ever want to pin to a specific tested version, remove `--refresh` and append `@v1.2.3` (or a commit SHA) to the repo URL: `git+https://github.com/CITE-HMS/cite-cli@v1.2.3`.

---

### Logging

Every `cite` command automatically writes its full output to a rotating log file at `%USERPROFILE%\.cite\logs\cite.log` (1 MB × 5 backups). You never need to redirect output yourself for day-to-day viewing — run `cite log` to open that folder.

The Task Scheduler arguments below still include a small `> bootstrap.log 2>&1` redirect. This covers the rare case where `uvx` itself fails before Python starts (e.g. GitHub unreachable, dependency conflict) — no Python code runs in that case, so the internal logger never gets a chance. The bootstrap file lives in the same `.cite\logs\` folder.

---

### Skipping when NIS-Elements is open

The scheduled tasks check whether `nis_ar.exe` is running before doing anything. If NIS-Elements is open, the task exits immediately without touching the dongle or the log file. This prevents license operations from interfering with an active microscopy session.

The check uses a single `||` idiom in the arguments line:

```bat
tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run ...
```

`tasklist` outputs all running processes; `findstr /I nis_ar.exe` exits 0 if found, non-zero if not. `||` runs the right-hand side only on failure — i.e. only when NIS-Elements is **not** running. The check itself is silent (`> nul 2>&1`). Each Task Scheduler arguments block below already includes this guard.

> **Why not `tasklist /FI "IMAGENAME eq nis_ar.exe"`?** That form requires inner double quotes which conflict with the outer `/c "..."` wrapping in Task Scheduler's arguments field, breaking the command silently. The plain `tasklist | findstr` form avoids all quoting issues.

---

### Email alerts on failure

`cite clean`, `cite renew`, and `cite notify-renewal` all send a failure email when they exit non-zero or raise an uncaught exception. Configure this once per Windows user account; every scheduled task on that account picks it up automatically. If the env vars are absent, alerting silently no-ops.

**One-time setup (PowerShell):**

1. Generate a **Gmail App Password** at <https://myaccount.google.com/apppasswords> (requires 2-Step Verification). Label it "cite-cli". Use the 16-character string as the password — **not** your real Gmail password.

2. Set the env vars:

   ```powershell
   setx CITE_ALERT_SMTP_USER     "you@gmail.com"
   setx CITE_ALERT_SMTP_PASSWORD "xxxx xxxx xxxx xxxx"
   setx CITE_ALERT_TO            "you@gmail.com"
   ```

3. Close and reopen PowerShell, then verify:

   ```powershell
   echo $env:CITE_ALERT_SMTP_USER
   ```

To use a non-Gmail SMTP server, also set `CITE_ALERT_SMTP_HOST` (default `smtp.gmail.com`) and `CITE_ALERT_SMTP_PORT` (default `587`, STARTTLS).

**Station names in email subjects:**

When the HASP ID is recognised, email subjects and bodies include the station name instead of (or in addition to) the hostname. The mapping is defined in `src/cite/_renew.py` (`HASP_ID_TO_STATIONS_MAP`). For example, a renewal on a dongle with HASP ID `09882A98` will produce:

```
Subject: [cite-cli] NIS-Elements license renewed on Station 2
Body:    Station:     Station 2
```

If the HASP ID is not in the map, the subject falls back to the machine hostname.

**Verify with a test email:**

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert
```

If configured correctly, you'll see `Test alert sent to ...` and an email arrives within seconds. The subject will read `[cite-cli] test-alert failed on <hostname>` — the word "failed" is intentional, it uses the same template as real failures. If it fails, the command prints the most common causes (wrong App Password, 2FA not enabled, port 587 blocked).

---

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

---

### `cite renew` — monitor, submit, and detect renewals in one daily command

Runs the renewal loop daily. **One Task Scheduler entry per machine** covers everything:

**Step 1 — detect a completed renewal:** if the dongle's expiration advanced since the last recorded baseline (i.e. someone applied Nikon's update manually via the HASP Update GUI), it sends the confirmation email (`[cite-cli] NIS-Elements license renewed on <Station>`) **and one Google-Calendar invite** for a recurring all-day reminder series. Its three weekly occurrences fall 14 days before, 7 days before, and on the new expiration date. The invite is emailed to `CITE_ALERT_TO`; Google Calendar adds recognized invitations according to that account's **Add invitations to my calendar** setting. Any stale pending-submission state is cleared.

**Step 2 — submit:** reads the dongle's expiration via ACC, checks the renewal window (default 14 days), and submits a fresh `.c2l` to Nikon if needed. While a submission is pending and the license is within 4 days of expiry, sends an URGENT reminder email (throttled to one per 20 h) to apply Nikon's reply manually.

**The apply step is manual by design:** when Nikon's reply arrives in the shared inbox, download the `.l2c` from the `dealers/download.php?request=...` link (each link is one-time!) and apply it on the matching station via the HASP Update GUI (`nis_hasp_update.exe`). The filename (`<HASPHEX>.l2c`) tells you which station it belongs to — see `HASP_ID_TO_STATIONS_MAP` in `src/cite/_renew.py`. The next daily run picks up the new expiry and handles the notifications.

**Details:**

- Expiration is read live from the local Sentinel HASP dongle via ACC at `http://localhost:1947`.
- The `.c2l` is auto-generated by running `nis_hasp_update.exe -r` (discovered under `C:\Program Files\NIS-Elements*\HASP\`).
- The submission note includes the HASP ID (e.g. `09882A98`) so Nikon's staff can identify the dongle.
- **Idempotent**: once submitted for a given expiration date, won't re-submit until the renewal is applied (state in `%USERPROFILE%\.cite\renew_state.json`). Safe to schedule daily.
- The renewal-detection baseline (`%USERPROFILE%\.cite\last_notified_renewal.json`) auto-seeds on the first run of a fresh machine — no setup step needed.

**Task Scheduler arguments** (runs daily):

```bat
/c "tasklist | findstr /I nis_ar.exe > nul 2>&1 || "<path/to/uv.exe>" tool run --refresh --from git+https://github.com/CITE-HMS/cite-cli cite renew --email you@example.com --full-name "Your Name" --url nikon > "%USERPROFILE%\.cite\logs\bootstrap.log" 2>&1"
```

Schedule at e.g. 01:00 (a random delay per machine is still a good idea to spread the load).

On most days both steps exit cleanly — no renewal detected, license not yet within the 14-day window — net effect: a quick log line and exit 0.

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

---

### `cite notify-renewal` — manual renewal check (optional)

Runs the same renewal-detection check as `cite renew` step 1 (confirmation email + calendar invites when the expiry advanced), as a standalone command. **You do not need to schedule this if `cite renew` is scheduled** — the check runs there daily. It exists for manual/one-off use, e.g. right after applying an update by hand when you don't want to wait for the next scheduled run.

**No duplicate emails:** the check only fires when the current expiry is *newer* than what's recorded in `%USERPROFILE%\.cite\last_notified_renewal.json`; once notified, re-running is a no-op. The baseline auto-seeds on first run; `--seed` re-baselines explicitly without sending an email.

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite notify-renewal
```

---

## Part 2 — CLI Reference

All commands follow the pattern:

```
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite <command> [options]
```

Or, if installed locally:

```
cite <command> [options]
```

---

### `cite clean`

Delete files older than N days from a directory (or all default paths if none is given).

```
cite clean [DIRECTORY] [OPTIONS]
```

| Argument / Option | Short | Default | Env var | Description |
|---|---|---|---|---|
| `DIRECTORY` | | _(none)_ | | Directory to clean. Accepts a local path or `smb://` URL. If omitted, all default paths found on the machine are cleaned. For SMB: username defaults to `Admin`; set password via `CITE_PASSWORD`. |
| `--days` | `-d` | `30` | | Delete files older than this many days. |
| `--dry-run` | `-n` | `False` | | Print what would be deleted without deleting anything. |
| `--force` | `-f` | `False` | | Delete without asking for confirmation. Required for unattended runs. |
| `--delete-empty-dirs` | | `True` | | Also remove empty directories after file deletion. |
| `--skip` | | `"delete"` | | Skip files whose path contains this string. |

---

### `cite renew`

Submit the NIS-Elements Time-DEMO license renewal form to Nikon when the license is within the renewal window.

```
cite renew --email EMAIL --full-name NAME --url TARGET [OPTIONS]
```

| Option | Short | Default | Env var | Description |
|---|---|---|---|---|
| `--email` | | _(required)_ | `CITE_LICENSE_EMAIL` | Email address to put in the renewal form. |
| `--full-name` | | _(required)_ | `CITE_LICENSE_FULL_NAME` | Full name to put in the renewal form. |
| `--url` | | _(required)_ | `CITE_LICENSE_URL` | Renewal target: `nikon` (real endpoint) or `test` (local mock at `http://127.0.0.1:8765/`). |
| `--c2l-file` | | _(auto-generate)_ | `CITE_LICENSE_C2L_FILE` | Path to a pre-generated `.c2l` file. If omitted, generates one via `nis_hasp_update.exe`. Use `mock` to use the bundled test file (for `--url test`). |
| `--note` | | `"CITE @ Harvard Medical School"` | `CITE_LICENSE_NOTE` | Free-text note included with the submission. The HASP ID is always appended automatically. |
| `--days-before` | | `14` | | Submit only when the license expires within this many days. |
| `--dry-run` | `-n` | `False` | | Print what would be submitted without making any HTTP request or generating a `.c2l`. |
| `--force` | `-f` | `False` | | Submit even if the license is outside the renewal window or was already submitted this cycle. |

---

### `cite notify-renewal`

Run the renewal-detection check (confirmation email + Google-Calendar reminder invites when the dongle's expiration has advanced since the last notification). Idempotent — re-running with no change is a no-op. The same check runs automatically on every `cite renew`.

```
cite notify-renewal [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--seed` | `False` | Record the current dongle state as the baseline without sending an email. Only needed to re-baseline explicitly — the baseline auto-seeds on first run. |

**State file:** `%USERPROFILE%\.cite\last_notified_renewal.json` — written atomically; contains `hasp_id`, `expiration_date`, and `notified_at`.

**Edge cases handled automatically:**

- No baseline yet (fresh machine): seeds silently without sending an email.
- HASP ID changed (dongle replaced): updates baseline silently without sending an email.
- SMTP configured but delivery fails: tracking file is **not** updated, so the next run retries.
- SMTP not configured: tracking file advances silently (no email, no error).

---

### `cite license`

Read the license expiration date and HASP ID from the local Sentinel HASP dongle.

```
cite license [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--raw` | `False` | Dump the unfiltered ACC features feed (useful for troubleshooting why a date didn't parse). |

Example output:

```text
[2026-05-14 ...] License expires 2026-06-05 (22 days left).
HASP ID: 159918744
```

---

### `cite request-file`

Manually generate a fresh `.c2l` renewal request file by running `nis_hasp_update.exe -r`.

```
cite request-file [OPTIONS]
```

| Option | Short | Default | Description |
|---|---|---|---|
| `--output` | `-o` | `%USERPROFILE%\.cite\generated_request.c2l` | Where to write the `.c2l` file. |

If `nis_hasp_update.exe` is not auto-discovered, set `CITE_RUS_EXE` to its full path.

---

### `cite notify-renewal` (reference)

See the [full description above](#cite-notify-renewal).

| Option | Default | Description |
|---|---|---|
| `--seed` | `False` | Write the current dongle state as baseline without sending an email. |

---

### `cite test-alert`

Send a one-off test failure email to verify SMTP configuration. No failure is needed.

```
cite test-alert
```

No options. Requires `CITE_ALERT_SMTP_USER`, `CITE_ALERT_SMTP_PASSWORD`, and `CITE_ALERT_TO` to be set. Prints the most common failure causes if sending fails.

---

### `cite update`

Update `cite-cli` itself to the latest version from GitHub.

```
cite update
```

No options.

---

### `cite log`

Open the `~/.cite/logs/` folder in the system file manager (Explorer on Windows, Finder on macOS). The folder contains:

- **`cite.log`** — rotating log of all `cite` command output (1 MB × 5 backups). Written automatically by every command.
- **`bootstrap.log`** — the Task Scheduler redirect target, covering the rare case where `uvx` fails before Python starts.

```
cite log
```

No options.

---

### Global options

| Option | Short | Description |
|---|---|---|
| `--version` | `-v` | Print the installed version and exit. |
| `--help` | | Show help for any command. |
