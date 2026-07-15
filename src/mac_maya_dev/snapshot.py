"""Build clean, content-addressed source snapshots."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import MayaDevError

BLOCKED_NAMES = {
    ".maya-dev.toml",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
BLOCKED_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".ppk"}
PRIVATE_KEY_HEADERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN PGP PRIVATE KEY BLOCK-----",
    b"PuTTY-User-Key-File-",
)
WINDOWS_INVALID_CHARS = frozenset('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
DEPLOYMENT_MANIFEST = ".maya-dev-deployment.json"


@dataclass(frozen=True)
class Snapshot:
    content_hash: str
    archive: Path
    file_count: int
    git_commit: str | None
    git_head: str | None
    git_dirty: bool


def _git(source: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(source), *args], check=False, capture_output=True
    )


def _validate_windows_paths(files: list[Path]) -> None:
    seen: dict[str, Path] = {}
    for relative in files:
        if len(relative.parts) == 1 and relative.name.casefold() == DEPLOYMENT_MANIFEST.casefold():
            raise MayaDevError(f"Reserved deployment path: {relative}")
        normalized_parts: list[str] = []
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise MayaDevError(f"Unsafe deployment path: {relative}")
            if part.endswith((" ", ".")):
                raise MayaDevError(f"Windows path cannot end with space or dot: {relative}")
            if any(character in WINDOWS_INVALID_CHARS or ord(character) < 32 for character in part):
                raise MayaDevError(f"Invalid Windows path characters: {relative}")
            device_name = part.split(".", 1)[0].upper()
            if device_name in WINDOWS_RESERVED_NAMES:
                raise MayaDevError(f"Reserved Windows path name: {relative}")
            normalized_parts.append(part.casefold())
        normalized = "/".join(normalized_parts)
        previous = seen.get(normalized)
        if previous is not None:
            raise MayaDevError(f"Windows path collision: {previous} and {relative}")
        seen[normalized] = relative


def _read_source_file(source: Path, relative: Path) -> bytes:
    """Read without following a symlink in any path component."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(source, directory_flags)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        with os.fdopen(file_fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise MayaDevError(f"Deployment path is not a regular file: {relative}")
            return handle.read()
    except OSError as exc:
        raise MayaDevError(f"Cannot safely read deployment path {relative}: {exc}") from exc
    finally:
        os.close(directory_fd)


def source_files(source: Path) -> list[Path]:
    if not (source / ".git").exists():
        raise MayaDevError(f"Source is not a Git checkout: {source}")
    result = _git(source, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if result.returncode != 0:
        raise MayaDevError(result.stderr.decode(errors="replace").strip() or "git ls-files failed")

    files: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8", errors="strict"))
        path = source / relative
        candidate = source
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise MayaDevError(f"Symlinks are not supported in deployments: {relative}")
        if not path.exists():
            continue
        if not path.is_file():
            continue
        lowered = relative.name.lower()
        if (
            lowered in BLOCKED_NAMES
            or lowered.startswith(".env")
            or relative.suffix.lower() in BLOCKED_SUFFIXES
        ):
            raise MayaDevError(f"Refusing to deploy possible secret file: {relative}")
        files.append(relative)
    files.sort(key=lambda item: item.as_posix())
    _validate_windows_paths(files)
    return files


def git_head(source: Path) -> str | None:
    result = _git(source, "rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    value = result.stdout.decode().strip()
    return value or None


def git_provenance(
    source: Path, file_hashes: dict[str, str]
) -> tuple[str | None, str | None, bool]:
    head = git_head(source)
    if head is None:
        return None, None, True
    archive = _git(source, "archive", "--format=tar", head)
    if archive.returncode != 0:
        return None, head, True

    head_hashes: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        for member in bundle.getmembers():
            if not member.isfile():
                continue
            extracted = bundle.extractfile(member)
            if extracted is None:
                continue
            head_hashes[member.name] = hashlib.sha256(extracted.read()).hexdigest()
    dirty = head_hashes != file_hashes
    return (head if not dirty else None), head, dirty


def build_snapshot(source: Path, output_dir: Path) -> Snapshot:
    files = source_files(source)
    if not files:
        raise MayaDevError(f"No deployable files found in {source}")

    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / f".maya-mcp-building-{uuid.uuid4().hex}.zip"
    digest = hashlib.sha256()
    file_hashes: dict[str, str] = {}
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for relative in files:
                # One read owns both the digest and archive, even if an editor saves mid-build.
                content = _read_source_file(source, relative)
                if any(header in content for header in PRIVATE_KEY_HEADERS):
                    raise MayaDevError(f"Refusing to deploy private key content: {relative}")
                path_bytes = relative.as_posix().encode("utf-8")
                digest.update(len(path_bytes).to_bytes(8, "big"))
                digest.update(path_bytes)
                digest.update(len(content).to_bytes(8, "big"))
                digest.update(content)
                file_hashes[relative.as_posix()] = hashlib.sha256(content).hexdigest()
                bundle.writestr(relative.as_posix(), content)

            content_hash = digest.hexdigest()[:16]
            commit, head, dirty = git_provenance(source, file_hashes)
            manifest = {
                "schema": 1,
                "content_hash": content_hash,
                "git_commit": commit,
                "git_head": head,
                "git_dirty": dirty,
                "file_count": len(files),
                "files": file_hashes,
                "created_at": datetime.now(UTC).isoformat(),
            }
            bundle.writestr(
                DEPLOYMENT_MANIFEST, json.dumps(manifest, indent=2) + "\n"
            )
        archive = output_dir / f"maya-mcp-{content_hash}.zip"
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)

    return Snapshot(content_hash, archive, len(files), commit, head, dirty)
