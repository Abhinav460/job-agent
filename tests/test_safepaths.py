import os

import pytest

from jobagent import safepaths


def test_inside_root_allowed(tmp_path):
    out = safepaths.write_text(tmp_path / "a" / "b.txt", "x", [tmp_path])
    assert out.read_text() == "x"
    assert out.stat().st_mode & 0o777 == 0o600
    assert out.parent.stat().st_mode & 0o777 == 0o700


def test_traversal_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(safepaths.PathOutsideWorkingDirs):
        safepaths.write_text(root / ".." / "escape.txt", "x", [root])
    assert not (tmp_path / "escape.txt").exists()


def test_symlink_escape_rejected(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    os.symlink(outside, root / "link")
    with pytest.raises(safepaths.PathOutsideWorkingDirs):
        safepaths.read_text(root / "link" / "secret.txt", [root])


def test_error_hides_full_path(tmp_path):
    with pytest.raises(safepaths.PathOutsideWorkingDirs) as err:
        safepaths.resolve_within("/etc/some-private-dir/file.txt", [tmp_path])
    assert "some-private-dir" not in str(err.value)


@pytest.mark.parametrize(
    "text, expected",
    [("Acme, Inc.", "acme-inc"), ("../../etc", "etc"), ("Café Ünïcode", "cafe-unicode"), ("", "unknown"), ("///", "unknown")],
)
def test_slug(text, expected):
    assert safepaths.slug(text) == expected
