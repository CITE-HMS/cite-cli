import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import typer

import cite
from cite._renew import RenewTarget, hasp_id_to_hex, hasp_id_to_station

app = typer.Typer(no_args_is_help=True, add_completion=False)
STATE = {"verbose": False}


def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")


def _dispatch_alert(command: str, error: BaseException) -> None:
    """Send a failure email (if configured) and log the dispatch result."""
    from cite._notify import send_failure_email

    # Unwrap typer.Exit to surface the underlying cause in the email.
    underlying: BaseException = error
    for _ in range(10):
        if not isinstance(underlying, typer.Exit) or underlying.__cause__ is None:
            break
        underlying = underlying.__cause__

    try:
        sent = send_failure_email(command, underlying)
    except Exception as e:  # never let alerting mask the real failure
        typer.secho(f"{_ts()}(alert dispatch errored: {e})", fg="yellow", err=True)
        return
    if sent:
        typer.secho(f"{_ts()}Failure alert email sent.", fg="yellow", err=True)


@contextmanager
def _alert_on_failure(command: str) -> Iterator[None]:
    """Wrap a command body; email on any failure exit. Aborts are not failures."""
    try:
        yield
    except typer.Abort:
        raise
    except typer.Exit as e:
        if e.exit_code:
            _dispatch_alert(command, e)
        raise
    except BaseException as e:
        _dispatch_alert(command, e)
        raise


# List of default paths to clean if no path is specified.
DEFAULT_PATHS = [
    "D:/User_Data",  # s1, s2, s8, s9, s10, s14
    "E:/User_Data",  # s1, s2, s3, s18
    "F:/User_Data",  # s1, s2, s5
    "C:/User_Data",  # s8
    "Z:/User_Data",  # s10
    "F:/UserData",  # s15
]


def _show_version_and_exit(value: bool) -> None:
    if value:
        typer.echo(f"cite-cli v{cite.__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool | None = typer.Option(
        None,
        "-v",
        "--version",
        callback=_show_version_and_exit,
        help="Show version and exit.",
    ),
) -> None:
    """Command line tool for CITE at HMS.

    v{version}
    """
    from cite._log import init_logging

    init_logging()


_main.__doc__ = typer.style(
    (_main.__doc__ or "").format(version=cite.__version__), fg="bright_yellow"
)


@app.command()
def update() -> None:
    """Update cite-cli itself."""
    import subprocess

    url = "https://github.com/CITE-HMS/cite-cli/archive/refs/heads/main.zip"
    subprocess.run(
        ["pip", "install", "--upgrade", "--force-reinstall", url],
        stderr=subprocess.DEVNULL,
    )


@app.command("log")
def show_log() -> None:
    """Open the ~/.cite/logs/ folder in the system file manager."""
    from cite._log import CITE_LOG, LOGS_DIR, open_logs_dir

    typer.secho(f"{_ts()}Opening {LOGS_DIR} ...", fg="bright_blue")
    if CITE_LOG.exists():
        size_kb = CITE_LOG.stat().st_size // 1024
        typer.secho(
            f"  cite.log: {size_kb} KB",
            fg=(160, 160, 160),
        )
    open_logs_dir()


@app.command()
def clean(
    directory: str | None = typer.Argument(
        None,
        help="The directory to cleanup. May be a local path or an smb:// path. "
        "If omitted, all default paths will be cleaned. "
        "If an smb:// path, the user name will default to 'Admin', unless it is "
        "specified in the path (e.g. 'Admin@server'). It is recommended to set "
        "the password as an environment variable: CITE_PASSWORD='mypassword'. "
        "For example: CITE_PASSWORD='mypassword' cite clean smb://Admin@10.10.10.10/share",
    ),
    days: float = typer.Option(
        30,
        "-d",
        "--days",
        metavar="FLOAT",
        help="Number of days old a file must be to be deleted",
    ),
    dry_run: bool = typer.Option(
        False,
        "-n",
        "--dry-run",
        help="Don't delete anything. Just print what would be deleted and exit",
    ),
    force: bool = typer.Option(
        False,
        "-f",
        "--force",
        help="Delete without confirmation (otherwise a prompt is shown with "
        "the number of files that would be deleted)",
    ),
    delete_empty_dirs: bool = typer.Option(True, help="Delete empty directories."),
    skip: str = typer.Option("delete", help="Don't delete files with this string."),
) -> None:
    """Delete files in a given directory older than a certain age."""
    with _alert_on_failure("clean"):
        if directory is None:
            dirs = [d for d in DEFAULT_PATHS if Path(d).is_dir()]
            if not dirs:
                typer.secho("No default directories found on this machine.", fg="red")
                raise typer.Exit(1)
            for d in dirs:
                typer.secho(
                    f"{_ts()}Cleaning default path: {d!r}",
                    fg="bright_blue",
                    bold=True,
                )
                _clean_directory(d, days, dry_run, force, delete_empty_dirs, skip)
        else:
            _clean_directory(directory, days, dry_run, force, delete_empty_dirs, skip)


def _clean_directory(
    directory: str,
    days: float,
    dry_run: bool,
    force: bool,
    delete_empty_dirs: bool,
    skip: str,
) -> None:
    """Clean a single directory."""
    context = None
    if directory.startswith("smb://"):
        from cite.remote import mount_smb

        server, *rest = directory[6:].split("/")
        share = rest[0] if rest else "data"
        user = "Admin"
        if "@" in server:
            user, server = server.split("@")
        if ":" in user:
            raise ValueError("Usernames with ':' are not supported")

        context = mount_smb(server, share, user)
        _directory = Path(context.__enter__())
        typer.secho(f"{_ts()}loaded remote directory")
    else:
        _directory = Path(directory).resolve()
        if not _directory.is_dir():
            if _directory.exists():
                typer.secho(f"{_ts()}Path is not a directory: {directory!r}", fg="red")
            else:
                typer.secho(f"{_ts()}Directory does not exist: {directory!r}", fg="red")
            return

    typer.secho(f"{_ts()}Cleaning directory: {directory!r}")
    try:
        # grab list of old files
        old_files = list(cite.iter_old_files(_directory, days, skip=skip))

        # if there are no old files, exit
        if not old_files:
            typer.secho(
                f"{_ts()}No files found in {directory!r} older than {days} days!",
                fg="green",
                bold=True,
            )
            return

        # if dry_run, just print what would be deleted
        if dry_run:
            for old_file, age in old_files:
                name_age = f"{old_file} ({age:.1f} days old)"
                typer.secho(f"{_ts()}Would delete {name_age}", fg=(140, 140, 140))
            return

        # if force was not specified, ask for confirmation
        if not force:
            msg = typer.style(
                f"This will delete {len(old_files)} files (use '--dry-run' to show them"
                "). Are you sure?",
                fg=typer.colors.BRIGHT_MAGENTA,
                bold=True,
            )
            typer.confirm(msg, abort=True)

        # actually delete files
        count = 0
        errs = 0
        for old_file, age in old_files:
            name_age = f"{old_file} ({age:.1f} days old)"
            try:
                try:
                    old_file.unlink()
                except PermissionError:
                    # On Windows, files with the read-only attribute raise
                    # PermissionError on unlink even when ACLs allow delete.
                    old_file.chmod(stat.S_IWRITE)
                    old_file.unlink()
                typer.secho(f"{_ts()}Deleted {name_age}", fg="green")
                count += 1
            except Exception as e:
                typer.secho(
                    f"{_ts()}Failed to delete {name_age}: {e}", err=True, fg="red"
                )
                errs += 1

        if delete_empty_dirs:
            typer.secho("---------------------------------------", fg=(110, 110, 110))
            for empty in cite.iter_empty_dirs(_directory, skip=skip):
                try:
                    empty.rmdir()
                    typer.secho(f"{_ts()}Deleted empty directory {empty}", fg="green")
                except Exception as e:
                    typer.secho(
                        f"{_ts()}Failed to delete empty directory {empty}: {e}",
                        err=True,
                        fg="red",
                    )

        typer.secho("---------------------------------------", fg=(160, 160, 160))

        # print summary
        if count:
            typer.secho(f"{_ts()}Deleted {count} files", fg="green", bold=True)
        if errs:
            typer.secho(f"{_ts()}Unable to delete {errs} files.", fg="red", bold=True)
    finally:
        if context:
            context.__exit__(None, None, None)


@app.command()
def clean_many(
    ip_file: Annotated[
        Path,
        typer.Argument(dir_okay=False, file_okay=True, resolve_path=True, exists=True),
    ],
    force: bool = typer.Option(
        False,
        "-f",
        "--force",
        help="Delete without confirmation (otherwise a prompt is shown with "
        "the number of files that would be deleted)",
    ),
) -> None:
    r"""Clean many directories at once from json file.

    File should be a single object, with station names as keys and IP addresses
    as values.

    Example:
    cite clean-many ~/Dropbox\ \(HMS\)/CITE\ Team/Equipment/stations_ips.json
    """
    import json
    from concurrent.futures import ThreadPoolExecutor

    if ip_file.suffix != ".json":
        raise typer.BadParameter("File must have .json extension")

    with open(ip_file) as f:
        data = json.load(f)

    with ThreadPoolExecutor(max_workers=8) as pool:
        args = [
            (f"smb://{ip}/data", 60, False, force, True, "delete")
            for _, ip in data.items()
            if ip is not None
        ]
        list(pool.map(_try_clean, args))


def _try_clean(args: tuple) -> None:
    """Wrap clean in a try catch for multithreading."""
    try:
        clean(*args)
    except Exception as e:
        if isinstance(e, typer.Exit) and e.exit_code == 0:
            return
        typer.secho(f"Failed to clean {args[0]}: {e}", fg="red")


def _port_in_use(host: str, port: int) -> bool:
    """Return True if something is listening at host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


@contextmanager
def _auto_mock_server_if_needed(target: RenewTarget, url: str) -> Iterator[None]:
    """For --url test, spin up the mock for this run if nothing's listening yet."""
    if target is not RenewTarget.test:
        yield
        return
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    if _port_in_use(host, port):
        typer.secho(
            f"{_ts()}Using existing mock server at {host}:{port}.",
            fg="bright_blue",
        )
        yield
        return
    from cite.mock_renew.server import serving

    typer.secho(
        f"{_ts()}Auto-starting mock server at {host}:{port} ...",
        fg="bright_blue",
    )
    with serving(host=host, port=port):
        try:
            yield
        finally:
            typer.secho(f"{_ts()}Stopping auto-started mock server.", fg="bright_blue")


@app.command()
def renew(
    email: str = typer.Option(
        ...,
        "--email",
        envvar="CITE_LICENSE_EMAIL",
        help="Email to put in the renewal form.",
    ),
    full_name: str = typer.Option(
        ...,
        "--full-name",
        envvar="CITE_LICENSE_FULL_NAME",
        help="Full name to put in the renewal form.",
    ),
    target: RenewTarget = typer.Option(
        ...,
        "--url",
        envvar="CITE_LICENSE_URL",
        case_sensitive=False,
        help="Renewal target: 'nikon' (real Nikon endpoint) or 'test' (local "
        "mock at http://127.0.0.1:8765/). Required.",
    ),
    c2l_file: str | None = typer.Option(
        None,
        "--c2l-file",
        envvar="CITE_LICENSE_C2L_FILE",
        help="Path to the .c2l file to upload. If omitted, auto-generates a "
        "fresh one via nis_hasp_update.exe (written to "
        "%USERPROFILE%\\.cite\\generated_request.c2l). "
        "Use the literal 'mock' for the bundled mock.c2l (for --url test).",
    ),
    note: str = typer.Option(
        "CITE @ Harvard Medical School",
        "--note",
        envvar="CITE_LICENSE_NOTE",
        help="Free-text note included with the submission.",
    ),
    days_before: int = typer.Option(
        14,
        "--days-before",
        help="Submit only when the license expires within this many days.",
    ),
    dry_run: bool = typer.Option(
        False,
        "-n",
        "--dry-run",
        help="Print what would be submitted and exit; do not POST.",
    ),
    force: bool = typer.Option(
        False,
        "-f",
        "--force",
        help="Submit even if the license is not within the renewal window.",
    ),
) -> None:
    """Monitor the license and submit a renewal request to Nikon when due.

    A daily `cite renew` does the following:

    1. Detect a completed renewal: if the dongle's expiration advanced
       since the last baseline (i.e. an update was applied manually), send
       the confirmation email plus Google-Calendar reminder invites for
       the new expiration date, and clear any stale pending state.

    2. Submit phase: read the dongle's expiration via ACC, check the
       renewal window, dedup against any prior submission, and POST a fresh
       `.c2l` to Nikon if needed. While a submission is pending and the
       license is within 4 days of expiry, sends an URGENT reminder email
       (at most once per 20 h) to apply Nikon's reply manually.

    Applying Nikon's reply is a manual step: download the .l2c from the
    link in Nikon's email and apply it on the station via the HASP Update
    GUI (nis_hasp_update.exe). The next daily run detects the new
    expiration date and sends the confirmation + calendar invites.
    """
    with _alert_on_failure("renew"):
        # Detect a manual renewal first: send confirmation + calendar
        # invites if the expiration advanced, and clear stale pending
        # state so the submit phase can start the next cycle cleanly.
        _check_and_notify_renewal(auto_seed=True)
        _clear_stale_renew_state_if_renewed()

        from cite._renew import (
            GENERATED_C2L_PATH,
            RENEW_STATE_PATH,
            RenewState,
            generate_c2l,
            get_license_info,
            load_renew_state,
            resolve_c2l_file,
            resolve_url,
            save_renew_state,
            should_renew,
            submit_license_form,
        )

        url = resolve_url(target)
        c2l_path: Path | None = None
        if c2l_file is not None:
            try:
                c2l_path = resolve_c2l_file(c2l_file)
            except FileNotFoundError as e:
                typer.secho(f"{_ts()}{e}", fg="red", err=True)
                raise typer.Exit(1) from e

        try:
            info = get_license_info()
        except RuntimeError as e:
            typer.secho(f"{_ts()}{e}", fg="red", err=True)
            raise typer.Exit(1) from e
        exp_date, hasp_id = info.expiration_date, info.hasp_id

        # Match the hex form Nikon's tools display (e.g. "09882A98").
        station = hasp_id_to_station(hasp_id)
        station_part = f"{station} | " if station else ""
        note = f"{note} [{station_part}HASP ID: {hasp_id_to_hex(hasp_id)}]"

        days_left = (exp_date - datetime.now().date()).days
        typer.secho(
            f"{_ts()}License expires {exp_date.isoformat()} ({days_left} days left).",
            fg="bright_blue",
        )

        if not force and not should_renew(exp_date, days_before):
            typer.secho(
                f"{_ts()}No renewal needed (window: {days_before} days).",
                fg="green",
                bold=True,
            )
            return

        if not force:
            state = load_renew_state()
            if state and state.expiration_date == exp_date and state.hasp_id == hasp_id:
                typer.secho(
                    f"{_ts()}Already submitted on "
                    f"{state.submitted_at.date().isoformat()} for license expiring "
                    f"{exp_date.isoformat()}. Awaiting Nikon's updated .c2v; "
                    f"rerun with --force to resubmit.",
                    fg="yellow",
                )
                # Submission pending but not applied yet: nag (throttled to
                # one email per 20 h) when expiry is imminent.
                from cite._notify import send_urgency_alert

                _maybe_send_urgency(state, send_urgency_alert)
                return

        if c2l_path is None and not dry_run:
            typer.secho(
                f"{_ts()}Generating fresh .c2l via nis_hasp_update.exe ...",
                fg="bright_blue",
            )
            try:
                c2l_path = generate_c2l(GENERATED_C2L_PATH)
            except RuntimeError as e:
                typer.secho(f"{_ts()}{e}", fg="red", err=True)
                raise typer.Exit(1) from e

        if dry_run:
            typer.secho(f"{_ts()}Would submit to {url}", fg=(140, 140, 140))
            typer.secho(f"  email     = {email}", fg=(140, 140, 140))
            typer.secho(f"  full_name = {full_name}", fg=(140, 140, 140))
            if c2l_path is not None:
                typer.secho(
                    f"  c2l_file  = {c2l_path} ({c2l_path.stat().st_size} bytes)",
                    fg=(140, 140, 140),
                )
            else:
                typer.secho(
                    f"  c2l_file  = (auto-generate to {GENERATED_C2L_PATH})",
                    fg=(140, 140, 140),
                )
            typer.secho(f"  note      = {note}", fg=(140, 140, 140))
            typer.secho(
                f"  state     = {RENEW_STATE_PATH} (hasp_id={hasp_id})",
                fg=(140, 140, 140),
            )
            return

        if c2l_path is None:
            typer.secho(
                f"{_ts()}Internal error: no .c2l path available.", fg="red", err=True
            )
            raise typer.Exit(1)
        typer.secho(f"{_ts()}Submitting renewal request to {url} ...")
        with _auto_mock_server_if_needed(target, url):
            try:
                resp = submit_license_form(
                    url=url,
                    email=email,
                    full_name=full_name,
                    c2l_file=c2l_path,
                    note=note,
                )
            except Exception as e:
                typer.secho(f"{_ts()}Submission failed: {e}", fg="red", err=True)
                raise typer.Exit(1) from e

        typer.secho(
            f"{_ts()}Submitted. HTTP {resp.status_code}.", fg="green", bold=True
        )

        save_renew_state(
            RenewState(
                expiration_date=exp_date,
                hasp_id=hasp_id,
                submitted_at=datetime.now(timezone.utc),
                url=url,
            )
        )


@app.command("license")
def license_info(
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Print the unfiltered ACC features feed (for debugging).",
    ),
) -> None:
    """Read the NIS-Elements license expiration date from the local HASP dongle."""
    from cite._renew import fetch_acc_response, get_license_info

    if raw:
        try:
            resp = fetch_acc_response()
        except RuntimeError as e:
            typer.secho(f"{_ts()}{e}", fg="red", err=True)
            raise typer.Exit(1) from e
        typer.echo(resp.text)
        return

    try:
        info = get_license_info()
    except RuntimeError as e:
        typer.secho(f"{_ts()}{e}", fg="red", err=True)
        raise typer.Exit(1) from e

    days_left = (info.expiration_date - datetime.now().date()).days
    typer.secho(
        f"{_ts()}License expires {info.expiration_date.isoformat()} "
        f"({days_left} days left).",
        fg="bright_blue",
    )
    typer.echo(f"HASP ID: {info.hasp_id}")


@app.command("request-file")
def request_file(
    output: Path = typer.Option(
        None,
        "--output",
        "-o",
        help="Where to write the .c2l file. "
        "Defaults to %USERPROFILE%\\.cite\\generated_request.c2l.",
    ),
) -> None:
    """Generate a fresh .c2l renewal request via nis_hasp_update.exe."""
    from cite._renew import GENERATED_C2L_PATH, generate_c2l

    target = output or GENERATED_C2L_PATH
    typer.secho(
        f"{_ts()}Generating .c2l via nis_hasp_update.exe -> {target} ...",
        fg="bright_blue",
    )
    try:
        path = generate_c2l(target)
    except RuntimeError as e:
        typer.secho(f"{_ts()}{e}", fg="red", err=True)
        raise typer.Exit(1) from e

    typer.secho(
        f"{_ts()}Wrote {path} ({path.stat().st_size:,} bytes).",
        fg="green",
        bold=True,
    )


def _check_and_notify_renewal(*, auto_seed: bool, strict: bool = False) -> None:
    """Detect an applied renewal (dongle expiry advanced past the baseline)
    and send the confirmation email + calendar reminder invites.

    Shared by `cite renew` (every daily run, non-strict) and the
    `notify-renewal` command (strict: ACC/SMTP failures exit non-zero).
    Non-strict mode never raises, so a hiccup here cannot block the submit
    phase. The baseline is only updated once the confirmation email is
    delivered (or SMTP is unconfigured), so failed sends retry next run.
    """
    from cite._calendar import send_reminder_invites
    from cite._notify import _is_configured, send_apply_success_email
    from cite._renew import (
        get_license_info,
        hasp_id_to_hex,
        hasp_id_to_station,
        load_last_notified,
        save_last_notified,
    )

    try:
        current = get_license_info()
    except RuntimeError as e:
        if strict:
            typer.secho(f"{_ts()}{e}", fg="red", err=True)
            raise typer.Exit(1) from e
        return

    last = load_last_notified()
    if last is None:
        if not auto_seed:
            typer.secho(
                f"{_ts()}No notification baseline found. Run "
                "`cite notify-renewal --seed` once to initialise "
                "(records the current expiry without sending an email).",
                fg="yellow",
                err=True,
            )
            raise typer.Exit(1)
        save_last_notified(current)
        typer.secho(
            f"{_ts()}Notification baseline seeded: HASP {current.hasp_id}, "
            f"expires {current.expiration_date.isoformat()}.",
            fg="green",
        )
        return

    if last.hasp_id != current.hasp_id:
        typer.secho(
            f"{_ts()}HASP ID changed ({last.hasp_id} -> {current.hasp_id}). "
            "Updating baseline without sending an email.",
            fg="yellow",
        )
        save_last_notified(current)
        return

    if current.expiration_date <= last.expiration_date:
        if strict:
            typer.secho(
                f"{_ts()}Already notified about "
                f"{last.expiration_date.isoformat()}; no-op.",
                fg="green",
            )
        return

    # Expiration advanced — a renewal was applied (manually or via --apply).
    typer.secho(
        f"{_ts()}Renewal detected: {last.expiration_date.isoformat()} -> "
        f"{current.expiration_date.isoformat()}.",
        fg="green",
        bold=True,
    )
    configured = _is_configured()
    sent = send_apply_success_email(before=last, after=current)
    if sent:
        typer.secho(f"{_ts()}Renewal confirmation email sent.", fg="green")
        if send_reminder_invites(
            station=hasp_id_to_station(current.hasp_id),
            hasp_hex=hasp_id_to_hex(current.hasp_id),
            expiry=current.expiration_date,
        ):
            typer.secho(
                f"{_ts()}Calendar reminder invites sent "
                f"(14 d / 7 d / day-of {current.expiration_date.isoformat()}).",
                fg="green",
            )
        else:
            typer.secho(
                f"{_ts()}Calendar invite email failed to send.",
                fg="yellow",
                err=True,
            )
    elif configured:
        # Configured but SMTP failed — leave baseline unchanged so next run
        # retries the whole notification.
        typer.secho(
            f"{_ts()}SMTP error: renewal confirmation email not sent. "
            "Tracking file not updated; will retry on next run.",
            fg="yellow",
            err=True,
        )
        if strict:
            raise typer.Exit(1)
        return
    else:
        typer.secho(f"{_ts()}SMTP not configured; skipping email.", fg="yellow")

    save_last_notified(current)
    typer.secho(
        f"{_ts()}Tracking file updated: {current.expiration_date.isoformat()}.",
        fg="green",
    )


def _clear_stale_renew_state_if_renewed() -> None:
    """Clear renew_state.json if the dongle's expiry already exceeds the pending state.

    Called at the start of `cite renew` so that a manually-applied license
    doesn't trigger urgency alerts or failed re-apply attempts in the
    apply phase.
    """
    from cite._renew import RENEW_STATE_PATH, get_license_info, load_renew_state

    state = load_renew_state()
    if state is None:
        return
    try:
        current = get_license_info(hasp_id=state.hasp_id)
    except RuntimeError:
        return
    if current.expiration_date > state.expiration_date:
        typer.secho(
            f"{_ts()}License already renewed "
            f"({state.expiration_date.isoformat()} -> "
            f"{current.expiration_date.isoformat()}). "
            "Clearing stale pending state.",
            fg="green",
        )
        RENEW_STATE_PATH.unlink(missing_ok=True)


def _maybe_send_urgency(state, send_urgency_alert) -> None:  # type: ignore[no-untyped-def]
    """Send the URGENT alert if we're within URGENCY_DAYS of expiration.

    Rate-limited to at most one alert per 20 hours so frequent Task Scheduler
    runs don't flood the inbox.
    """
    from cite._notify import URGENCY_DAYS
    from cite._renew import load_last_urgency, save_last_urgency

    days_remaining = (state.expiration_date - datetime.now().date()).days
    if days_remaining > URGENCY_DAYS:
        return

    last = load_last_urgency()
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (datetime.now(tz=timezone.utc) - last).total_seconds() < 20 * 3600:
            return

    sent = send_urgency_alert(state, days_remaining)
    if sent:
        save_last_urgency(datetime.now(tz=timezone.utc))
        typer.secho(
            f"{_ts()}URGENT alert email sent (license has "
            f"{days_remaining} day(s) left).",
            fg="yellow",
            err=True,
        )


@app.command("notify-renewal")
def notify_renewal(
    seed: bool = typer.Option(
        False,
        "--seed",
        help="Record the current dongle state as the notification baseline "
        "without sending an email. Run once on a freshly-set-up machine.",
    ),
) -> None:
    """Send the renewal-confirmation email + calendar reminder invites if the
    dongle's expiration has advanced since the last notification.
    Idempotent: re-running with no change is a no-op.

    The same check runs automatically on every `cite renew`; this command
    exists for manual/one-off use.
    """
    from cite._renew import get_license_info, save_last_notified

    with _alert_on_failure("notify-renewal"):
        if seed:
            try:
                current = get_license_info()
            except RuntimeError as e:
                typer.secho(f"{_ts()}{e}", fg="red", err=True)
                raise typer.Exit(1) from e
            save_last_notified(current)
            typer.secho(
                f"{_ts()}Baseline set: HASP {current.hasp_id}, "
                f"expires {current.expiration_date.isoformat()}. No email sent.",
                fg="green",
                bold=True,
            )
            return

        _check_and_notify_renewal(auto_seed=True, strict=True)


@app.command("test-alert")
def test_alert() -> None:
    """Send a test failure-alert email to verify SMTP configuration."""
    import os

    from cite._notify import _is_configured, send_failure_email

    if not _is_configured():
        typer.secho(
            f"{_ts()}Alert env vars not set. Set these in PowerShell first:\n"
            '  setx CITE_ALERT_SMTP_USER     "you@gmail.com"\n'
            '  setx CITE_ALERT_SMTP_PASSWORD "<Gmail App Password>"\n'
            '  setx CITE_ALERT_TO            "you@gmail.com"\n'
            "Then close and reopen PowerShell, and try again.",
            fg="yellow",
            err=True,
        )
        raise typer.Exit(1)

    err = RuntimeError(
        "Test alert from `cite test-alert` — if you got this email, "
        "the alert mechanism is working."
    )
    typer.secho(f"{_ts()}Sending test alert ...", fg="bright_blue")
    sent = send_failure_email("test-alert", err)
    if sent:
        typer.secho(
            f"{_ts()}Test alert sent to {os.environ['CITE_ALERT_TO']}. "
            "Check your inbox.",
            fg="green",
            bold=True,
        )
        return

    typer.secho(
        f"{_ts()}SMTP send failed. Common causes:\n"
        "  - Wrong App Password (regenerate at "
        "https://myaccount.google.com/apppasswords)\n"
        "  - 2-Step Verification not enabled on the Google account\n"
        "  - Network/firewall blocking outbound port 587\n"
        "  - Typo in CITE_ALERT_SMTP_USER or CITE_ALERT_TO",
        fg="red",
        err=True,
    )
    raise typer.Exit(1)


@app.command("mock-renew-server")
def mock_renew_server(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    log_dir: Path | None = typer.Option(
        None,
        "--log-dir",
        help="Directory to write submissions.log into. Defaults to CWD.",
    ),
) -> None:
    """Run the local mock of the Nikon renewal form (for testing 'cite renew')."""
    from cite.mock_renew import run

    run(host=host, port=port, log_dir=log_dir)


def main() -> None:
    """Run main app."""
    app()
