from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from mac_maya_dev.errors import MayaDevError
from mac_maya_dev.snapshot import _validate_windows_paths, build_snapshot, source_files


def test_snapshot_includes_dirty_files_and_ignores_gitignored(
    source_repo: Path, tmp_path: Path
) -> None:
    (source_repo / "src" / "server.py").write_text("VALUE = 2\n", encoding="utf-8")
    (source_repo / "new.py").write_text("NEW = True\n", encoding="utf-8")
    (source_repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (source_repo / "ignored.txt").write_text("ignore me", encoding="utf-8")

    snapshot = build_snapshot(source_repo, tmp_path / "out")

    with zipfile.ZipFile(snapshot.archive) as bundle:
        names = bundle.namelist()
        assert "src/server.py" in names
        assert "new.py" in names
        assert ".gitignore" in names
        assert "ignored.txt" not in names
        manifest = json.loads(bundle.read(".maya-dev-deployment.json"))
    assert manifest["content_hash"] == snapshot.content_hash
    assert manifest["git_commit"] == snapshot.git_commit
    assert manifest["git_head"] == snapshot.git_head
    assert manifest["git_dirty"] is True
    assert snapshot.git_commit is None
    assert manifest["files"]["src/server.py"] == __import__("hashlib").sha256(
        b"VALUE = 2\n"
    ).hexdigest()


def test_snapshot_hash_is_content_addressed(source_repo: Path, tmp_path: Path) -> None:
    first = build_snapshot(source_repo, tmp_path / "one")
    second = build_snapshot(source_repo, tmp_path / "two")
    assert first.content_hash == second.content_hash

    (source_repo / "src" / "server.py").write_text("VALUE = 3\n", encoding="utf-8")
    third = build_snapshot(source_repo, tmp_path / "three")
    assert third.content_hash != first.content_hash


def test_clean_snapshot_records_reproducible_commit(source_repo: Path, tmp_path: Path) -> None:
    snapshot = build_snapshot(source_repo, tmp_path / "out")
    assert snapshot.git_dirty is False
    assert snapshot.git_commit == snapshot.git_head
    assert snapshot.git_commit is not None


def test_snapshot_hash_framing_is_unambiguous(source_repo: Path, tmp_path: Path) -> None:
    (source_repo / "a").write_bytes(b"x\0b\0y")
    first = build_snapshot(source_repo, tmp_path / "one")

    (source_repo / "a").write_bytes(b"x")
    (source_repo / "b").write_bytes(b"y")
    second = build_snapshot(source_repo, tmp_path / "two")

    assert first.content_hash != second.content_hash


def test_snapshot_archive_uses_the_bytes_that_were_hashed(
    source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = source_repo / "src" / "server.py"
    from mac_maya_dev.snapshot import _read_source_file

    original_content = _read_source_file(source_repo, Path("src/server.py"))
    changed = False

    def read_then_change(source: Path, relative: Path) -> bytes:
        nonlocal changed
        content = _read_source_file(source, relative)
        if relative == Path("src/server.py") and not changed:
            changed = True
            target.write_text("VALUE = 999\n", encoding="utf-8")
        return content

    monkeypatch.setattr("mac_maya_dev.snapshot._read_source_file", read_then_change)
    snapshot = build_snapshot(source_repo, tmp_path / "out")

    with zipfile.ZipFile(snapshot.archive) as bundle:
        assert bundle.read("src/server.py") == original_content
    assert target.read_text(encoding="utf-8") == "VALUE = 999\n"


@pytest.mark.parametrize(
    "name",
    [".env", ".env.example", "private.pem", "private.ppk", "id_rsa", "id_ecdsa", "id_dsa"],
)
def test_possible_secret_blocks_deployment(source_repo: Path, name: str) -> None:
    (source_repo / name).write_text("secret", encoding="utf-8")
    with pytest.raises(MayaDevError, match="possible secret"):
        source_files(source_repo)


def test_private_key_content_blocks_deployment(source_repo: Path, tmp_path: Path) -> None:
    (source_repo / "innocent-name.txt").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n", encoding="utf-8"
    )
    with pytest.raises(MayaDevError, match="private key content"):
        build_snapshot(source_repo, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.zip"))


def test_putty_private_key_content_blocks_deployment(source_repo: Path, tmp_path: Path) -> None:
    (source_repo / "innocent-name.txt").write_text(
        "PuTTY-User-Key-File-3: ssh-ed25519\n", encoding="utf-8"
    )
    with pytest.raises(MayaDevError, match="private key content"):
        build_snapshot(source_repo, tmp_path / "out")


def test_non_git_source_fails(tmp_path: Path) -> None:
    with pytest.raises(MayaDevError, match="not a Git checkout"):
        source_files(tmp_path)


def test_symlink_fails(source_repo: Path) -> None:
    link = source_repo / "linked.py"
    link.symlink_to(source_repo / "src" / "server.py")
    subprocess.run(["git", "-C", str(source_repo), "add", "linked.py"], check=True)
    with pytest.raises(MayaDevError, match="Symlinks"):
        source_files(source_repo)


def test_dangling_symlink_fails(source_repo: Path) -> None:
    link = source_repo / "missing.py"
    link.symlink_to(source_repo / "does-not-exist.py")
    subprocess.run(["git", "-C", str(source_repo), "add", "missing.py"], check=True)
    with pytest.raises(MayaDevError, match="Symlinks"):
        source_files(source_repo)


def test_symlinked_parent_directory_fails(source_repo: Path, tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "settings.py").write_text("SECRET = True\n", encoding="utf-8")
    linked_dir = source_repo / "linked-dir"
    linked_dir.mkdir()
    linked_file = linked_dir / "settings.py"
    linked_file.write_text("SAFE = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source_repo), "add", "linked-dir/settings.py"], check=True)
    linked_file.unlink()
    linked_dir.rmdir()
    linked_dir.symlink_to(external, target_is_directory=True)
    with pytest.raises(MayaDevError, match="Symlinks"):
        source_files(source_repo)


def test_windows_case_collision_fails(source_repo: Path) -> None:
    with pytest.raises(MayaDevError, match="Windows path collision"):
        _validate_windows_paths([Path("Foo.py"), Path("foo.py")])


def test_generated_manifest_path_is_reserved() -> None:
    with pytest.raises(MayaDevError, match="Reserved deployment path"):
        _validate_windows_paths([Path(".MAYA-DEV-DEPLOYMENT.JSON")])


@pytest.mark.parametrize("name", ["CON.txt", "trailing.", "bad:name.py"])
def test_invalid_windows_name_fails(source_repo: Path, name: str) -> None:
    (source_repo / name).write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(MayaDevError, match=r"Windows path|Invalid Windows|Reserved Windows"):
        source_files(source_repo)
