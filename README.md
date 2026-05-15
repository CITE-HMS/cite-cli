# cite-cli

[![CI](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml)

Command line tools for CITE@HMS.

Available commands:

- [`cite clean`](#cite-clean) — delete files older than N days from one or more directories
- [`cite renew`](#cite-renew) — auto-submit the NIS-Elements Time-DEMO license renewal form to Nikon
- [`cite license`](#cite-license) — quick check of the license expiration date read from the HASP dongle
- [Email alerts on failure](#email-alerts-on-failure) — for `cite clean` and `cite renew`, plus `cite test-alert` to verify SMTP setup

## Prerequisites

1. Find where `uv.exe` is installed (e.g. `C:\Users\User\.local\bin\uv.exe`). If you don't have it, install from <https://docs.astral.sh/uv/getting-started/installation/>.
2. Make sure `git` is installed: `git --version`. If not, install from <https://git-scm.com/install/>.

### Task Scheduler — common setup

All commands below are designed to run unattended from Windows Task Scheduler. Create a new task with action **Start a program** and these settings:

- **Program/script**: `C:\Windows\System32\cmd.exe`
- **Add arguments**: see each command's section.

Replace `<path/to/uv.exe>` with the actual path to `uv.exe` on your system. If the path contains spaces, enclose it in double quotes (e.g., `/c ""C:\Users\My User\.local\bin\uv.exe" tool run ..."`).

## `cite clean`

Deletes files older than N days from one or more directories.

**Task Scheduler arguments:**

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite clean -d 25 -f > C:\cite_clean_log.log 2>&1"
```

## `cite renew`

Submits the NIS-Elements Time-DEMO license renewal form to Nikon when the license is within 14 days of expiring. The current expiration date is read directly from the local Sentinel HASP dongle, so no manual date input is needed.

Idempotent: once a renewal is submitted for a given expiration date, `cite renew` will not re-submit until Nikon's updated `.c2v` file is applied to the dongle (state tracked in `%USERPROFILE%\.cite\renew_state.json`). Safe to schedule daily.

**Task Scheduler arguments:**

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite renew --email you@example.com --full-name "Your Name" --c2l-file C:\path\to\license_request.c2l --url nikon > C:\cite_renew_log.log 2>&1"
```

Schedule daily. On most runs `cite renew` will detect that the license isn't yet within the renewal window (or that the current cycle was already submitted) and exit cleanly without contacting Nikon.

## `cite license`

Quick check of what the local HASP dongle reports. Useful to verify the dongle is reachable and that `cite renew` would see the expected expiration date, before scheduling anything:

```powershell
<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite license
```

Example output:

```text
[2026-05-14 ...] License expires 2026-06-05 (22 days left).
HASP ID: 159918744
```

Pass `--raw` to dump the unfiltered ACC features feed (useful when troubleshooting why an expiration date didn't parse).

## Email alerts on failure

Both `cite clean` and `cite renew` send an email when they fail (non-zero exit / uncaught exception). Configuration is one-time per Windows user account; once the env vars are set, every existing and future scheduled task picks it up automatically. If the env vars are unset, alerting silently no-ops — dev runs are unaffected.

### One-time alert setup (PowerShell)

1. Generate a **Gmail App Password** at <https://myaccount.google.com/apppasswords> (requires 2-Step Verification on your Google account). Label it "cite-cli". Copy the 16-character string Google shows — that's what you'll set as the password. **Do not use your real Gmail login password**; Google rejects SMTP login with it.

2. Set the alert env vars:

   ```powershell
   setx CITE_ALERT_SMTP_USER     "you@gmail.com"
   setx CITE_ALERT_SMTP_PASSWORD "xxxx xxxx xxxx xxxx"
   setx CITE_ALERT_TO            "you@gmail.com"
   ```

3. Close and reopen PowerShell, then verify:

   ```powershell
   echo $env:CITE_ALERT_SMTP_USER
   ```

That's it. The next time `cite clean` or `cite renew` fails on this machine, you'll receive an email with the command name, hostname, error message, and traceback.

### Verify it works — `cite test-alert`

Sends a one-off test email right now (no real failure needed). Use this immediately after running `setx` to confirm the App Password and recipient address are correct:

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert
```

If everything's wired up, you'll see `Test alert sent to ...` and an email lands in your inbox within seconds. If not, the command prints common causes (wrong App Password, 2FA not enabled, firewall blocking port 587, etc.) and exits 1.

To use a non-Gmail SMTP server, additionally set `CITE_ALERT_SMTP_HOST` (default `smtp.gmail.com`) and `CITE_ALERT_SMTP_PORT` (default `587`, STARTTLS).
