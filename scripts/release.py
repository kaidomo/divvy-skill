#!/usr/bin/env python3
"""Validate divvy release metadata and render release notes."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re
import subprocess
import sys
from typing import List, Optional, Tuple


ROOT = Path(__file__).resolve().parent.parent
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)
RELEASE_HEADING_RE = re.compile(r"^## \[([^]]+)\] - (\d{4}-\d{2}-\d{2})$")
PLACEHOLDER_RE = re.compile(r"\b(?:TODO|TBD|CHANGEME)\b", re.IGNORECASE)


class ReleaseError(RuntimeError):
    pass


def read_version(root: Path = ROOT) -> str:
    path = root / "VERSION"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(f"cannot read VERSION: {exc}") from exc
    version = raw.strip()
    if raw != version + "\n":
        raise ReleaseError("VERSION must contain exactly one stable SemVer line ending in newline")
    if not SEMVER_RE.fullmatch(version):
        raise ReleaseError(f"VERSION is not valid stable SemVer: {version!r}")
    return version


def release_notes(root: Path, version: str) -> str:
    path = root / "CHANGELOG.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseError(f"cannot read CHANGELOG.md: {exc}") from exc

    matches = [index for index, line in enumerate(lines) if line.startswith(f"## [{version}] ")]
    if len(matches) != 1:
        raise ReleaseError(f"CHANGELOG.md must contain exactly one release heading for {version}")
    start = matches[0]
    heading = RELEASE_HEADING_RE.fullmatch(lines[start])
    if heading is None:
        raise ReleaseError(f"release heading must be '## [{version}] - YYYY-MM-DD'")
    try:
        date.fromisoformat(heading.group(2))
    except ValueError as exc:
        raise ReleaseError(f"release heading has an invalid date: {heading.group(2)}") from exc
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    notes = "\n".join(lines[start + 1:end]).strip()
    if not notes:
        raise ReleaseError(f"CHANGELOG.md release {version} has no notes")
    if PLACEHOLDER_RE.search(notes):
        raise ReleaseError(f"CHANGELOG.md release {version} still contains a placeholder")
    return notes + "\n"


def version_tuple(version: str) -> Tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise ReleaseError(f"not a stable MAJOR.MINOR.PATCH version: {version!r}")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def git_release_versions(root: Path, current_tag: str) -> List[Tuple[str, Tuple[int, int, int]]]:
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v*"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseError(f"cannot inspect Git release tags: {exc}") from exc

    versions = []
    for tag in result.stdout.splitlines():
        if tag == current_tag or not tag.startswith("v"):
            continue
        version = tag[1:]
        if SEMVER_RE.fullmatch(version):
            versions.append((tag, version_tuple(version)))
    return versions


def check_release(root: Path = ROOT, tag: Optional[str] = None, history: bool = False) -> str:
    version = read_version(root)
    release_notes(root, version)
    if tag is not None and tag != f"v{version}":
        raise ReleaseError(f"tag {tag!r} does not match VERSION; expected 'v{version}'")
    if history:
        if tag is None:
            raise ReleaseError("--history requires --tag")
        prior_versions = git_release_versions(root, tag)
        if prior_versions:
            highest_tag, highest_version = max(prior_versions, key=lambda item: item[1])
            if version_tuple(version) <= highest_version:
                raise ReleaseError(
                    f"VERSION {version} must be greater than existing release {highest_tag}"
                )
    return version


def next_version(version: str, bump: str) -> str:
    major, minor, patch = version_tuple(version)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"unknown bump: {bump}")


def write_notes(path: Path, notes: str) -> None:
    if not path.parent.is_dir():
        raise ReleaseError(f"output directory does not exist: {path.parent}")
    try:
        with path.open("x", encoding="utf-8") as output:
            output.write(notes)
    except FileExistsError as exc:
        raise ReleaseError(f"refusing to overwrite existing file: {path}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="divvy release metadata helper")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("current", help="print the current VERSION")

    next_parser = subparsers.add_parser("next", help="print the next semantic version")
    next_parser.add_argument("bump", choices=("major", "minor", "patch"))

    check_parser = subparsers.add_parser("check", help="validate VERSION and CHANGELOG")
    check_parser.add_argument("--tag", help="require an exact v<VERSION> tag")
    check_parser.add_argument(
        "--history",
        action="store_true",
        help="require VERSION to be newer than all other stable release tags",
    )

    notes_parser = subparsers.add_parser("notes", help="render the current CHANGELOG section")
    notes_parser.add_argument("--output", type=Path, help="write notes to a file instead of stdout")
    return parser


def main(argv: Optional[Tuple[str, ...]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "current":
            print(read_version())
        elif args.command == "next":
            print(next_version(read_version(), args.bump))
        elif args.command == "check":
            version = check_release(tag=args.tag, history=args.history)
            print(f"release metadata valid: v{version}")
        elif args.command == "notes":
            version = read_version()
            notes = release_notes(ROOT, version)
            if args.output is None:
                print(notes, end="")
            else:
                write_notes(args.output, notes)
                print(f"release notes written: {args.output}")
        return 0
    except (OSError, UnicodeError, ReleaseError) as exc:
        print(f"release refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
