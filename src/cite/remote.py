from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from getpass import getpass
from urllib.parse import quote

# To add an SMB share for an NIC microscope:
# - right click on the folder you want to share: (e.g. User Data)
# - click Properties
# - click Sharing tab
# - click Advanced Sharing ...
# - check Share this folder
# - change the share name to "data"
# - click Permissions
# - remove "Everyone" from the list
# - click Add
# - type "Admin" in the box
# - click Check Names
# - click OK
# - click Allow Full Control
# - click Apply
# - click OK
# - confirm that the share path looks something like \\NIC-1234\data

#
# - go to programs and features
# - click Turn Windows features on or off
# - check SMB 1.0/CIFS File Sharing Support

# tip for windows 7
# Netloon Service (NP-in needs to be enabled in Advanced Windows Firewall settings)


@contextmanager
def mount_smb(
    server: str,
    share: str = "data",
    user: str = "Admin",
    password: str | None = None,
) -> Iterator[str]:
    """Mount smb share at `//server/share` to a temporary directory."""
    password = password or os.getenv("CITE_PASSWORD") or getpass("CITE Password: ")
    if not password:
        raise ValueError("Password required")

    target = f"//{quote(user)}:{quote(password)}@{server}/{quote(share)}"
    mountpoint = tempfile.mkdtemp()
    cmd = ["mount", "-t", "smbfs", target, mountpoint]
    response = subprocess.run(cmd)
    if response.returncode != 0:
        raise RuntimeError("Failed to mount SMB share")
    try:
        yield mountpoint
    finally:
        subprocess.run(["umount", mountpoint])
        os.rmdir(mountpoint)
