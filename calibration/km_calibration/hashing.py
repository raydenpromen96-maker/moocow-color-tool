"""Canonical JSON and SHA-256 helpers for immutable calibration artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from stat import S_ISLNK, S_ISREG
from typing import Any

from .errors import DatasetValidationError


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_link_or_reparse(path_stat: os.stat_result) -> bool:
    return S_ISLNK(path_stat.st_mode) or bool(
        getattr(path_stat, "st_file_attributes", 0) & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
    )


def _assert_no_link_components(path: Path, trusted_root: Path | None) -> None:
    if trusted_root is None:
        return
    try:
        relative_path = path.relative_to(trusted_root)
    except ValueError as error:
        raise DatasetValidationError(f"Artifact path must stay within {trusted_root}: {path}") from error
    current = trusted_root
    for part in relative_path.parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except OSError as error:
            raise DatasetValidationError(f"Cannot inspect artifact path {current}: {error}") from error
        if _is_link_or_reparse(current_stat):
            raise DatasetValidationError(f"Artifact path must not traverse a link or reparse point: {current}")


def _read_regular_file_bytes(path: Path, *, trusted_root: Path | None = None) -> bytes:
    """Read one opened regular, unlinked file after checking its actual descriptor."""
    _assert_no_link_components(path, trusted_root)
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise DatasetValidationError(f"Cannot inspect artifact {path}: {error}") from error
    if _is_link_or_reparse(path_stat) or not S_ISREG(path_stat.st_mode):
        raise DatasetValidationError(f"Artifact must be a regular non-link file: {path}")
    if path_stat.st_nlink > 1:
        raise DatasetValidationError(f"Artifact must not have multiple hard links: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DatasetValidationError(f"Cannot open artifact {path}: {error}") from error
    try:
        with os.fdopen(descriptor, "rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            if not S_ISREG(opened_stat.st_mode):
                raise DatasetValidationError(f"Artifact must be a regular file: {path}")
            if opened_stat.st_nlink > 1:
                raise DatasetValidationError(f"Artifact must not have multiple hard links: {path}")
            return handle.read()
    except OSError as error:
        raise DatasetValidationError(f"Cannot read artifact {path}: {error}") from error


def read_regular_file_snapshot(
    path: Path | str, *, trusted_root: Path | None = None
) -> tuple[bytes, str]:
    """Return bytes and SHA-256 from one checked regular-file open.

    This is the raw-file counterpart to :func:`read_verified_json`: callers
    that need to bind non-JSON evidence can retain the exact bytes that were
    checked for link/reparse/hard-link safety and hashed.
    """
    artifact_path = Path(path)
    raw_bytes = _read_regular_file_bytes(artifact_path, trusted_root=trusted_root)
    return raw_bytes, sha256_bytes(raw_bytes)


def _normalize_sha256(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DatasetValidationError(f"{label} must be a SHA-256 hex digest")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise DatasetValidationError(f"{label} must be a SHA-256 hex digest")
    return normalized


def _read_sidecar_digest(path: Path, *, required: bool, trusted_root: Path | None) -> str | None:
    sidecar = path.with_name(f"{path.name}.sha256")
    if not sidecar.exists():
        if required:
            raise DatasetValidationError(f"Missing SHA-256 sidecar for {path}")
        return None
    try:
        raw_sidecar = _read_regular_file_bytes(sidecar, trusted_root=trusted_root)
        expected = raw_sidecar.decode("ascii").split()[0]
    except (UnicodeDecodeError, IndexError, DatasetValidationError) as error:
        raise DatasetValidationError(f"Invalid SHA-256 sidecar for {path}") from error
    return _normalize_sha256(expected, f"SHA-256 sidecar for {path}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _parse_json_bytes(path: Path, raw_bytes: bytes) -> Any:
    try:
        text = raw_bytes.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value!r}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise DatasetValidationError(f"Cannot read JSON artifact {path}: {error}") from error


def read_verified_json(
    path: Path | str,
    *,
    expected_sha256: str | None = None,
    require_sidecar: bool = False,
    trusted_root: Path | None = None,
) -> tuple[Any, str]:
    """Verify and parse one immutable JSON byte snapshot.

    Callers must supply either an expected digest or a mandatory sidecar. The
    JSON value is parsed directly from the bytes that produced the returned
    digest, closing the hash-then-reopen mismatch for one process invocation.
    """
    if expected_sha256 is None and not require_sidecar:
        raise ValueError("read_verified_json requires expected_sha256 or require_sidecar=True")
    artifact_path = Path(path)
    normalized_expected = (
        _normalize_sha256(expected_sha256, f"Expected SHA-256 for {artifact_path}")
        if expected_sha256 is not None
        else None
    )
    sidecar_expected = _read_sidecar_digest(
        artifact_path,
        required=require_sidecar,
        trusted_root=trusted_root,
    )
    if normalized_expected is not None and sidecar_expected is not None and normalized_expected != sidecar_expected:
        raise DatasetValidationError(f"Conflicting SHA-256 expectations for {artifact_path}")

    raw_bytes, actual = read_regular_file_snapshot(
        artifact_path, trusted_root=trusted_root
    )
    expected = normalized_expected or sidecar_expected
    if actual != expected:
        raise DatasetValidationError(
            f"SHA-256 mismatch for {artifact_path}: expected {expected}, got {actual}"
        )
    return _parse_json_bytes(artifact_path, raw_bytes), actual


def read_json(path: Path) -> Any:
    return _parse_json_bytes(path, _read_regular_file_bytes(path))


def write_json_with_sha256(path: Path, value: Any) -> str:
    """Write canonical JSON plus a sidecar containing the exact file SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    digest = sha256_file(path)
    path.with_name(f"{path.name}.sha256").write_text(
        f"{digest}  {path.name}\n", encoding="ascii"
    )
    return digest


def verify_sha256_sidecar(path: Path, *, required: bool = True) -> str:
    artifact_path = Path(path)
    expected = _read_sidecar_digest(artifact_path, required=required, trusted_root=None)
    raw_bytes = _read_regular_file_bytes(artifact_path)
    actual = sha256_bytes(raw_bytes)
    if expected is None:
        return actual
    if actual != expected:
        raise DatasetValidationError(
            f"SHA-256 mismatch for {artifact_path}: expected {expected}, got {actual}"
        )
    return actual
