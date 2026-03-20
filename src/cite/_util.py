import os
from typing import Optional

PATHS = ["C:/UserData"]


def find_userd(noinput: bool = False) -> Optional[str]:
    # detect user directory
    userd = None
    for path in PATHS:
        if os.path.isdir(path):
            userd = path
            break
    if not userd and not noinput:
        userd = ""
        print("Could not autodetect user directory")
        while not os.path.exists(userd):
            if userd.lower() == "q":
                return None
            userd = input(f"{userd} does not exist. Enter path (Q to quit):")
    return userd
