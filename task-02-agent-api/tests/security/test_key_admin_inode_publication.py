from __future__ import annotations

import ctypes
import errno
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import agent_api.security.key_admin as key_admin


def _cleanup_entries(directory: Path) -> list[Path]:
    return list(directory.glob(".api-key-cleanup-*"))


def _cleanup_payload(entry: Path) -> bytes:
    return (entry / "owned").read_bytes() if entry.is_dir() else entry.read_bytes()


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
    assert _cleanup_payload(cleanup[0]) == b""


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
    assert _cleanup_payload(cleanup[0]) == b""


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
    assert _cleanup_payload(cleanup[0]) == b""


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
    assert _cleanup_payload(cleanup[0]) == b""


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
    assert _cleanup_payload(cleanup[0]) == b""


def test_close_error_scrubs_a_created_inode_moved_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    displaced = tmp_path / "displaced.key"
    replacement_content = b"concurrent-owner\n"
    real_close = os.close
    real_open = os.open
    replacement_descriptor = -1
    injected = False

    def close_replace_and_reuse(descriptor: int) -> None:
        nonlocal injected, replacement_descriptor
        current = os.fstat(descriptor)
        if stat.S_ISREG(current.st_mode) and not injected:
            injected = True
            real_close(descriptor)
            output.rename(displaced)
            output.write_bytes(replacement_content)
            replacement_descriptor = real_open(output, os.O_WRONLY)
            assert replacement_descriptor == descriptor
            raise OSError(errno.EIO, "injected close failure after replacement")
        real_close(descriptor)

    monkeypatch.setattr(os, "close", close_replace_and_reuse)

    try:
        with pytest.raises(SystemExit, match="protected output file"):
            key_admin._write_plaintext_file(output, "temporary-key")
    finally:
        if replacement_descriptor >= 0:
            real_close(replacement_descriptor)

    assert injected
    assert output.read_bytes() == replacement_content
    assert displaced.read_bytes() == b""
    assert _cleanup_entries(tmp_path) == []


def test_late_parent_replacement_is_rejected_and_scrubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "output"
    parent.mkdir()
    displaced = tmp_path / "displaced"
    output = parent / "generated.key"
    replacement_content = b"concurrent-owner\n"
    real_stat = os.stat
    relative_stats = 0

    def replace_parent_before_final_stat(
        target: str | bytes | int | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal relative_stats
        if target == output.name and dir_fd is not None:
            relative_stats += 1
            if relative_stats == 2:
                parent.rename(displaced)
                parent.mkdir()
                output.write_bytes(replacement_content)
        return real_stat(
            target,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "stat", replace_parent_before_final_stat)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert relative_stats >= 2
    assert output.read_bytes() == replacement_content
    assert not (displaced / output.name).exists()
    cleanup = _cleanup_entries(displaced)
    assert len(cleanup) == 1
    assert _cleanup_payload(cleanup[0]) == b""


def test_quarantine_reservation_does_not_overwrite_a_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    foreign_content = b"foreign-owner\n"
    real_fsync = os.fsync
    real_mkdir = os.mkdir
    collision: Path | None = None

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    def create_collision(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal collision
        if (
            collision is None
            and isinstance(target, str)
            and target.startswith(".api-key-cleanup-")
            and dir_fd is not None
        ):
            foreign = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                os.write(foreign, foreign_content)
            finally:
                os.close(foreign)
            collision = tmp_path / target
            raise FileExistsError(errno.EEXIST, "injected quarantine collision")
        real_mkdir(target, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(os, "mkdir", create_collision)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert collision is not None
    assert collision.read_bytes() == foreign_content
    cleanup_directories = [
        entry for entry in _cleanup_entries(tmp_path) if entry.is_dir()
    ]
    assert len(cleanup_directories) == 1
    assert _cleanup_payload(cleanup_directories[0]) == b""


def test_source_replacement_after_reservation_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    displaced = tmp_path / "created.key"
    foreign_content = b"foreign-owner\n"
    real_fsync = os.fsync
    real_mkdir = os.mkdir
    replaced = False

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    def replace_source_after_reservation(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal replaced
        real_mkdir(target, mode, dir_fd=dir_fd)
        if (
            not replaced
            and isinstance(target, str)
            and target.startswith(".api-key-cleanup-")
            and dir_fd is not None
        ):
            output.rename(displaced)
            output.write_bytes(foreign_content)
            replaced = True

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(os, "mkdir", replace_source_after_reservation)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert replaced
    assert output.read_bytes() == foreign_content
    assert displaced.read_bytes() == b""
    cleanup = _cleanup_entries(tmp_path)
    assert len(cleanup) == 1
    assert _cleanup_payload(cleanup[0]) == b""


def test_owned_child_collision_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    foreign_content = b"foreign-owner\n"
    real_fsync = os.fsync
    real_rename = key_admin._rename_noreplace
    collision: Path | None = None

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    def collide_before_rename(
        source_descriptor: int,
        source: str,
        destination_descriptor: int,
        destination: str,
    ) -> bool:
        nonlocal collision
        if collision is None and destination.endswith("/owned"):
            foreign = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=destination_descriptor,
            )
            try:
                os.write(foreign, foreign_content)
            finally:
                os.close(foreign)
            collision = tmp_path / destination
        return real_rename(
            source_descriptor,
            source,
            destination_descriptor,
            destination,
        )

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(key_admin, "_rename_noreplace", collide_before_rename)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert collision is not None
    assert collision.read_bytes() == foreign_content
    cleanup = _cleanup_entries(tmp_path)
    assert len(cleanup) == 2
    assert sorted(_cleanup_payload(entry) for entry in cleanup) == [
        b"",
        foreign_content,
    ]


def test_restrictive_umask_keeps_documented_cleanup_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    real_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    previous_umask = os.umask(0o777)
    try:
        with pytest.raises(SystemExit, match="protected output file"):
            key_admin._write_plaintext_file(output, "temporary-key")
    finally:
        os.umask(previous_umask)

    cleanup = _cleanup_entries(tmp_path)
    assert len(cleanup) == 1
    assert stat.S_IMODE(cleanup[0].stat().st_mode) == 0o700
    assert stat.S_IMODE((cleanup[0] / "owned").stat().st_mode) == 0o600
    assert _cleanup_payload(cleanup[0]) == b""


@pytest.mark.parametrize("failure", [OSError, NotImplementedError])
def test_cleanup_chmod_wrapper_failure_keeps_quarantine_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    output = tmp_path / "generated.key"
    real_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    def fail_cleanup_chmod(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        del mode, dir_fd, follow_symlinks
        if isinstance(target, str) and target.startswith(".api-key-cleanup-"):
            raise failure(errno.EIO, "injected cleanup chmod failure")
        raise AssertionError(f"unexpected chmod target: {target!r}")

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(key_admin.os, "chmod", fail_cleanup_chmod)
    previous_umask = os.umask(0o777)
    try:
        with pytest.raises(SystemExit, match="protected output file"):
            key_admin._write_plaintext_file(output, "temporary-key")
    finally:
        os.umask(previous_umask)

    assert not output.exists()
    cleanup = _cleanup_entries(tmp_path)
    assert len(cleanup) == 1
    assert stat.S_IMODE(cleanup[0].stat().st_mode) == 0o700
    assert stat.S_IMODE((cleanup[0] / "owned").stat().st_mode) == 0o600


@pytest.mark.parametrize("failure", [OSError, NotImplementedError])
def test_marker_fchmod_wrapper_failure_keeps_documented_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    output = tmp_path / "generated.key"
    displaced = tmp_path / "displaced.key"
    real_fsync = os.fsync
    real_mkdir = os.mkdir
    real_open = os.open
    real_fchmod = os.fchmod
    marker_descriptor = -1
    moved = False

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    def move_source_after_reservation(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal moved
        real_mkdir(target, mode, dir_fd=dir_fd)
        if (
            not moved
            and isinstance(target, str)
            and target.startswith(".api-key-cleanup-")
            and dir_fd is not None
        ):
            os.rename(
                output.name,
                displaced.name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            moved = True

    def capture_marker_open(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal marker_descriptor
        opened = real_open(target, flags, mode, dir_fd=dir_fd)
        if isinstance(target, str) and target.endswith("/owned"):
            marker_descriptor = opened
        return opened

    def fail_marker_fchmod(descriptor: int, mode: int) -> None:
        if descriptor == marker_descriptor:
            raise failure(errno.EIO, "injected marker fchmod failure")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(key_admin.os, "mkdir", move_source_after_reservation)
    monkeypatch.setattr(key_admin.os, "open", capture_marker_open)
    monkeypatch.setattr(key_admin.os, "fchmod", fail_marker_fchmod)
    previous_umask = os.umask(0o777)
    try:
        with pytest.raises(SystemExit, match="protected output file"):
            key_admin._write_plaintext_file(output, "temporary-key")
    finally:
        os.umask(previous_umask)

    cleanup = _cleanup_entries(tmp_path)
    assert moved and marker_descriptor >= 0 and len(cleanup) == 1
    assert stat.S_IMODE(cleanup[0].stat().st_mode) == 0o700
    assert stat.S_IMODE((cleanup[0] / "owned").stat().st_mode) == 0o600


def test_permission_repair_preserves_rebound_foreign_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    quarantine_name = ".api-key-cleanup-race"
    quarantine = tmp_path / quarantine_name
    displaced = tmp_path / "displaced-cleanup"
    real_chmod = os.chmod
    real_fsync = os.fsync
    real_mkdir = os.mkdir

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    def replace_reservation_then_fail(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        del mode, follow_symlinks
        assert target == quarantine_name and dir_fd is not None
        os.rename(
            quarantine_name,
            displaced.name,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
        )
        real_mkdir(quarantine_name, 0o755, dir_fd=dir_fd)
        real_chmod(quarantine_name, 0o755, dir_fd=dir_fd)
        raise OSError(errno.EIO, "injected cleanup chmod failure")

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(key_admin.secrets, "token_hex", lambda _: "race")
    monkeypatch.setattr(key_admin.os, "chmod", replace_reservation_then_fail)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert displaced.is_dir() and quarantine.is_dir()
    assert stat.S_IMODE(quarantine.stat().st_mode) == 0o755
    assert not (quarantine / "owned").exists()


def test_linux_without_renameat2_symbol_uses_raw_syscall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"source")

    class FakeSyscall:
        restype: type[ctypes.c_long] | None = None

        def __call__(
            self,
            number: int,
            source_descriptor: int,
            source_pointer: ctypes.c_char_p,
            destination_descriptor: int,
            destination_pointer: ctypes.c_char_p,
            flag: int,
        ) -> int:
            assert number > 0 and flag == 1
            source_name = os.fsdecode(source_pointer.value or b"")
            destination_name = os.fsdecode(destination_pointer.value or b"")
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_descriptor,
                dst_dir_fd=destination_descriptor,
            )
            return 0

    fake_syscall = FakeSyscall()
    fake_libc = type("FakeLibc", (), {"syscall": fake_syscall})()
    monkeypatch.setattr(key_admin.sys, "platform", "linux")
    monkeypatch.setattr(
        key_admin.ctypes,
        "CDLL",
        lambda *args, **kwargs: fake_libc,
    )
    descriptor = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        assert key_admin._rename_noreplace(
            descriptor,
            source.name,
            descriptor,
            destination.name,
        )
    finally:
        os.close(descriptor)

    assert not source.exists()
    assert destination.read_bytes() == b"source"


def test_unsupported_linux_syscall_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"source")

    class UnsupportedSyscall:
        restype: type[ctypes.c_long] | None = None

        def __call__(self, *args: object) -> int:
            del args
            ctypes.set_errno(errno.ENOSYS)
            return -1

    fake_libc = type("FakeLibc", (), {"syscall": UnsupportedSyscall()})()
    monkeypatch.setattr(key_admin.sys, "platform", "linux")
    monkeypatch.setattr(
        key_admin.ctypes,
        "CDLL",
        lambda *args, **kwargs: fake_libc,
    )
    descriptor = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(OSError) as raised:
            key_admin._rename_noreplace(
                descriptor,
                source.name,
                descriptor,
                destination.name,
            )
    finally:
        os.close(descriptor)

    assert raised.value.errno == errno.ENOTSUP
    assert source.read_bytes() == b"source"
    assert not destination.exists()


def test_failed_nonblocking_quarantine_open_restores_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    output.write_bytes(b"owned")
    real_open = os.open
    observed_flags = 0

    def fail_quarantine_open(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal observed_flags
        if (
            isinstance(target, str)
            and target.endswith("/owned")
            and observed_flags == 0
        ):
            observed_flags = flags
            raise OSError(errno.ENXIO, "injected special-file open failure")
        return real_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(key_admin.os, "open", fail_quarantine_open)
    descriptor = real_open(
        tmp_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        current = os.stat(output.name, dir_fd=descriptor, follow_symlinks=False)
        key_admin._quarantine_owned_output(
            descriptor,
            name=output.name,
            expected=(current.st_dev, current.st_ino),
        )
    finally:
        os.close(descriptor)

    assert observed_flags & getattr(os, "O_NONBLOCK", 0)
    assert output.read_bytes() == b"owned"
    cleanup = _cleanup_entries(tmp_path)
    assert len(cleanup) == 1
    assert _cleanup_payload(cleanup[0]) == b""


def test_symlink_race_during_move_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated.key"
    displaced = tmp_path / "created.key"
    foreign_target = tmp_path / "foreign-target"
    foreign_target.write_bytes(b"foreign-owner\n")
    real_fsync = os.fsync
    real_rename = key_admin._rename_noreplace
    raced = False

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)

    def replace_source_during_move(
        source_descriptor: int,
        source: str,
        destination_descriptor: int,
        destination: str,
    ) -> bool:
        nonlocal raced
        if not raced and destination.endswith("/owned"):
            os.rename(
                source,
                displaced.name,
                src_dir_fd=source_descriptor,
                dst_dir_fd=source_descriptor,
            )
            os.symlink(foreign_target, source, dir_fd=source_descriptor)
            raced = True
        return real_rename(
            source_descriptor,
            source,
            destination_descriptor,
            destination,
        )

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(key_admin, "_rename_noreplace", replace_source_during_move)

    with pytest.raises(SystemExit, match="protected output file"):
        key_admin._write_plaintext_file(output, "temporary-key")

    assert raced and output.is_symlink()
    assert displaced.read_bytes() == b""
    assert output.read_bytes() == b"foreign-owner\n"
