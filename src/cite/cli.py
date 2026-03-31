from pathlib import Path
from typing import Annotated

import typer

import cite

app = typer.Typer(no_args_is_help=True, add_completion=False)
STATE = {"verbose": False}


def _ts() -> str:
    from datetime import datetime

    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")


# List of default paths to clean if no path is specified.
DEFAULT_PATHS = [
    "D:/User_Data",  # s1, s2, s8, s9, s14
    "E:/User_Data",  # s1, s2, s3
    "F:/User_Data",  # s1, s2, s5
    "C:/User_Data",  # s8
    # "C:/UserData",  # s10
    "F:/UserData",  # s15
    # "C:/UserData",  # s18
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
    if directory is None:
        dirs = [d for d in DEFAULT_PATHS if Path(d).is_dir()]
        if not dirs:
            typer.secho("No default directories found on this machine.", fg="red")
            raise typer.Exit(1)
        for d in dirs:
            typer.secho(f"{_ts()}Cleaning default path: {d!r}", fg="bright_blue", bold=True)
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
                old_file.unlink()
                typer.secho(f"{_ts()}Deleted {name_age}", fg="green")
                count += 1
            except Exception as e:
                typer.secho(f"{_ts()}Failed to delete {name_age}: {e}", err=True, fg="red")
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


def main() -> None:
    """Run main app."""
    app()
