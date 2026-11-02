"""Real NTFS ACL checks use only disposable fictional files."""

import os
from pathlib import Path
import subprocess

import pytest

from jobagent import private_storage


@pytest.mark.skipif(os.name != "nt", reason="requires Windows NTFS ACLs")
def test_windows_repairs_existing_acl_and_new_files_inherit_private_access(tmp_path):
    home = tmp_path / "fictional-home"
    child = home / "nested"
    child.mkdir(parents=True)
    (home / private_storage.HOME_MARKER_NAME).write_text("jobagent-private-home-v1\n")
    old_file = child / "fictional.txt"
    old_file.write_text("fictional")
    # Give Everyone explicit read access to exercise repair of an existing tree.
    granted = subprocess.run(
        ["icacls.exe", str(home), "/grant", "*S-1-1-0:(OI)(CI)R"],
        capture_output=True, check=False,
    )
    assert granted.returncode == 0

    private_storage.secure_home(home)
    new_file = child / "new.txt"
    new_file.write_text("fictional")
    # A new command must repair changes made after this process first validated
    # the home, even though internal request helpers may use the trusted cache.
    granted_again = subprocess.run(
        ["icacls.exe", str(old_file), "/grant", "*S-1-1-0:R"],
        capture_output=True, check=False,
    )
    assert granted_again.returncode == 0
    private_storage.secure_home(home, force=True)
    environment = os.environ.copy()
    environment["JOBAGENT_ACL_TEST_HOME"] = str(home)
    verify = r"""
$ErrorActionPreference = 'Stop'
$target = [Environment]::GetEnvironmentVariable('JOBAGENT_ACL_TEST_HOME', 'Process')
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($item in @($target, (Join-Path $target 'nested'),
                    (Join-Path $target 'nested\fictional.txt'),
                    (Join-Path $target 'nested\new.txt'))) {
    $acl = Get-Acl -LiteralPath $item
    $rules = @($acl.GetAccessRules($true, $true,
        [System.Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $sid -or
        $rules[0].AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
        ($rules[0].FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -ne
            [System.Security.AccessControl.FileSystemRights]::FullControl) { exit 1 }
}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", verify],
        env=environment, capture_output=True, check=False,
    )
    assert result.returncode == 0


@pytest.mark.skipif(os.name != "nt", reason="requires Windows")
def test_windows_acl_failure_exposes_no_private_path(tmp_path, monkeypatch):
    marker = "fictional-private-marker"
    home = tmp_path / marker

    def failed(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1)

    monkeypatch.setattr(private_storage.subprocess, "run", failed)
    with pytest.raises(private_storage.PrivateStorageError) as error:
        private_storage.secure_home(home)
    assert marker not in str(error.value)
    assert not home.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows reparse points")
def test_windows_refuses_symlink_inside_private_home(tmp_path):
    home = tmp_path / "fictional-home"
    outside = tmp_path / "outside"
    home.mkdir()
    (home / private_storage.HOME_MARKER_NAME).write_text("jobagent-private-home-v1\n")
    outside.mkdir()
    try:
        (home / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        environment = os.environ.copy()
        environment["JOBAGENT_ACL_TEST_LINK"] = str(home / "link")
        environment["JOBAGENT_ACL_TEST_OUTSIDE"] = str(outside)
        junction = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "New-Item -ItemType Junction -Path $env:JOBAGENT_ACL_TEST_LINK "
             "-Target $env:JOBAGENT_ACL_TEST_OUTSIDE | Out-Null"],
            env=environment, capture_output=True, check=False,
        )
        if junction.returncode != 0:
            pytest.skip("creating a Windows reparse point needs permission")
    with pytest.raises(private_storage.PrivateStorageError):
        private_storage.secure_home(home)
    assert outside.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows")
@pytest.mark.parametrize("candidate", ["profile", "profile-parent", "drive", "unrelated"])
def test_windows_rejects_nondedicated_roots_before_acl_changes(tmp_path, monkeypatch, candidate):
    if candidate == "profile":
        home = Path.home()
    elif candidate == "profile-parent":
        home = Path.home().parent
    elif candidate == "drive":
        home = Path(Path.home().anchor)
    else:
        home = tmp_path / "unrelated-documents"
        home.mkdir()
        (home / "notes.txt").write_text("fictional")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ACL command must not run for a non-dedicated directory")

    monkeypatch.setattr(private_storage.subprocess, "run", forbidden)
    with pytest.raises(private_storage.PrivateStorageError, match="dedicated"):
        private_storage.secure_home(home, force=True)
    if candidate == "unrelated":
        assert (home / "notes.txt").read_text() == "fictional"


@pytest.mark.skipif(os.name != "nt", reason="requires NTFS hard links")
def test_windows_refuses_hard_link_before_acl_changes(tmp_path):
    home = tmp_path / "fictional-home"
    home.mkdir()
    (home / private_storage.HOME_MARKER_NAME).write_text("jobagent-private-home-v1\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("fictional")
    os.link(outside, home / "linked.txt")
    assert outside.stat().st_nlink > 1
    with pytest.raises(private_storage.PrivateStorageError, match="link"):
        private_storage.secure_home(home, force=True)
    assert outside.read_text() == "fictional"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows")
def test_acl_helper_uses_absolute_system_powershell():
    executable = private_storage._powershell_path()
    assert executable.is_absolute() and executable.is_file()
    assert executable.name.lower() == "powershell.exe"
