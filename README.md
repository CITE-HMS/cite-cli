# cite-cli

[![CI](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml)

Command line tools for CITE@HMS.

---

## Part 1 — Task Scheduler Setup

This section covers everything needed to schedule `cite` commands unattended on Windows. Each subsection describes what the command does, how to configure email alerts, and the exact Task Scheduler arguments to use.

### Common prerequisites

1. Install `uv`: <https://docs.astral.sh/uv/getting-started/installation/>. Note where `uv.exe` lands (e.g. `C:\Users\User\.local\bin\uv.exe`).
2. Install `git`: `git --version`. If missing: <https://git-scm.com/install/>.

All tasks use **Start a program** in Task Scheduler with:

- **Program/script**: `C:\Windows\System32\cmd.exe`
- **Add arguments**: see each subsection below.

If the path to `uv.exe` contains spaces, wrap it in an extra pair of double quotes:

```bat
/c ""C:\Users\My User\.local\bin\uv.exe" tool run ..."
```

---

### Email alerts on failure

`cite clean`, `cite renew`, and `cite apply-update` all send a failure email when they exit non-zero or raise an uncaught exception. Configure this once per Windows user account; every scheduled task on that account picks it up automatically. If the env vars are absent, alerting silently no-ops.

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

**Verify with a test email:**

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite test-alert
```

If configured correctly, you'll see `Test alert sent to ...` and an email arrives within seconds. The subject will read `[cite-cli] test-alert failed on <hostname>` — the word "failed" is intentional, it uses the same template as real failures. If it fails, the command prints the most common causes (wrong App Password, 2FA not enabled, port 587 blocked).

---

### `cite clean` — delete old files on a schedule

Deletes files older than N days from one or more directories. When no directory is given, it cleans all default paths found on the machine (`D:/User_Data`, `E:/User_Data`, etc.). Sends a failure alert email if it crashes.

**Task Scheduler arguments** (runs daily, logs to `C:\cite_clean_log.log`):

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite clean -d 25 -f > C:\cite_clean_log.log 2>&1"
```

- `-d 25` — delete files older than 25 days (adjust as needed).
- `-f` — skip the confirmation prompt (required for unattended runs).

To clean a specific directory instead of the defaults, add the path as the first argument:

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite clean D:\MyData -d 30 -f > C:\cite_clean_log.log 2>&1"
```

---

### `cite renew` — auto-submit the Nikon license renewal form

Submits the NIS-Elements Time-DEMO license renewal form to Nikon when the license is within 14 days of expiring. Fully unattended:

- Reads the expiration date live from the local Sentinel HASP dongle via ACC at `http://localhost:1947`.
- Auto-generates the `.c2l` request file by running `nis_hasp_update.exe -r` (discovered under `C:\Program Files\NIS-Elements*\HASP\`).
- Appends the HASP ID (e.g. `09882A98`) to the submission note so Nikon's staff can identify the dongle.
- **Idempotent**: once submitted for a given expiration date, won't re-submit until Nikon's updated `.c2v` is applied. State is tracked in `%USERPROFILE%\.cite\renew_state.json`. Safe to schedule daily.
- Sends a failure alert email if it crashes (requires [email alert setup](#email-alerts-on-failure)).

**Task Scheduler arguments** (runs daily, logs to `C:\cite_renew_log.log`):

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite renew --email you@example.com --full-name "Your Name" --url nikon > C:\cite_renew_log.log 2>&1"
```

On most days, `cite renew` will detect that the license is still outside the renewal window (or already submitted this cycle) and exit cleanly without contacting Nikon.

**Optional overrides:**

- If `nis_hasp_update.exe` is not auto-discovered, set the path explicitly:
  ```powershell
  setx CITE_RUS_EXE "C:\custom\path\to\nis_hasp_update.exe"
  ```
- To supply a pre-generated `.c2l` file instead of auto-generating one:
  ```bat
  ... cite renew --email ... --full-name ... --url nikon --c2l-file C:\path\to\file.c2l
  ```

**Dry-run (no side effects, for smoke-testing):**

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite renew `
    --email you@example.com `
    --full-name "Your Name" `
    --url nikon `
    --force --dry-run
```

`--force` bypasses the renewal-window check; `--dry-run` skips the POST and the `.c2l` generation. Nothing is submitted or written.

---

### `cite apply-update` — auto-apply Nikon's `.l2c` reply

Closes the renewal loop: after `cite renew` submits the request, this command polls the shared Gmail inbox for Nikon's reply, downloads the `.l2c` file, verifies it matches this PC's dongle, and applies it silently via `nis_hasp_update.exe -a`.

**Key behavior:**

- **No-ops cleanly** when there is no pending renewal on this PC (no `~/.cite/renew_state.json` → exits 0 without opening IMAP).
- **Multi-PC-aware**: the same inbox feeds every microscope. Downloads each candidate `.l2c`, parses its HASP ID, and applies only the one matching this PC's dongle. Other PCs' replies are cached in `~/.cite/checked_emails.json` so they are never re-downloaded.
- **URGENT alert** if no matching reply has arrived and the dongle expires in ≤ 4 days. Subject prefixed `[cite-cli] URGENT: ...`.
- **Defense-in-depth**: HASP ID is verified from the filename before applying, and `nis_hasp_update.exe` itself rejects mismatches.

**Prerequisites:** uses the same Gmail App Password set up for [email alerts](#email-alerts-on-failure). `CITE_ALERT_SMTP_USER` / `CITE_ALERT_SMTP_PASSWORD` serve both outbound SMTP and inbound IMAP — no additional env vars needed.

**Task Scheduler arguments** (runs daily, logs to `C:\cite_apply_log.log`):

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite apply-update > C:\cite_apply_log.log 2>&1"
```

Schedule at e.g. 01:00. **Stagger start times across machines** to avoid all PCs hitting Gmail simultaneously:

- **(Recommended)** In the trigger's **Edit** dialog, enable **Delay task for up to (random delay)** and set 1 hour. Task Scheduler picks a different random offset each run.
- Or set a fixed offset per PC (01:00, 01:05, 01:10, …).

**Dry-run (cross-platform, no dongle needed):**

Polls IMAP, downloads candidate `.l2c` files into a temp dir, reports what was found — without invoking `nis_hasp_update.exe`, writing state, or touching the production cache. Useful for verifying the inbox + download pipeline on macOS before deploying to Windows.

```bash
# macOS / Linux — set env vars first
export CITE_ALERT_SMTP_USER="you@gmail.com"
export CITE_ALERT_SMTP_PASSWORD="xxxx xxxx xxxx xxxx"
export CITE_ALERT_TO="you@gmail.com"

uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite apply-update --dry-run
```

Example output:

```text
[2026-05-15 ...] DRY RUN: no renew_state.json on this machine — running in pure-diagnostic mode.
[2026-05-15 ...] Found 2 candidate email(s).
[2026-05-15 ...] Downloading into /var/folders/.../cite-apply-dryrun-XYZ (you can delete this after).

  ┌─ #1
  │  From:     ahus@lim.cz
  │  Sent:     2026-05-15T10:30:00+00:00
  │  Token:    e556d5faf993ece4b7eaaa56fa5be2ad
  │  URL:      https://nis-e-update.nikon-instruments.jp/dealers/download.php?request=...
  │  File:     520D66C9.l2c (20,114 bytes)
  │  HASPID:   1376609993 (hex 520D66C9) [parsed from filename]
  └─

[2026-05-15 ...] DRY RUN complete. Nothing was applied or persisted.
```

If `~/.cite/renew_state.json` exists on the machine, the report also shows `Match: YES` next to the candidate whose HASP ID matches the local dongle.

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
| `--expires` | | _(read from dongle)_ | `CITE_LICENSE_EXPIRES` | License expiration date (`YYYY-MM-DD`). If set, reads from this value instead of the HASP dongle, and skips dedup. |
| `--days-before` | | `14` | | Submit only when the license expires within this many days. |
| `--dry-run` | `-n` | `False` | | Print what would be submitted without making any HTTP request or generating a `.c2l`. |
| `--force` | `-f` | `False` | | Submit even if the license is outside the renewal window or was already submitted this cycle. |

---

### `cite apply-update`

Poll the shared Gmail inbox for Nikon's `.l2c` reply and apply it to the local HASP dongle.

```
cite apply-update [OPTIONS]
```

| Option | Short | Default | Env var | Description |
|---|---|---|---|---|
| `--dry-run` | `-n` | `False` | | Poll IMAP, download candidate `.l2c` files into a temp dir, and print a report — without applying anything, writing state, or touching the production cache. Cross-platform; no dongle required. |

Reads `CITE_ALERT_SMTP_USER` and `CITE_ALERT_SMTP_PASSWORD` for both outbound failure emails and inbound IMAP access.

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

### Global options

| Option | Short | Description |
|---|---|---|
| `--version` | `-v` | Print the installed version and exit. |
| `--help` | | Show help for any command. |
