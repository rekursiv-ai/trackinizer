"""Rename a path without replacing what is at its destination, in one step.

``os.rename`` replaces a file or an empty directory at the destination, and a
check before it leaves a window another process can fill. POSIX has no rename
that refuses instead: its ``rename`` and ``renameat`` replace, and ``link`` then
``unlink``, which does refuse, cannot move a directory. So this uses each kernel's
own extension, which the standard library exposes neither of: Linux's
``renameat2(RENAME_NOREPLACE)`` and macOS's ``renameatx_np(RENAME_EXCL)``.

Typical usage example:

  rename_noreplace(staging, published)  # FileExistsError if published exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import ctypes
import errno
import os
import sys


if TYPE_CHECKING:
    from pathlib import Path


__all__ = ["rename_noreplace"]


def rename_noreplace(source: Path, destination: Path) -> None:
    """Rename ``source`` to ``destination``, failing if ``destination`` exists.

    Args:
      source: The file or directory to move.
      destination: Its new path, which must not exist when the kernel renames.

    Raises:
      FileExistsError: ``destination`` exists, even if it appeared just before.
      OSError: The rename failed otherwise; ``errno.ENOSYS`` when this system has
        no no-replace rename.

    """
    platform = sys.platform
    if platform == "win32":
        # Windows' own rename already refuses an existing destination.
        source.rename(destination)
        return
    darwin = platform == "darwin"
    try:
        library = ctypes.CDLL(None, use_errno=True)
        rename = library.renameatx_np if darwin else library.renameat2
    except AttributeError as error:
        raise OSError(
            errno.ENOSYS,
            "no-replace rename (renameat2 or renameatx_np) is unavailable",
            destination,
        ) from error
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    # The two take the same arguments; AT_FDCWD (-2, -100) and the no-replace flag
    # (RENAME_EXCL 4, RENAME_NOREPLACE 1) differ.
    directory_fd, flags = (-2, 4) if darwin else (-100, 1)
    result = cast(
        int,
        rename(
            directory_fd,
            os.fsencode(source),
            directory_fd,
            os.fsencode(destination),
            flags,
        ),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        # OSError builds the errno's own subclass: FileExistsError for EEXIST.
        raise OSError(error_number, os.strerror(error_number), destination)
