#!/usr/bin/env python3
"""Initialize divvy's private per-user roster and ledger without overwriting them."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
LEDGER_TEMPLATE = ROOT / "templates" / "LEDGER.md"
ROSTER_TEMPLATE = ROOT / "templates" / "ROSTER.md"


class StateError(RuntimeError):
    pass


def _file_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.parent.resolve() / path.name


def _dir_path(direct_env: str, xdg_env: str, fallback: Path) -> Path:
    direct = os.environ.get(direct_env)
    if direct:
        return Path(direct).expanduser().resolve()
    xdg = os.environ.get(xdg_env)
    base = Path(xdg).expanduser() if xdg else fallback
    return (base / "divvy").resolve()


def resolve_paths() -> tuple[Path, Path]:
    ledger_override = os.environ.get("DIVVY_LEDGER")
    roster_override = os.environ.get("DIVVY_ROSTER")
    ledger = (
        _file_path(ledger_override)
        if ledger_override
        else _dir_path("DIVVY_STATE_DIR", "XDG_STATE_HOME", Path.home() / ".local" / "state") / "LEDGER.md"
    )
    roster = (
        _file_path(roster_override)
        if roster_override
        else _dir_path("DIVVY_CONFIG_DIR", "XDG_CONFIG_HOME", Path.home() / ".config") / "ROSTER.md"
    )
    return ledger, roster


def install_once(template: Path, target: Path) -> str:
    if target.is_symlink():
        raise StateError(f"심링크 대상은 초기화하지 않음: {target}")
    if target.exists():
        if not target.is_file():
            raise StateError(f"일반 파일이 아닌 기존 경로: {target}")
        return "preserved"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        os.close(fd)
        shutil.copyfile(template, tmp)
        shutil.copymode(template, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return "created"


def main() -> int:
    parser = argparse.ArgumentParser(description="initialize private divvy state outside the Git checkout")
    parser.add_argument("command", choices=("paths", "init"))
    args = parser.parse_args()
    ledger, roster = resolve_paths()
    print(f"ledger={ledger}")
    print(f"roster={roster}")
    if args.command == "paths":
        return 0
    try:
        print(f"ledger_status={install_once(LEDGER_TEMPLATE, ledger)}")
        print(f"roster_status={install_once(ROSTER_TEMPLATE, roster)}")
        return 0
    except (OSError, StateError) as exc:
        print(f"초기화 거부: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
