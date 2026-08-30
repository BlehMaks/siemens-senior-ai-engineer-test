from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import agent_api.security.key_admin as key_admin


def _cleanup_entries(directory: Path) -> list[Path]:
    return list(directory.glob(".api-key-cleanup-*"))


def test_output_is_created_directly_without_a_link_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"

    def forbidden_link(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError(
            "direct publication must not use a replaceable link source"
        )

    monkeypatch.setattr(os, "link", forbidden_link)

    key_admin._write_plaintext_file(output, "temporary-key")

    assert output.read_text(encoding="utf-8") == "temporary-key\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert _cleanup_entries(tmp_path) == []


def test_concurrent_destination_creation_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    real_open = os.open
    injected = False

    def create_destination_before_open(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injected
        if target == output.name and dir_fd is not None and not injected:
            injected = True
            concurrent = real_open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                os.write(concurrent, b"concurrent-owner\n")
            finally:
                os.close(concurrent)
        return real_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", create_destination_before_open)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert output.read_text(encoding="utf-8") == "concurrent-owner\n"
    assert _cleanup_entries(tmp_path) == []


def test_parent_rename_during_final_fsync_removes_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "output"
    parent.mkdir()
    displaced = tmp_path / "displaced"
    output = parent / "generated.key"
    real_fsync = os.fsync
    renamed = False

    def rename_parent(descriptor: int) -> None:
        nonlocal renamed
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            parent.rename(displaced)
            parent.mkdir()
            renamed = True
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", rename_parent)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert renamed
    assert not output.exists()
    assert not (displaced / output.name).exists()
    cleanup = _cleanup_entries(displaced)
    assert len(cleanup) == 1
    assert cleanup[0].read_bytes() == b""


def test_failure_keeps_an_empty_quarantine_instead_of_unlinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    real_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    def forbidden_unlink(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("quarantine cleanup must not unlink a replaceable path")

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(os, "unlink", forbidden_unlink)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert not output.exists()
    cleanup = _cleanup_entries(tmp_path)
    assert len(cleanup) == 1
    assert cleanup[0].read_bytes() == b""


def test_special_permission_bits_fail_without_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    real_fstat = os.fstat

    def report_special_bit(descriptor: int) -> os.stat_result:
        current = real_fstat(descriptor)
        if stat.S_ISREG(current.st_mode):
            values = list(current)
            values[stat.ST_MODE] = current.st_mode | stat.S_ISUID
            return os.stat_result(values)
        return current

    monkeypatch.setattr(os, "fstat", report_special_bit)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert not output.exists()
    cleanup = _cleanup_entries(tmp_path)
    assert len(cleanup) == 1
    assert cleanup[0].read_bytes() == b""


def test_close_error_after_release_scrubs_the_owned_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    real_close = os.close
    injected = False

    def close_then_report_error(descriptor: int) -> None:
        nonlocal injected
        current = os.fstat(descriptor)
        if stat.S_ISREG(current.st_mode) and not injected:
            injected = True
            real_close(descriptor)
            raise OSError(errno.EIO, "injected close failure after release")
        real_close(descriptor)

    monkeypatch.setattr(os, "close", close_then_report_error)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert injected
    assert not output.exists()
    cleanup = _cleanup_entries(tmp_path)
    assert len(cleanup) == 1
    assert cleanup[0].read_bytes() == b""


def test_close_error_after_reuse_does_not_touch_the_new_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_bytes(b"must-not-be-truncated\n")
    real_close = os.close
    reused = False

    def close_release_and_reuse(descriptor: int) -> None:
        nonlocal reused
        current = os.fstat(descriptor)
        if stat.S_ISREG(current.st_mode) and not reused:
            real_close(descriptor)
            replacement = os.open(unrelated, os.O_WRONLY)
            assert replacement == descriptor
            reused = True
            raise OSError(errno.EIO, "injected close failure after reuse")
        real_close(descriptor)

    monkeypatch.setattr(os, "close", close_release_and_reuse)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert reused
    assert unrelated.read_bytes() == b"must-not-be-truncated\n"
    cleanup = _cleanup_entries(tmp_path)
    assert len(cleanup) == 1
    assert cleanup[0].read_bytes() == b""
