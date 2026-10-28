"Safe creation and replacement of application-owned private local files."

import os
import stat
import tempfile
from pathlib import Path


def restrict_existing_private(path: Path) -> None:
    "Restrict legacy POSIX files without broadening existing owner permissions."
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("private_file_must_be_regular")
        os.fchmod(fd, stat.S_IMODE(metadata.st_mode) & 0o600)
    finally:
        os.close(fd)


def create_private(path: Path, content: bytes) -> None:
    "Exclusively create a file with owner-only POSIX mode; keep Windows ACL behavior."
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def replace_private(path: Path, content: bytes) -> None:
    "Atomically replace using a unique private temporary file in the same directory."
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
