import os


def find_userd(noinput: bool = False) -> str | None:
    # detect user directory
    userd = None
    for i in ("D:/", "E:/", "F:/"):
        _d = os.path.join(i, "User Data")
        if os.path.isdir(_d):
            userd = _d
            break
    if not userd and not noinput:
        userd = ""
        print("Could not autodetect user directory")
        while not os.path.exists(userd):
            if userd.lower() == "q":
                return None
            userd = input(f"{userd} does not exist. Enter path (Q to quit):")
    return userd
