#!/usr/bin/env python3
"""Apply the divvy-local workaround for OMX issue #3420.

This never runs from dispatch.sh. It only edits an explicitly selected or
discovered oh-my-codex installation after exact source/dist shape checks.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ISSUE_URL = "https://github.com/Yeachan-Heo/oh-my-codex/issues/3420"
SUPPORTED_VERSION = "0.20.4"
BACKUP_SUFFIX = ".divvy-omx-3420.bak"

TARGETS = (
    (
        Path("src/scripts/codex-native-hook.ts"),
        '      const unmatchedStopSession = failure.stopReason === "session_scope_unmatched";\n',
        '      const identityIndeterminateStopPointer = pointer.status === "identity-indeterminate";\n',
        "      if (pointerCannotAuthorizeThisCwd || unmatchedStopSession || stopHookActive) {",
        "      if (pointerCannotAuthorizeThisCwd || unmatchedStopSession || identityIndeterminateStopPointer || stopHookActive) {",
    ),
    (
        Path("dist/scripts/codex-native-hook.js"),
        '            const unmatchedStopSession = failure.stopReason === "session_scope_unmatched";\n',
        '            const identityIndeterminateStopPointer = pointer.status === "identity-indeterminate";\n',
        "            if (pointerCannotAuthorizeThisCwd || unmatchedStopSession || stopHookActive) {",
        "            if (pointerCannotAuthorizeThisCwd || unmatchedStopSession || identityIndeterminateStopPointer || stopHookActive) {",
    ),
)


class HotfixError(RuntimeError):
    pass


def discover_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_root = os.environ.get("DIVVY_OMX_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    try:
        npm_root = subprocess.run(
            ["npm", "root", "-g"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise HotfixError("OMX 설치 위치를 찾지 못함: --omx-root 또는 DIVVY_OMX_ROOT를 지정하라") from exc
    if not npm_root:
        raise HotfixError("npm root -g가 빈 경로를 반환함")
    return (Path(npm_root) / "oh-my-codex").resolve()


def package_version(root: Path) -> str:
    package_json = root / "package.json"
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HotfixError(f"유효한 package.json을 읽지 못함: {package_json}") from exc
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise HotfixError(f"package.json에 version이 없음: {package_json}")
    return version


def classify(text: str, anchor: str, declaration: str,
             old_condition: str, new_condition: str) -> str:
    anchor_count = text.count(anchor)
    declaration_count = text.count(declaration)
    old_count = text.count(old_condition)
    new_count = text.count(new_condition)
    if anchor_count == 1 and declaration_count == 1 and old_count == 0 and new_count == 1:
        return "patched"
    if anchor_count == 1 and declaration_count == 0 and old_count == 1 and new_count == 0:
        return "vulnerable"
    return "unknown"


def inspect(root: Path):
    rows = []
    for relative, anchor, declaration, old_condition, new_condition in TARGETS:
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HotfixError(f"대상 파일을 읽지 못함: {path}") from exc
        rows.append((path, text, anchor, declaration, old_condition, new_condition,
                     classify(text, anchor, declaration, old_condition, new_condition)))
    return rows


def atomic_write(path: Path, content: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def patched_text(text: str, anchor: str, declaration: str,
                 old_condition: str, new_condition: str) -> str:
    if text.count(anchor) != 1:
        raise HotfixError("패치 기준 anchor가 정확히 한 번 존재하지 않음")
    updated = text.replace(anchor, anchor + declaration, 1)
    updated = updated.replace(old_condition, new_condition, 1)
    return updated


def print_status(root: Path, version: str, rows) -> None:
    print(f"OMX root: {root}")
    print(f"OMX version: {version}")
    for path, *_rest, state in rows:
        print(f"{path.relative_to(root)}: {state}")
    print(f"Upstream: {ISSUE_URL}")


def apply(root: Path, version: str, rows) -> None:
    states = [row[-1] for row in rows]
    if all(state == "patched" for state in states):
        print("이미 적용됐거나 동일한 upstream 수정이 존재함 — 변경 없음")
        return
    if version != SUPPORTED_VERSION:
        raise HotfixError(
            f"지원하지 않는 OMX 버전 {version}; {SUPPORTED_VERSION} 외 버전은 upstream 상태를 먼저 확인하라"
        )
    if not all(state == "vulnerable" for state in states):
        raise HotfixError(f"source/dist 상태가 예상과 다름({', '.join(states)}); 안전을 위해 수정하지 않음")

    backup_paths = [(row[0], Path(str(row[0]) + BACKUP_SUFFIX)) for row in rows]
    existing = [str(backup) for _path, backup in backup_paths if backup.exists()]
    if existing:
        raise HotfixError("기존 백업이 있어 중단: " + ", ".join(existing))

    backups = []
    written = []
    try:
        for path, backup in backup_paths:
            shutil.copy2(path, backup)
            backups.append((path, backup))
        for path, text, anchor, declaration, old_condition, new_condition, _state in rows:
            atomic_write(path, patched_text(text, anchor, declaration, old_condition, new_condition))
            written.append(path)
    except Exception:
        rollback_failed = False
        for path, backup in backups:
            if path in written and backup.exists():
                try:
                    shutil.copy2(backup, path)
                except OSError:
                    rollback_failed = True
        if not rollback_failed:
            for _path, backup in backups:
                backup.unlink(missing_ok=True)
        raise
    print("핫픽스 적용 완료. OMX 업데이트/재설치 후에는 status를 다시 실행하라.")


def restore(rows) -> None:
    states = [row[-1] for row in rows]
    if not all(state == "patched" for state in states):
        raise HotfixError(f"현재 파일이 정확한 핫픽스 형태가 아님({', '.join(states)}); 복원하지 않음")
    backups = [(row[0], Path(str(row[0]) + BACKUP_SUFFIX)) for row in rows]
    missing = [str(backup) for _path, backup in backups if not backup.is_file()]
    if missing:
        raise HotfixError("백업이 없어 복원할 수 없음: " + ", ".join(missing))
    originals = []
    for path, backup in backups:
        backup_text = backup.read_text(encoding="utf-8")
        _relative, _anchor, declaration, old_condition, new_condition = next(
            target for target in TARGETS if root_relative(path, rows) == target[0]
        )
        if classify(backup_text, _anchor, declaration, old_condition, new_condition) != "vulnerable":
            raise HotfixError(f"백업 내용이 예상한 원본이 아님: {backup}")
        originals.append((path, backup, backup_text))
    restored = []
    try:
        for path, _backup, backup_text in originals:
            current_text = next(row[1] for row in rows if row[0] == path)
            atomic_write(path, backup_text)
            restored.append((path, current_text))
    except Exception:
        for path, current_text in restored:
            atomic_write(path, current_text)
        raise
    for _path, backup, _backup_text in originals:
        backup.unlink()
    print("핫픽스 복원 완료")


def root_relative(path: Path, rows) -> Path:
    root = rows[0][0].parents[2]
    return path.relative_to(root)


def main() -> int:
    parser = argparse.ArgumentParser(description="divvy local workaround for OMX #3420")
    parser.add_argument("command", choices=("status", "apply", "restore"))
    parser.add_argument("--omx-root", help="oh-my-codex package root (tests/manual override)")
    args = parser.parse_args()
    try:
        root = discover_root(args.omx_root)
        version = package_version(root)
        rows = inspect(root)
        print_status(root, version, rows)
        if args.command == "apply":
            apply(root, version, rows)
        elif args.command == "restore":
            restore(rows)
        return 0
    except HotfixError as exc:
        print(f"거부: {exc}", file=sys.stderr)
        print(f"Upstream: {ISSUE_URL}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
