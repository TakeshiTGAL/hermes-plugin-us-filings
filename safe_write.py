"""Write one file without following a symlink.

Copied from hermes-plugin-sprite-sheet ``rewrite_files`` (836fa8c).
The home path is resolved first, so a parent symlink such as macOS ``/tmp``
is the real directory. Each component is then opened with ``O_NOFOLLOW``.
A symlink at the file, or a symlink under the home, is still refused.
Bytes go to a temporary file in that directory, then ``os.replace``.
The temporary name is not predictable. It is created with ``O_EXCL`` and mode 0600.
Replace waits until that inode has one link. An existing hard link is named when it is detached.
"""
from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path

HARD_LINK_CUT = "Cut the hard link this file shared with another name"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class WriteRefused(Exception):
    def __init__(self, kind: str, detail: str):
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


def _close_all(fds: list[int]) -> None:
    for fd in reversed(fds):
        try:
            os.close(fd)
        except OSError:
            pass


def _open_child(parent_fd: int, name: str) -> tuple[int | None, str]:
    # macOS returns ENOTDIR, not ELOOP, when O_NOFOLLOW opens a symlink.
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd), ""
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None, "missing"
        try:
            info = os.lstat(name, dir_fd=parent_fd)
        except OSError:
            return None, "error"
        if stat.S_ISLNK(info.st_mode):
            return None, "symlink"
        return None, "error"


def _ensure_dir(parent_fd: int, name: str) -> tuple[int | None, str]:
    fd, kind = _open_child(parent_fd, name)
    if kind != "missing":
        return fd, kind
    try:
        os.mkdir(name, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError:
        return None, "error"
    return _open_child(parent_fd, name)


def _abs_parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute():
        raise WriteRefused("outside", str(path) or ".")
    raw = path.as_posix()
    if "\x00" in raw or "\\" in raw:
        raise WriteRefused("outside", raw)
    parts = path.parts
    if not parts or parts[0] != "/":
        raise WriteRefused("outside", raw)
    body = parts[1:]
    if any(part in ("", ".", "..") or "/" in part or "\\" in part or "\x00" in part for part in body):
        raise WriteRefused("outside", raw)
    return body


def _file_parts(relative: str) -> tuple[str, ...]:
    if not relative or relative.startswith("/") or "\\" in relative or "\x00" in relative:
        raise WriteRefused("outside", relative or ".")
    parts = Path(relative).parts
    if not parts or any(part in ("", ".", "..") or "/" in part or "\\" in part or "\x00" in part for part in parts):
        raise WriteRefused("outside", relative)
    return parts


def _resolved_parts(path: Path) -> tuple[str, ...]:
    """Parts of ``path`` after symlink ancestors are resolved.

    ``..`` is refused before resolution. A missing tail stays in the parts.
    A symlink that is the home itself, such as macOS ``/tmp``, becomes the
    real directory. Components under the root are not passed here.
    """
    _abs_parts(path)
    resolved = Path(os.path.realpath(path.as_posix()))
    return _abs_parts(resolved)


def directory_is_real(path: Path) -> None:
    """Refuse a symlink that remains after the home path is resolved.

    A missing tail is allowed. Nothing is created. A parent symlink such as
    ``/tmp`` is resolved first, so it is not reported as a broken file.
    """
    parts = _resolved_parts(path)
    opened: list[int] = []
    try:
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        opened.append(current)
        for part in parts:
            fd, kind = _open_child(current, part)
            if kind == "symlink":
                raise WriteRefused("symlink", str(path))
            if kind == "missing":
                return
            if fd is None:
                raise WriteRefused("outside", str(path))
            opened.append(fd)
            current = fd
    finally:
        _close_all(opened)


def _open_dir(path: Path, *, create: bool) -> tuple[int, list[int]]:
    """Open the resolved directory. Missing parents are created only when asked."""
    parts = _resolved_parts(path)
    opened: list[int] = []
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    opened.append(current)
    try:
        for part in parts:
            fd, kind = _ensure_dir(current, part) if create else _open_child(current, part)
            if kind == "symlink":
                raise WriteRefused("symlink", str(path))
            if fd is None:
                raise WriteRefused("missing" if kind == "missing" else "outside", str(path))
            opened.append(fd)
            current = fd
        return current, opened
    except Exception:
        _close_all(opened)
        raise


def _open_under(root: Path, extra: tuple[str, ...], *, create: bool) -> tuple[int, list[int]]:
    """Open ``root`` on its real path, then ``extra`` without following links.

    ``extra`` is the relative parent. A symlink there is refused. A symlink
    among the ancestors of ``root`` is resolved once, then the walk uses
    ``O_NOFOLLOW``.
    """
    parent_fd, fds = _open_dir(root, create=create)
    try:
        for part in extra:
            fd, kind = _ensure_dir(parent_fd, part) if create else _open_child(parent_fd, part)
            if kind == "symlink":
                raise WriteRefused("symlink", part)
            if fd is None:
                raise WriteRefused("missing" if kind == "missing" else "outside", part)
            fds.append(fd)
            parent_fd = fd
        return parent_fd, fds
    except Exception:
        _close_all(fds)
        raise


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        wrote = os.write(fd, view)
        if wrote <= 0:
            raise OSError("short write")
        view = view[wrote:]


def _single_name(parent_fd: int, tmp: str, fd: int, inode: int) -> bool:
    """True when the open file and the directory name are the same regular file, once."""
    try:
        held = os.fstat(fd)
        named = os.lstat(tmp, dir_fd=parent_fd)
    except OSError:
        return False
    return (
        stat.S_ISREG(held.st_mode)
        and stat.S_ISREG(named.st_mode)
        and held.st_ino == inode
        and named.st_ino == inode
        and held.st_nlink == 1
        and named.st_nlink == 1
    )


def _discard_temp(parent_fd: int, tmp: str, fd: int, inode: int) -> None:
    """Drop the bytes, then the name, only when that name still is this inode."""
    try:
        os.ftruncate(fd, 0)
    except OSError:
        pass
    try:
        named = os.lstat(tmp, dir_fd=parent_fd)
    except OSError:
        named = None
    if named is not None and named.st_ino == inode:
        try:
            os.unlink(tmp, dir_fd=parent_fd)
        except OSError:
            pass
    try:
        os.close(fd)
    except OSError:
        pass


def _place_new(parent_fd: int, name: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = -1
    tmp = ""
    for _attempt in range(4):
        tmp = ".rewrite." + secrets.token_hex(16)
        try:
            fd = os.open(tmp, flags, 0o600, dir_fd=parent_fd)
            break
        except FileExistsError:
            fd = -1
    if fd < 0:
        raise OSError("temporary file exists")
    try:
        os.fchmod(fd, 0o600)
        held = os.fstat(fd)
        if not _single_name(parent_fd, tmp, fd, held.st_ino):
            _discard_temp(parent_fd, tmp, fd, held.st_ino)
            fd = -1
            raise WriteRefused("hardlink", name)
        _write_all(fd, data)
        if not _single_name(parent_fd, tmp, fd, held.st_ino):
            _discard_temp(parent_fd, tmp, fd, held.st_ino)
            fd = -1
            raise WriteRefused("hardlink", name)
        os.close(fd)
        fd = -1
        os.replace(tmp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        placed = os.lstat(name, dir_fd=parent_fd)
        if placed.st_ino != held.st_ino or placed.st_nlink != 1:
            wipe = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            _discard_temp(parent_fd, name, wipe, placed.st_ino)
            raise WriteRefused("hardlink", name)
    except WriteRefused:
        raise
    except Exception:
        if fd >= 0:
            try:
                inode = os.fstat(fd).st_ino
            except OSError:
                inode = -1
            _discard_temp(parent_fd, tmp, fd, inode)
            fd = -1
        else:
            try:
                os.unlink(tmp, dir_fd=parent_fd)
            except OSError:
                pass
        raise


def write_bytes(
    root: Path,
    relative: str,
    data: bytes,
    *,
    create: bool = False,
    exclusive: bool = False,
    cut_hardlink: bool = True,
) -> str:
    """Write ``data`` to ``root/relative``. Return the hard-link note, or ``""``.

    ``create`` makes missing directories on the ``O_NOFOLLOW`` walk.
    ``exclusive`` refuses an existing final name.
    ``cut_hardlink`` False refuses a regular file with link count 2 or more.
    A symlink at the final name is refused before replace.
    """
    file_parts = _file_parts(relative)
    name = file_parts[-1]
    parent_fd, fds = _open_under(Path(root), file_parts[:-1], create=create)
    try:
        try:
            info = os.lstat(name, dir_fd=parent_fd)
        except FileNotFoundError:
            info = None
        note = ""
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise WriteRefused("symlink", relative)
        if info is not None and stat.S_ISREG(info.st_mode) and info.st_nlink >= 2:
            if exclusive or not cut_hardlink:
                raise WriteRefused("hardlink", relative)
            note = HARD_LINK_CUT
        elif info is not None and (exclusive or not stat.S_ISREG(info.st_mode)):
            raise WriteRefused("exists", relative)
        try:
            _place_new(parent_fd, name, data)
        except WriteRefused:
            raise
        except Exception as exc:
            raise WriteRefused("partial", relative) from exc
        return note
    finally:
        _close_all(fds)


def read_bytes(root: Path, relative: str) -> bytes | None:
    """Read ``root/relative``. Return None when it is missing.

    A symlink is refused. The walk uses ``O_NOFOLLOW``.
    """
    file_parts = _file_parts(relative)
    name = file_parts[-1]
    try:
        parent_fd, fds = _open_under(Path(root), file_parts[:-1], create=False)
    except WriteRefused as exc:
        if exc.kind == "missing":
            return None
        raise
    try:
        try:
            info = os.lstat(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise WriteRefused("symlink", relative)
        if not stat.S_ISREG(info.st_mode):
            raise WriteRefused("exists", relative)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            chunks: list[bytes] = []
            while True:
                block = os.read(fd, 1 << 20)
                if not block:
                    break
                chunks.append(block)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        _close_all(fds)


def remove_file(root: Path, relative: str) -> str:
    """Remove ``root/relative`` when it is one regular file. Return ``removed`` or ``missing``.

    A symlink is refused. The walk uses ``O_NOFOLLOW``, so the target of a link is left in place.
    """
    file_parts = _file_parts(relative)
    name = file_parts[-1]
    try:
        parent_fd, fds = _open_under(Path(root), file_parts[:-1], create=False)
    except WriteRefused as exc:
        if exc.kind == "missing":
            return "missing"
        raise
    try:
        try:
            info = os.lstat(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return "missing"
        if stat.S_ISLNK(info.st_mode):
            raise WriteRefused("symlink", relative)
        if not stat.S_ISREG(info.st_mode):
            raise WriteRefused("exists", relative)
        os.unlink(name, dir_fd=parent_fd)
        return "removed"
    finally:
        _close_all(fds)
