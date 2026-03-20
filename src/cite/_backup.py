import contextlib
import getpass
import os
import socket
import zipfile
from datetime import datetime
from typing import List, Optional

from smb.SMBConnection import OperationFailure, SMBConnection, SMBTimeout
from tqdm import tqdm

# These folders will be backed up, if they exist
BACKUP = [
    "C:/Users/Public/NIC/Elements Macros",
    "C:/Users/Public/NIC/Elements Settings",
    "C:/Users/Public/NIC/Metamorph Journals",
    "C:/Users/Public/NIC/Metamorph Settings",
    "C:/Users/Public/NIC/Ti2 settings",
    "C:/MM",
    "C:/Journals",
    "C:/NIC Elements Configuration Files",
    "E:/MM Data",
    "F:/MM Data",
    "D:/MM Data",
]
STATION_NAME = socket.gethostname()


def connect_to_smb(
    servername: str = "nucleus.med.harvard.edu", share: str = "nicadmin", tries: int = 4
) -> Optional[SMBConnection]:
    print(f"Enter login credentials for {servername}/{share}")
    addr = socket.gethostbyname(servername)
    _try = 0
    while _try < tries:
        try:
            _try += 1
            userid = input("Username: ")
            passwrd = getpass.getpass()
            conn = SMBConnection(userid, passwrd, "NIC", servername, use_ntlm_v2=True)
            conn.connect(addr, timeout=8)
            conn.listPath(share, "/")
            return conn
        except SMBTimeout:
            print("Connection timeout... try again")
        except OperationFailure:
            print(f"connection to {share} failed... try again")
            share = input("SMB share (e.g. nicadmin): ")
    return None


def create_remote_dir(conn: "SMBConnection", share: str, dst: str) -> None:
    L = [i for i in dst.split("/") if i]
    for i, x in enumerate(L):
        _dst = "/".join(L[: i + 1])
        with contextlib.suppress(OperationFailure):
            conn.createDirectory(share, _dst)


def backup_folders(
    conn: "SMBConnection",
    folderlist: List[str] = BACKUP,
    share: str = "nicadmin",
    dst: str = "/BackUp/AUTO/",
) -> None:
    date = datetime.now().strftime("%Y%m%d")
    outname = f"{date}_{STATION_NAME}.zip"
    print(f"\nBacking up configuration to {share + dst}...")
    with zipfile.ZipFile(outname, "w", zipfile.ZIP_DEFLATED) as archive:
        for src in folderlist:
            if not os.path.exists(src):
                continue
            _dir = src.replace(":/", "_").replace("\\", "_").replace("/", "_")
            _dir += ".zip"
            transfers: List[str] = []
            for root, _, files in os.walk(src):
                transfers.extend(os.path.join(root, file) for file in files)
            print(f"Zipping {_dir}...")
            for t in tqdm(transfers):
                archive.write(t)
    with open(outname, "rb") as f:
        print("copying to server...")
        create_remote_dir(conn, share, dst)
        try:
            conn.storeFile(share, dst + outname, f)
        except Exception as e:
            print(f"BACKUP FAILED: {str(e)}")
    os.remove(outname)
