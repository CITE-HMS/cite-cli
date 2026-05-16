# cite-cli

[![CI](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml)

Command line tools for CITE@HMS.

---

## Part 1 — Task Scheduler Setup

This section covers everything needed to schedule `cite` commands unattended on Windows. Each subsection describes what the command does, how to configure email alerts, and the exact Task Scheduler arguments to use.

**Three commands are intended to be scheduled** in a typical deployment:

| Task | Purpose |
|---|---|
| `cite clean` | Delete old files on a schedule |
| `cite renew` | Full renewal cycle (submit + apply) |
| `cite notify-renewal` | Send confirmation email when expiry advances (safety-net for manual applies) |

The `cite apply-update` subsection is documented for advanced use and debugging — it does **not** need its own scheduled task if you already schedule `cite renew`.

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

### Why `--refresh`?

Optional: include in Task Scheduler arguments`--refresh` on the `uv tool run` line. This tells `uv` to re-fetch the latest commit from GitHub on every invocation, instead of using its cached build. Tradeoff: ~one extra small fetch per machine per day (well under a second on a normal network); benefit: any bug fix or feature you push gets picked up automatically on every machine the next time the task fires — no manual cache invalidation, no logging into each PC. For a multi-machine deployment that's the right default. If you ever want to pin to a specific tested version, replace `--refresh` with `git+https://github.com/CITE-HMS/cite-cli@v1.2.3` (or a commit SHA).

---

### Logging

Every `cite` command automatically writes its full output to a rotating log file at `%USERPROFILE%\.cite\logs\cite.log` (1 MB × 5 backups). You never need to redirect output yourself for day-to-day viewing — run `cite log` to open that folder.

The Task Scheduler arguments below still include a small `> bootstrap.log 2>&1` redirect. This covers the rare case where `uvx` itself fails before Python starts (e.g. GitHub unreachable, dependency conflict) — no Python code runs in that case, so the internal logger never gets a chance. The bootstrap file lives in the same `.cite\logs\` folder.

---

### Email alerts on failure

`cite clean`, `cite renew`, `cite apply-update`, and `cite notify-renewal` all send a failure email when they exit non-zero or raise an uncaught exception. Configure this once per Windows user account; every scheduled task on that account picks it up automatically. If the env vars are absent, alerting silently no-ops.

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
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite clean -d 25 -f > %USERPROFILE%\.cite\logs\bootstrap.log 2>&1"
```

- `-d 25` — delete files older than 25 days (adjust as needed).
- `-f` — skip the confirmation prompt (required for unattended runs).

To clean a specific directory instead of the defaults, add the path as the first argument:

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite clean D:\MyData -d 30 -f > %USERPROFILE%\.cite\logs\bootstrap.log 2>&1"
```

---

### `cite renew` — full renewal cycle (apply then submit) in one command

Runs the complete renewal loop daily: first apply Nikon's reply if one is pending, then submit a fresh request if the dongle is in the renewal window. **One Task Scheduler entry per machine** covers both halves of the cycle.

**Phase 1 — apply** (runs first; skip with `--no-apply`): if `%USERPROFILE%\.cite\renew_state.json` exists, polls the shared Gmail inbox for Nikon's `.l2c` reply, downloads it, verifies the HASP ID matches this dongle, and applies it via `nis_hasp_update.exe -a`. Sends an URGENT email if no matching reply has arrived AND the license expires in ≤ 4 days.

**Phase 2 — submit** (always runs unless phase 1 raised): reads the dongle's expiration via ACC, checks the renewal window, and submits a fresh `.c2l` to Nikon if needed.

Each phase has its own failure-alert wrapper — a failure in phase 1 does **not** block phase 2.

**Per-phase details:**

- Phase 2 reads expiration live from the local Sentinel HASP dongle via ACC at `http://localhost:1947`.
- Phase 2 auto-generates the `.c2l` by running `nis_hasp_update.exe -r` (discovered under `C:\Program Files\NIS-Elements*\HASP\`).
- The submission note includes the HASP ID (e.g. `09882A98`) so Nikon's staff can identify the dongle.
- **Idempotent**: once submitted for a given expiration date, won't re-submit until Nikon's updated `.c2v` is applied (state in `%USERPROFILE%\.cite\renew_state.json`). Safe to schedule daily.
- Phase 1's multi-PC handling: the shared `citeathms@gmail.com` inbox receives replies for every microscope. Each candidate `.l2c` is identified by its HASP-ID-in-filename (`<HEX>.l2c`); we apply only the one matching this PC. Other PCs' Message-IDs are cached in `~/.cite/checked_emails.json` so they are never re-downloaded.
- **Defense-in-depth**: HASP ID is verified from the filename before applying, and `nis_hasp_update.exe` itself rejects key-type mismatches.

**Prerequisites for phase 1:** uses the same Gmail App Password set up for [email alerts](#email-alerts-on-failure). `CITE_ALERT_SMTP_USER` / `CITE_ALERT_SMTP_PASSWORD` serve both outbound SMTP and inbound IMAP — no additional env vars needed.

**Task Scheduler arguments** (runs daily):

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite renew --email you@example.com --full-name "Your Name" --url nikon > %USERPROFILE%\.cite\logs\bootstrap.log 2>&1"
```

Schedule at e.g. 01:00. **Stagger start times across machines** to avoid all PCs hitting Gmail simultaneously:

- **(Recommended)** In the trigger's **Edit** dialog, enable **Delay task for up to (random delay)** and set 1 hour. Task Scheduler picks a different random offset each run.
- Or set a fixed offset per PC (01:00, 01:05, 01:10, …).

On most days the apply phase exits cleanly because no `.cite/renew_state.json` exists (no pending renewal), and the submit phase exits cleanly because the license isn't yet within the 14-day window — net effect: a quick log line and exit 0.

**Optional overrides:**

- If `nis_hasp_update.exe` is not auto-discovered, set its path:

  ```powershell
  setx CITE_RUS_EXE "C:\custom\path\to\nis_hasp_update.exe"
  ```

- To supply a pre-generated `.c2l` instead of auto-generating one:

  ```bat
  ... cite renew --email ... --full-name ... --url nikon --c2l-file C:\path\to\file.c2l
  ```

- To skip the apply phase (e.g. for testing the submit path in isolation):

  ```bat
  ... cite renew ... --no-apply
  ```

**Dry-run (no side effects):**

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite renew `
    --email you@example.com --full-name "Your Name" --url nikon `
    --force --dry-run
```

`--force` bypasses the renewal-window check; `--dry-run` skips the POST and the `.c2l` generation. Nothing is submitted or written. (The apply phase runs but its `--dry-run` semantics — diagnostic-only, no apply — also apply.)

---

### `cite notify-renewal` — post-renewal email confirmation

Sends the renewal-confirmation email if the dongle's expiration has advanced since the last notification. Idempotent — re-running with no change is a no-op.

**Why schedule this alongside `cite renew`?** When `cite renew` applies an update itself, it sends the confirmation email immediately and updates the tracking file, so `cite notify-renewal` is a no-op. But if you ever apply a license **manually** via Nikon's HASP Update GUI, `cite notify-renewal` will detect the new expiry on its next daily run and send the email. It also acts as a safety net for cases where `cite renew`'s email was not delivered (SMTP misconfigured, network blip).

**No duplicate emails:** `cite renew` writes `%USERPROFILE%\.cite\last_notified_renewal.json` after each successful apply. `cite notify-renewal` only sends an email when the current expiry is *newer* than what's recorded in that file — so if `cite renew` already sent the email, `cite notify-renewal` silently exits.

**One-time setup (per machine — run this once before scheduling):**

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite notify-renewal --seed
```

This writes the current expiry as the baseline. Subsequent daily runs are no-ops until the dongle's expiry advances.

**Task Scheduler arguments** (runs daily, same trigger time as `cite renew`):

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite notify-renewal > %USERPROFILE%\.cite\logs\bootstrap.log 2>&1"
```

---

### `cite apply-update` — standalone apply-only command (advanced / debugging)

This is the same logic as `cite renew`'s apply phase, exposed as a standalone command for debugging and `--dry-run` testing. **You do not need to schedule this separately if you already schedule `cite renew`** — the apply phase runs there by default.

After a successful apply, `cite apply-update` automatically:
- Sends the renewal-confirmation email (subject: `[cite-cli] NIS-Elements license renewed on <Station Name or hostname>`).
- Updates `%USERPROFILE%\.cite\last_notified_renewal.json` so that `cite notify-renewal` (see below) knows the new expiry date and won't re-send.

When useful:

- **`--dry-run` mode**: cross-platform diagnostic. Polls IMAP, downloads candidate `.l2c` files into a tmp dir, parses HASP IDs, and reports what was found — without invoking `nis_hasp_update.exe`, writing state, or touching the production cache. Lets you verify your Gmail credentials and the inbox shape on macOS before deploying to Windows.
- **Manual one-shot apply**: if you've manually placed a `~/.cite/renew_state.json` and want to force the apply step without the submit phase running afterward.

If you do want a separate, IMAP-only Task Scheduler entry (e.g. to poll more frequently than `cite renew` runs):

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite apply-update > %USERPROFILE%\.cite\logs\bootstrap.log 2>&1"
```

Stagger across machines the same way as `cite renew` (random delay in Task Scheduler).

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

### `cite notify-renewal`

Send the renewal-confirmation email if the dongle's expiration has advanced since the last notification. Idempotent — re-running with no change is a no-op.

Useful when a license was applied **manually** via Nikon's HASP Update GUI (bypassing `cite apply-update`), or as a scheduled safety-net to catch cases where the email was not delivered during the original `apply-update` run (SMTP misconfigured, network blip, etc.).

```
cite notify-renewal [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--seed` | `False` | Record the current dongle state as the baseline without sending an email. Run this **once** on each freshly-set-up machine before scheduling the command. |

**First-time setup (per machine):**

```powershell
uvx --from "git+https://github.com/CITE-HMS/cite-cli" cite notify-renewal --seed
```

This writes `%USERPROFILE%\.cite\last_notified_renewal.json` with the current expiry date. Subsequent daily runs are no-ops until the dongle's expiry advances (i.e. a renewal was applied).

**Scheduling (optional, Task Scheduler):**

```bat
/c "<path/to/uv.exe> tool run --from git+https://github.com/CITE-HMS/cite-cli cite notify-renewal > %USERPROFILE%\.cite\logs\bootstrap.log 2>&1"
```

**State file:** `%USERPROFILE%\.cite\last_notified_renewal.json` — written atomically; contains `hasp_id`, `expiration_date`, and `notified_at`.

**Edge cases handled automatically:**

- HASP ID changed (dongle replaced): updates baseline silently without sending an email.
- SMTP configured but delivery fails: tracking file is **not** updated, so the next scheduled run retries.
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
