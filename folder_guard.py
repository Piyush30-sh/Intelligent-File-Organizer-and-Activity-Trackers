"""
folder_guard.py — Folder & system safety restrictions.

Blocks ALL OS / system / driver folders on every drive letter.
Any path that resolves into a restricted zone is rejected before
the application can monitor, organize, move-to, or delete from it.

Usage
-----
    from folder_guard import is_folder_safe, get_block_reason

    if not is_folder_safe(path):
        reason = get_block_reason(path)
        messagebox.showerror("Blocked", reason)
"""

import os
import re
import string

# ---------------------------------------------------------------------------
# 1.  Environment-expanded restricted roots  (case-insensitive on Windows)
# ---------------------------------------------------------------------------
_ENV_RESTRICTED = [
    # Windows core
    os.environ.get("SystemRoot", r"C:\Windows"),
    os.environ.get("windir", r"C:\Windows"),
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "SysWOW64"),
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    os.environ.get("ProgramW6432", r"C:\Program Files"),
    os.environ.get("APPDATA", ""),
    os.environ.get("LOCALAPPDATA", ""),
    os.environ.get("ProgramData", r"C:\ProgramData"),
    os.environ.get("CommonProgramFiles", ""),
    os.environ.get("CommonProgramFiles(x86)", ""),
    os.environ.get("CommonProgramW6432", ""),
    # User-critical root
    os.path.join(os.environ.get("SystemDrive", "C:"), os.sep, "Users"),
]

# Normalise & filter blanks
_ENV_RESTRICTED_NORM = set()
for _p in _ENV_RESTRICTED:
    if _p:
        _ENV_RESTRICTED_NORM.add(os.path.normcase(os.path.normpath(_p)))


# ---------------------------------------------------------------------------
# 2.  Pattern-based restricted folder names (matched on any drive)
# ---------------------------------------------------------------------------
# These names, when found as a top-level folder on *any* drive, are blocked.
_RESTRICTED_NAMES_LOWER = {
    "windows",
    "system32",
    "syswow64",
    "program files",
    "program files (x86)",
    "programdata",
    "users",
    "recovery",
    "system volume information",
    "$recycle.bin",
    "$windows.~bt",
    "$windows.~ws",
    "boot",
    "efi",
    "msocache",
    "documents and settings",
    "perflogs",
    "config.msi",
    "intel",
    "amd",
    "nvidia",
    "drivers",
    "driverstore",
}

# Regex: drive root itself  (e.g. "C:\")
_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:\\?$")


# ---------------------------------------------------------------------------
# 3.  Public API
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _is_drive_root(normed: str) -> bool:
    """True for 'c:\\' etc."""
    return bool(_DRIVE_ROOT_RE.match(normed.rstrip(os.sep) + os.sep))


def _is_or_under(normed: str, restricted: str) -> bool:
    """True if *normed* equals or is a child of *restricted*."""
    if normed == restricted:
        return True
    return normed.startswith(restricted + os.sep)


def is_folder_safe(path: str) -> bool:
    """Return True only if the path is safe for monitoring / organising.

    Blocks:
    * Drive roots (C:\\, D:\\, …)
    * Every path that is or lives under an environment-resolved restricted dir
    * Every path whose first directory component on *any* drive is in the
      restricted-names set
    """
    if not path:
        return False

    normed = _norm(path)

    # Block bare drive roots
    if _is_drive_root(normed):
        return False

    # Block environment-resolved paths
    for restricted in _ENV_RESTRICTED_NORM:
        if _is_or_under(normed, restricted):
            return False

    # Block by first-component name on every drive
    # e.g.  D:\Windows  or  E:\System Volume Information
    parts = normed.split(os.sep)
    # parts[0] is "c:", parts[1] is first folder name
    if len(parts) >= 2:
        first_dir = parts[1].lower()
        if first_dir in _RESTRICTED_NAMES_LOWER:
            return False

    return True


def get_block_reason(path: str) -> str:
    """Human-friendly reason why *path* is blocked (empty if safe)."""
    if not path:
        return "No path provided."

    normed = _norm(path)

    if _is_drive_root(normed):
        return (
            f"Operating on an entire drive root ({normed}) is not allowed.\n"
            "Please select a specific sub-folder."
        )

    for restricted in _ENV_RESTRICTED_NORM:
        if _is_or_under(normed, restricted):
            return (
                f"This path is inside a protected system directory:\n"
                f"  {restricted}\n\n"
                "Monitoring, organising, or deleting files here could "
                "damage your operating system."
            )

    parts = normed.split(os.sep)
    if len(parts) >= 2:
        first_dir = parts[1].lower()
        if first_dir in _RESTRICTED_NAMES_LOWER:
            return (
                f"The folder '{parts[1]}' is a known OS / system directory.\n\n"
                "Operations on system folders are blocked for safety."
            )

    return ""


def list_safe_drives() -> list:
    """Return a list of available drive letters (e.g. ['C:\\\\', 'D:\\\\'])."""
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.isdir(root):
            drives.append(root)
    return drives
