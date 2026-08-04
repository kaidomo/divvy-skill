#!/usr/bin/env python3
"""Initialize and validate divvy's private per-user state."""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Dict, List, NamedTuple, Optional, Tuple


ROOT = Path(__file__).resolve().parent.parent
LEDGER_TEMPLATE = ROOT / "templates" / "LEDGER.md"
ROSTER_TEMPLATE = ROOT / "templates" / "ROSTER.md"
SCHEMA = "divvy-state-permissions/v1"
FIELDS = (
    "schema", "command", "target", "path", "path_label", "status",
    "mode_before", "mode_after", "content_unchanged", "reason_code",
    "detail", "resume_stage",
)
REASON_CODES = {
    "ok", "mode_mismatch", "unsafe_type", "owner_mismatch",
    "symlink_refused", "hardlink_refused", "duplicate_target",
    "unsupported_safe_primitive", "partial_rollback", "content_changed",
    "residue_cleanup_required", "verification_failed",
}
_HAS_DIR_FD = all(
    func in os.supports_dir_fd for func in (os.open, os.mkdir, os.stat, os.unlink, os.link)
)
_HAS_LINK_NOFOLLOW = os.link in os.supports_follow_symlinks


class StateError(RuntimeError):
    def __init__(self, reason_code: str, detail: str, target: str = "state") -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail
        self.target = target


class TargetSpec(NamedTuple):
    label: str
    path: Path
    template: Path
    parent_kind: str
    base: Optional[Path]


class OpenTarget(NamedTuple):
    spec: TargetSpec
    parent_fd: int
    fd: int
    before_mode: int
    digest: str


class OpenDirectory(NamedTuple):
    spec: TargetSpec
    fd: int
    before_mode: int


def _absolute_lexical(value: str) -> Path:
    path = os.path.abspath(os.path.expanduser(value))
    # macOS exposes these root-owned compatibility aliases.  Normalize only
    # the fixed system prefix; resolving the whole path would hide unsafe
    # user-controlled symlinks later in the chain.
    if sys.platform == "darwin":
        if path == "/var" or path.startswith("/var/"):
            path = "/private" + path
        elif path == "/tmp" or path.startswith("/tmp/"):
            path = "/private" + path
    return Path(path)


def _file_path(value: str) -> Path:
    # Deliberately do not resolve: final-component and ancestor symlinks must
    # remain visible to descriptor-relative validation.
    return _absolute_lexical(value)


def _dir_path(direct_env: str, xdg_env: str, fallback: Path) -> Path:
    direct = os.environ.get(direct_env)
    if direct:
        return _absolute_lexical(direct)
    xdg = os.environ.get(xdg_env)
    base = _absolute_lexical(xdg) if xdg else fallback
    return _absolute_lexical(str(base / "divvy"))


def resolve_paths() -> Tuple[Path, Path]:
    """Return ledger and roster paths (kept compatible with ledger_distribution)."""
    ledger_override = os.environ.get("DIVVY_LEDGER")
    roster_override = os.environ.get("DIVVY_ROSTER")
    home = _absolute_lexical(os.environ.get("HOME", str(Path.home())))
    ledger = (
        _file_path(ledger_override)
        if ledger_override
        else _dir_path("DIVVY_STATE_DIR", "XDG_STATE_HOME", home / ".local" / "state") / "LEDGER.md"
    )
    roster = (
        _file_path(roster_override)
        if roster_override
        else _dir_path("DIVVY_CONFIG_DIR", "XDG_CONFIG_HOME", home / ".config") / "ROSTER.md"
    )
    return ledger, roster


def _specs() -> List[TargetSpec]:
    ledger, roster = resolve_paths()
    home = _absolute_lexical(os.environ.get("HOME", str(Path.home())))

    def make(label: str, path: Path, template: Path, file_env: str,
             dir_env: str, xdg_env: str, fallback: Path) -> TargetSpec:
        if os.environ.get(file_env):
            return TargetSpec(label, path, template, "file", path.parent)
        if os.environ.get(dir_env):
            leaf = _absolute_lexical(os.environ[dir_env])
            return TargetSpec(label, path, template, "direct", leaf.parent)
        if os.environ.get(xdg_env):
            return TargetSpec(label, path, template, "xdg", _absolute_lexical(os.environ[xdg_env]))
        return TargetSpec(label, path, template, "default", home)

    return [
        make("ledger", ledger, LEDGER_TEMPLATE, "DIVVY_LEDGER", "DIVVY_STATE_DIR", "XDG_STATE_HOME", home / ".local" / "state"),
        make("roster", roster, ROSTER_TEMPLATE, "DIVVY_ROSTER", "DIVVY_CONFIG_DIR", "XDG_CONFIG_HOME", home / ".config"),
    ]


def _receipt(command: str, target: str, path: Path, status: str,
             mode_before: str = "-", mode_after: str = "-",
             unchanged: str = "true", reason: str = "ok", detail: str = "-",
             resume_stage: str = "-") -> Dict[str, str]:
    return {
        "schema": SCHEMA, "command": command, "target": target,
        "path": str(path), "path_label": target, "status": status,
        "mode_before": mode_before, "mode_after": mode_after,
        "content_unchanged": unchanged, "reason_code": reason,
        "detail": detail, "resume_stage": resume_stage,
    }


def render_permission_receipt(record: Dict[str, str], public: bool = False) -> str:
    """Render a stable receipt, optionally projecting away host-local values."""
    reason = str(record.get("reason_code", "unsupported_safe_primitive"))
    if reason not in REASON_CODES:
        reason = "unsupported_safe_primitive"
    safe = dict(record)
    safe["reason_code"] = reason
    selected = (
        ("schema", "command", "path_label", "status", "content_unchanged", "reason_code")
        if public else FIELDS
    )
    return " ".join(
        "%s=%s" % (key, str(safe.get(key, "-")).replace("\n", "\\n").replace("\r", "\\r"))
        for key in selected
    )


def _emit(record: Dict[str, str], *, error: bool = False) -> None:
    print(render_permission_receipt(record), file=sys.stderr if error else sys.stdout)


def _mode(value: int) -> str:
    return format(value, "04o")


def _require_capabilities() -> None:
    required_flags = (getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_DIRECTORY", 0))
    if (not all(required_flags)
            or not _HAS_DIR_FD
            or not _HAS_LINK_NOFOLLOW):
        raise StateError("unsupported_safe_primitive", "descriptor/no-follow publication is unavailable")


def _open_root() -> int:
    return os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _components(path: Path) -> List[str]:
    if not path.is_absolute():
        raise StateError("unsafe_type", "absolute path required")
    return [part for part in path.parts if part not in (path.anchor, "")]


def _validate_directory_fd(fd: int, *, require_owner: bool) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise StateError("unsafe_type", "directory component is not a directory")
    if require_owner and info.st_uid != os.geteuid():
        raise StateError("owner_mismatch", "directory is not owned by the effective user")


def _open_directory(path: Path, *, require_final_owner: bool = True) -> int:
    current = _open_root()
    try:
        parts = _components(path)
        for index, part in enumerate(parts):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except OSError as exc:
                try:
                    entry = os.stat(part, dir_fd=current, follow_symlinks=False)
                except OSError:
                    entry = None
                if exc.errno == errno.ELOOP or (entry is not None and stat.S_ISLNK(entry.st_mode)):
                    raise StateError("symlink_refused", "symlinked directory component refused") from exc
                raise
            os.close(current)
            current = child
            _validate_directory_fd(current, require_owner=require_final_owner and index == len(parts) - 1)
        return current
    except BaseException:
        os.close(current)
        raise


def _descend_or_create(parent_fd: int, components: List[str]) -> int:
    current = os.dup(parent_fd)
    try:
        for part in components:
            enforce_mode = False
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    # A concurrent initializer may have published the owned
                    # directory after our failed open.
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                enforce_mode = True
            except OSError as exc:
                try:
                    entry = os.stat(part, dir_fd=current, follow_symlinks=False)
                except OSError:
                    entry = None
                if exc.errno == errno.ELOOP or (entry is not None and stat.S_ISLNK(entry.st_mode)):
                    raise StateError("symlink_refused", "symlinked directory component refused") from exc
                raise
            previous = current
            current = child
            os.close(previous)
            _validate_directory_fd(current, require_owner=True)
            if enforce_mode:
                os.fchmod(current, 0o700)
        return current
    except BaseException:
        os.close(current)
        raise


def _prepare_parent(spec: TargetSpec, *, create: bool, allow_mode_mismatch: bool = False) -> int:
    if spec.parent_kind == "file":
        return _open_directory(spec.path.parent)
    if spec.parent_kind == "default":
        assert spec.base is not None
        base_fd = _open_directory(spec.base)
        try:
            relative = spec.path.parent.relative_to(spec.base)
            if create:
                result = _descend_or_create(base_fd, list(relative.parts))
            else:
                result = _descend_existing(base_fd, list(relative.parts))
        finally:
            os.close(base_fd)
    else:
        assert spec.base is not None
        base_fd = _open_directory(spec.base)
        try:
            leaf = spec.path.parent.name
            if spec.path.parent.parent != spec.base:
                raise StateError("unsafe_type", "directory override must select one leaf")
            if create:
                result = _descend_or_create(base_fd, [leaf])
            else:
                result = _descend_existing(base_fd, [leaf])
        finally:
            os.close(base_fd)
    if not allow_mode_mismatch and stat.S_IMODE(os.fstat(result).st_mode) != 0o700:
        os.close(result)
        raise StateError("mode_mismatch", "owned divvy directory requires migrate-permissions", spec.label)
    return result


def _descend_existing(parent_fd: int, components: List[str]) -> int:
    current = os.dup(parent_fd)
    try:
        for part in components:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except OSError as exc:
                try:
                    entry = os.stat(part, dir_fd=current, follow_symlinks=False)
                except OSError:
                    entry = None
                if exc.errno == errno.ELOOP or (entry is not None and stat.S_ISLNK(entry.st_mode)):
                    raise StateError("symlink_refused", "symlinked directory component refused") from exc
                raise
            previous = current
            current = child
            os.close(previous)
            _validate_directory_fd(current, require_owner=True)
        return current
    except BaseException:
        os.close(current)
        raise


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        block = os.read(fd, 1024 * 128)
        if not block:
            break
        digest.update(block)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _open_target(spec: TargetSpec, parent_fd: int, *, defer_link_check: bool = False) -> OpenTarget:
    name = spec.path.name
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(info.st_mode):
        raise StateError("symlink_refused", "final-component symlink refused", spec.label)
    if not stat.S_ISREG(info.st_mode):
        raise StateError("unsafe_type", "target is not a regular file", spec.label)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StateError("symlink_refused", "final-component symlink refused", spec.label) from exc
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise StateError("unsafe_type", "target is not a regular file", spec.label)
        if opened.st_uid != os.geteuid():
            raise StateError("owner_mismatch", "target is not owned by the effective user", spec.label)
        if opened.st_nlink != 1 and not defer_link_check:
            raise StateError("hardlink_refused", "target link count is not one", spec.label)
        return OpenTarget(spec, parent_fd, fd, stat.S_IMODE(opened.st_mode), _hash_fd(fd))
    except BaseException:
        os.close(fd)
        raise


def _existing_residues(spec: TargetSpec, parent_fd: int) -> List[str]:
    prefix = ".%s." % spec.path.name
    # Listing by fd is supported on Linux via /proc but not portably on macOS;
    # scandir the already validated directory path, then validate any entry via
    # the retained descriptor before acting on it.
    return sorted(name for name in os.listdir(parent_fd) if name.startswith(prefix))


def _check_residues(spec: TargetSpec, parent_fd: int) -> None:
    for name in _existing_residues(spec, parent_fd):
        # A basename is not an authenticated managed-name receipt.  Even a
        # same-inode sibling may be a user-created hardlink, so init never
        # deletes or silently normalizes it.  A concurrent creator or a crash
        # residue is reported for explicit, separately authorized cleanup.
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        raise StateError(
            "residue_cleanup_required",
            "residue_requires_explicit_cleanup: verify owner token liveness prerequisites",
            spec.label,
        )


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_directory(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
            raise


def _publish(spec: TargetSpec, parent_fd: int) -> str:
    data = spec.template.read_bytes()
    token = "%d-%s" % (os.getpid(), secrets.token_hex(16))
    temp_name = ".%s.%s" % (spec.path.name, token)
    fd = os.open(
        temp_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    published = False
    try:
        os.fchmod(fd, 0o600)
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1):
            raise StateError("unsafe_type", "temporary sibling validation failed", spec.label)
        _write_all(fd, data)
        os.fsync(fd)
        if _hash_fd(fd) != hashlib.sha256(data).hexdigest():
            raise StateError("unsafe_type", "temporary sibling content verification failed", spec.label)
        named = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
        held = os.fstat(fd)
        if (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
            raise StateError("unsafe_type", "temporary sibling identity changed", spec.label)
        try:
            os.link(
                temp_name, spec.path.name,
                src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            published = True
        except FileExistsError:
            # A concurrent compliant publisher is success; any other occupant
            # is refused and is never overwritten.
            existing = _open_target(spec, parent_fd)
            try:
                if existing.before_mode != 0o600 or existing.digest != hashlib.sha256(data).hexdigest():
                    raise StateError("unsafe_type", "no-clobber publication found an unsafe occupant", spec.label)
            finally:
                os.close(existing.fd)
            os.unlink(temp_name, dir_fd=parent_fd)
            return "preserved"
        _fsync_directory(parent_fd)
        os.unlink(temp_name, dir_fd=parent_fd)
        _fsync_directory(parent_fd)
        return "created"
    except (StateError, OSError):
        # Ordinary handled failures clean an unpublished sibling. Deliberate or
        # process-level crashes are BaseException paths and leave evidence.
        if not published:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(fd)


def _duplicate_reason(opened: List[OpenTarget]) -> bool:
    paths = [os.path.normcase(os.path.abspath(str(item.spec.path))) for item in opened]
    if len(paths) != len(set(paths)):
        return True
    inodes = [(os.fstat(item.fd).st_dev, os.fstat(item.fd).st_ino) for item in opened]
    return len(inodes) != len(set(inodes))


def _run_init(specs: List[TargetSpec]) -> int:
    parents: List[int] = []
    opened: List[OpenTarget] = []
    states: List[Tuple[TargetSpec, int, Optional[OpenTarget]]] = []
    try:
        lexical = [os.path.normcase(os.path.abspath(str(spec.path))) for spec in specs]
        if len(lexical) != len(set(lexical)):
            raise StateError("duplicate_target", "ledger and roster resolve to one target")
        for spec in specs:
            parent_fd = _prepare_parent(spec, create=True)
            parents.append(parent_fd)
            _check_residues(spec, parent_fd)
            try:
                item = _open_target(spec, parent_fd, defer_link_check=True)
                opened.append(item)
            except FileNotFoundError:
                item = None
            states.append((spec, parent_fd, item))
        if _duplicate_reason(opened):
            raise StateError("duplicate_target", "ledger and roster alias one inode")
        for item in opened:
            if os.fstat(item.fd).st_nlink != 1:
                raise StateError("hardlink_refused", "target link count is not one", item.spec.label)
            if item.before_mode != 0o600:
                raise StateError("mode_mismatch", "run migrate-permissions explicitly", item.spec.label)
        for spec, parent_fd, item in states:
            status = "preserved" if item is not None else _publish(spec, parent_fd)
            print("%s_status=%s" % (spec.label, status))
            _emit(_receipt("init", spec.label, spec.path, status,
                           _mode(item.before_mode) if item else "-", "0600", "true", "ok"))
        return 0
    except StateError as exc:
        path = next((spec.path for spec in specs if spec.label == exc.target), specs[0].path)
        _emit(_receipt("init", exc.target, path, "refused", reason=exc.reason_code,
                       detail=exc.detail, resume_stage="validation"), error=True)
        if exc.reason_code == "mode_mismatch":
            print("초기화 거부: migrate-permissions 명령으로 명시적으로 수정하십시오", file=sys.stderr)
        return 2
    except OSError as exc:
        _emit(_receipt("init", "state", specs[0].path, "refused",
                       reason="unsafe_type", detail=str(exc), resume_stage="validation"), error=True)
        return 2
    finally:
        for item in opened:
            os.close(item.fd)
        for fd in parents:
            os.close(fd)


def _open_all(specs: List[TargetSpec]) -> Tuple[List[int], List[OpenDirectory], List[OpenTarget]]:
    parents: List[int] = []
    directories: List[OpenDirectory] = []
    opened: List[OpenTarget] = []
    lexical = [os.path.normcase(os.path.abspath(str(spec.path))) for spec in specs]
    if len(lexical) != len(set(lexical)):
        raise StateError("duplicate_target", "ledger and roster resolve to one target")
    try:
        for spec in specs:
            parent_fd = _prepare_parent(spec, create=False, allow_mode_mismatch=True)
            parents.append(parent_fd)
            if spec.parent_kind != "file":
                identity = (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
                if not any((os.fstat(item.fd).st_dev, os.fstat(item.fd).st_ino) == identity for item in directories):
                    directories.append(OpenDirectory(spec, os.dup(parent_fd), stat.S_IMODE(os.fstat(parent_fd).st_mode)))
            try:
                opened.append(_open_target(spec, parent_fd, defer_link_check=True))
            except (OSError, StateError):
                linked = next((item for item in opened if os.fstat(item.fd).st_nlink != 1), None)
                if linked is not None:
                    raise StateError("hardlink_refused", "target link count is not one", linked.spec.label)
                raise
        if _duplicate_reason(opened):
            raise StateError("duplicate_target", "ledger and roster alias one inode")
        for item in opened:
            if os.fstat(item.fd).st_nlink != 1:
                raise StateError("hardlink_refused", "target link count is not one", item.spec.label)
        return parents, directories, opened
    except BaseException:
        for item in opened:
            os.close(item.fd)
        for item in directories:
            os.close(item.fd)
        for fd in parents:
            os.close(fd)
        raise


def _run_check(specs: List[TargetSpec]) -> int:
    parents: List[int] = []
    directories: List[OpenDirectory] = []
    opened: List[OpenTarget] = []
    try:
        parents, directories, opened = _open_all(specs)
        mismatch = False
        for item in directories:
            compliant = item.before_mode == 0o700
            mismatch = mismatch or not compliant
            _emit(_receipt(
                "check-permissions", item.spec.label + "_dir", item.spec.path.parent,
                "compliant" if compliant else "noncompliant",
                _mode(item.before_mode), _mode(item.before_mode), "true",
                "ok" if compliant else "mode_mismatch",
            ))
        for item in opened:
            compliant = item.before_mode == 0o600
            mismatch = mismatch or not compliant
            _emit(_receipt(
                "check-permissions", item.spec.label, item.spec.path,
                "compliant" if compliant else "noncompliant",
                _mode(item.before_mode), _mode(item.before_mode), "true",
                "ok" if compliant else "mode_mismatch",
            ))
        return 3 if mismatch else 0
    except StateError as exc:
        path = next((spec.path for spec in specs if spec.label == exc.target), specs[0].path)
        _emit(_receipt("check-permissions", exc.target, path, "refused",
                       reason=exc.reason_code, detail=exc.detail,
                       resume_stage="validation"), error=True)
        return 2
    except OSError as exc:
        _emit(_receipt("check-permissions", "state", specs[0].path, "refused",
                       reason="unsafe_type", detail=str(exc), resume_stage="validation"), error=True)
        return 2
    finally:
        for item in opened:
            os.close(item.fd)
        for item in directories:
            os.close(item.fd)
        for fd in parents:
            os.close(fd)


def _run_migrate(specs: List[TargetSpec]) -> int:
    parents: List[int] = []
    directories: List[OpenDirectory] = []
    opened: List[OpenTarget] = []
    changed_dirs: List[OpenDirectory] = []
    changed: List[OpenTarget] = []
    try:
        parents, directories, opened = _open_all(specs)
        for item in directories:
            if item.before_mode == 0o700:
                continue
            try:
                os.fchmod(item.fd, 0o700)
                changed_dirs.append(item)
            except OSError as mutation_error:
                rollback_failed = False
                for prior in reversed(changed_dirs):
                    try:
                        os.fchmod(prior.fd, prior.before_mode)
                    except OSError:
                        rollback_failed = True
                stage = "mutation:%s_dir" % item.spec.label
                _emit(_receipt(
                    "migrate-permissions", item.spec.label + "_dir", item.spec.path.parent,
                    "PARTIAL" if rollback_failed else "refused", _mode(item.before_mode),
                    _mode(stat.S_IMODE(os.fstat(item.fd).st_mode)), "true",
                    "partial_rollback" if rollback_failed else "mode_mismatch",
                    str(mutation_error), stage,
                ), error=True)
                return 4 if rollback_failed else 2
        for item in opened:
            if item.before_mode == 0o600:
                continue
            try:
                os.fchmod(item.fd, 0o600)
                changed.append(item)
            except OSError as mutation_error:
                rollback_failed = False
                for prior in reversed(changed):
                    try:
                        os.fchmod(prior.fd, prior.before_mode)
                    except OSError:
                        rollback_failed = True
                for prior in reversed(changed_dirs):
                    try:
                        os.fchmod(prior.fd, prior.before_mode)
                    except OSError:
                        rollback_failed = True
                stage = "mutation:%s" % item.spec.label
                if rollback_failed:
                    _emit(_receipt(
                        "migrate-permissions", item.spec.label, item.spec.path,
                        "PARTIAL", _mode(item.before_mode), _mode(stat.S_IMODE(os.fstat(item.fd).st_mode)),
                        "true", "partial_rollback", str(mutation_error), stage,
                    ), error=True)
                    return 4
                _emit(_receipt(
                    "migrate-permissions", item.spec.label, item.spec.path,
                    "refused", _mode(item.before_mode), _mode(item.before_mode),
                    "true", "mode_mismatch", str(mutation_error), stage,
                ), error=True)
                return 2
        changed_content: Optional[OpenTarget] = None
        verification_failure: Optional[Tuple[OpenTarget, OSError]] = None
        if changed or changed_dirs:
            for item in opened:
                try:
                    if _hash_fd(item.fd) != item.digest:
                        changed_content = item
                        break
                except OSError as exc:
                    verification_failure = (item, exc)
                    break
        if changed_content is not None or verification_failure is not None:
            rollback_failed = False
            for prior in reversed(changed):
                try:
                    os.fchmod(prior.fd, prior.before_mode)
                except OSError:
                    rollback_failed = True
            for prior in reversed(changed_dirs):
                try:
                    os.fchmod(prior.fd, prior.before_mode)
                except OSError:
                    rollback_failed = True
            failed_item = changed_content if changed_content is not None else verification_failure[0]
            content_status = "false" if changed_content is not None else "unknown"
            reason = "content_changed" if changed_content is not None else "verification_failed"
            detail = (
                "target bytes changed while migration descriptors were retained"
                if changed_content is not None
                else "content verification failed after mode mutation: %s" % verification_failure[1]
            )
            _emit(_receipt(
                "migrate-permissions", failed_item.spec.label, failed_item.spec.path,
                "PARTIAL", _mode(failed_item.before_mode),
                _mode(stat.S_IMODE(os.fstat(failed_item.fd).st_mode)), content_status,
                "partial_rollback" if rollback_failed else reason,
                detail, "verification:%s" % failed_item.spec.label,
            ), error=True)
            return 4
        for item in directories:
            after_mode = stat.S_IMODE(os.fstat(item.fd).st_mode)
            _emit(_receipt(
                "migrate-permissions", item.spec.label + "_dir", item.spec.path.parent,
                "no-op" if item.before_mode == 0o700 else "migrated",
                _mode(item.before_mode), _mode(after_mode), "true", "ok",
                resume_stage="complete",
            ))
        for item in opened:
            after_mode = stat.S_IMODE(os.fstat(item.fd).st_mode)
            status = "no-op" if item.before_mode == 0o600 else "migrated"
            _emit(_receipt(
                "migrate-permissions", item.spec.label, item.spec.path, status,
                _mode(item.before_mode), _mode(after_mode), "true",
                "ok", resume_stage="complete",
            ))
        return 0
    except StateError as exc:
        path = next((spec.path for spec in specs if spec.label == exc.target), specs[0].path)
        _emit(_receipt("migrate-permissions", exc.target, path, "refused",
                       reason=exc.reason_code, detail=exc.detail,
                       resume_stage="validation"), error=True)
        return 2
    except OSError as exc:
        _emit(_receipt("migrate-permissions", "state", specs[0].path, "refused",
                       reason="unsafe_type", detail=str(exc), resume_stage="validation"), error=True)
        return 2
    finally:
        for item in opened:
            os.close(item.fd)
        for item in directories:
            os.close(item.fd)
        for fd in parents:
            os.close(fd)


def install_once(template: Path, target: Path) -> str:
    """Compatibility wrapper for callers that initialize one exact file."""
    _require_capabilities()
    spec = TargetSpec(target.stem.lower(), _file_path(str(target)), template, "file", target.parent)
    parent_fd = _prepare_parent(spec, create=True)
    opened: Optional[OpenTarget] = None
    try:
        _check_residues(spec, parent_fd)
        try:
            opened = _open_target(spec, parent_fd)
        except FileNotFoundError:
            return _publish(spec, parent_fd)
        if opened.before_mode != 0o600:
            raise StateError("mode_mismatch", "run migrate-permissions explicitly", spec.label)
        return "preserved"
    finally:
        if opened is not None:
            os.close(opened.fd)
        os.close(parent_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description="initialize private divvy state outside the Git checkout")
    parser.add_argument("command", choices=("paths", "init", "check-permissions", "migrate-permissions"))
    args = parser.parse_args()
    ledger, roster = resolve_paths()
    if args.command == "paths":
        print("ledger=%s" % ledger)
        print("roster=%s" % roster)
        return 0
    try:
        _require_capabilities()
    except StateError as exc:
        _emit(_receipt(args.command, "state", ledger, "refused",
                       reason=exc.reason_code, detail=exc.detail,
                       resume_stage="capability-check"), error=True)
        return 2
    specs = _specs()
    if args.command == "init":
        return _run_init(specs)
    if args.command == "check-permissions":
        return _run_check(specs)
    return _run_migrate(specs)


if __name__ == "__main__":
    raise SystemExit(main())
