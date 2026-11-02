"""Fail-closed private storage setup for the local Windows runtime.

Windows ignores the POSIX modes used elsewhere in this project. Before a
private command opens data, replace the DACL on the home and every existing
descendant with a single full-control ACE for the current user. Directories
pass that ACE to new files and subdirectories made by Qdrant and Chromium.
"""

from __future__ import annotations

import base64
import ctypes
import os
from pathlib import Path
import stat
import subprocess
import threading


class PrivateStorageError(RuntimeError):
    """Fixed diagnostic; never include the private path or command output."""


_secure_lock = threading.Lock()
_validated_roots: set[Path] = set()
HOME_MARKER_NAME = ".jobagent-home"
_HOME_MARKER_TEXT = "jobagent-private-home-v1\n"


_WINDOWS_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$privateTarget = [Environment]::GetEnvironmentVariable('JOBAGENT_ACL_TARGET', 'Process')
if ([string]::IsNullOrWhiteSpace($privateTarget)) { throw 'missing target' }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User
if ($null -eq $sid) { throw 'missing user SID' }
[System.IO.Directory]::CreateDirectory($privateTarget) | Out-Null
$root = [System.IO.DirectoryInfo]::new($privateTarget)
$pending = New-Object 'System.Collections.Generic.Stack[System.IO.FileSystemInfo]'
$pending.Push($root)
while ($pending.Count -gt 0) {
    $item = $pending.Pop()
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'reparse point in private storage'
    }
    $isDirectory = $item -is [System.IO.DirectoryInfo]
    $acl = Get-Acl -LiteralPath $item.FullName -ErrorAction Stop
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleAll($rule) | Out-Null }
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    if ($isDirectory) {
        $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                       [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    $allow = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($allow)
    if ($isDirectory) {
        [System.IO.Directory]::SetAccessControl($item.FullName, $acl)
    } else {
        [System.IO.File]::SetAccessControl($item.FullName, $acl)
    }
    $check = Get-Acl -LiteralPath $item.FullName -ErrorAction Stop
    $rules = @($check.GetAccessRules($true, $true,
        [System.Security.Principal.SecurityIdentifier]))
    if (-not $check.AreAccessRulesProtected -or $rules.Count -ne 1 -or
        $rules[0].IdentityReference.Value -ne $sid.Value -or
        $rules[0].AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
        $rules[0].IsInherited -or
        ($rules[0].FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -ne
            [System.Security.AccessControl.FileSystemRights]::FullControl -or
        ($isDirectory -and $rules[0].InheritanceFlags -ne $inheritance)) {
        throw 'private ACL verification failed'
    }
    if ($isDirectory) {
        foreach ($child in $item.EnumerateFileSystemInfos()) { $pending.Push($child) }
    }
}
"""


def _powershell_path() -> Path:
    """Use Windows' own system directory, never executable search or PATH."""
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise PrivateStorageError("private Windows storage could not be secured")
    executable = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_absolute() or not executable.is_file():
        raise PrivateStorageError("private Windows storage could not be secured")
    return executable


def _preflight_tree(target: Path) -> bool:
    """Reject unsafe roots and links before changing any ACL in the tree.

    Existing custom homes require a marker. An absent custom home is treated as
    a new dedicated directory and receives the marker after ACL setup.
    """
    profile = Path.home().resolve()
    resolved = target.resolve(strict=False)
    if (resolved == Path(resolved.anchor) or resolved == profile
            or resolved in profile.parents or not target.parent.is_dir()):
        raise PrivateStorageError("choose a dedicated Job Agent private home")
    if not target.exists():
        return True
    if not target.is_dir():
        raise PrivateStorageError("choose a dedicated Job Agent private home")
    pending = [target]
    while pending:
        current = pending.pop()
        details = current.stat(follow_symlinks=False)
        if (details.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not current.is_dir() and details.st_nlink > 1):
            raise PrivateStorageError("private Windows storage contains a link")
        if current.is_dir():
            pending.extend(current.iterdir())
    marker = target / HOME_MARKER_NAME
    legacy_default = resolved == (profile / ".jobagent").resolve(strict=False)
    if not legacy_default and (not marker.is_file()
                               or marker.read_text(encoding="ascii") != _HOME_MARKER_TEXT):
        raise PrivateStorageError("choose a dedicated Job Agent private home")
    return not marker.exists()


def secure_home(home: Path, *, force: bool = False) -> None:
    """Repair and verify all existing private ACLs before use on Windows.

    The script receives its target through a process-local environment variable;
    neither a path nor PowerShell diagnostics are included in failure messages.
    POSIX callers retain their existing umask/chmod behavior.
    """
    if os.name != "nt":
        return
    target = Path(home).expanduser().absolute()
    with _secure_lock:
        if not force and target in _validated_roots:
            return
        try:
            needs_marker = _preflight_tree(target)
        except PrivateStorageError:
            raise
        except (OSError, UnicodeError, ValueError, RuntimeError):
            raise PrivateStorageError("private Windows storage could not be inspected") from None
        encoded = base64.b64encode(_WINDOWS_ACL_SCRIPT.encode("utf-16le")).decode("ascii")
        environment = os.environ.copy()
        environment["JOBAGENT_ACL_TARGET"] = str(target)
        try:
            result = subprocess.run(
                [str(_powershell_path()), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=120, check=False, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                raise PrivateStorageError("private Windows storage could not be secured")
        except (OSError, subprocess.TimeoutExpired):
            raise PrivateStorageError("private Windows storage could not be secured") from None
        if needs_marker:
            try:
                (target / HOME_MARKER_NAME).write_text(_HOME_MARKER_TEXT, encoding="ascii")
            except OSError:
                raise PrivateStorageError("private Windows storage could not be secured") from None
        _validated_roots.add(target)
