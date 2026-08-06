#!/usr/bin/env python3
"""Validate divvy release metadata and render release notes."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


ROOT = Path(__file__).resolve().parent.parent
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)
RELEASE_HEADING_RE = re.compile(r"^## \[([^]]+)\] - (\d{4}-\d{2}-\d{2})$")
PLACEHOLDER_RE = re.compile(r"\b(?:TODO|TBD|CHANGEME)\b", re.IGNORECASE)
RELEASE_LABELS = {"release:skip", "release:major", "release:minor", "release:patch"}
BUMP_RANK = {"patch": 0, "minor": 1, "major": 2}
SENSITIVE_PATHS = (
    ".github/workflows/",
    ".github/release_allowed_signers",
    ".github/release_label_actors",
    "scripts/release.py",
    "VERSION",
    "CHANGELOG.md",
    "RELEASING.md",
)
METADATA_PATHS = {"VERSION", "CHANGELOG.md"}
REPAIR_IDENTITY_FIELDS = (
    "schema_version",
    "batch_id",
    "frontier",
    "prior_tag",
    "pr_set_digest",
    "label_note_digest",
    "metadata_tree",
    "candidate_version",
    "expected_parent",
)


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


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def value_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ReleaseError(f"INVALID_TIMESTAMP: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ReleaseError("INVALID_TIMESTAMP: timezone required")
    return parsed.astimezone(timezone.utc)


def immutable_title(commit_message: str, number: int) -> str:
    lines = commit_message.splitlines()
    merge_prefix = f"Merge pull request #{number} "
    if lines and lines[0].startswith(merge_prefix):
        candidates = [line.strip() for line in lines[1:] if line.strip()]
        if len(candidates) != 1:
            raise ReleaseError("UNBOUND_MAIN_PUSH: merge message must contain one immutable title")
        title = candidates[0]
    else:
        suffix = re.compile(rf"\s+\(#{number}\)$")
        if len(lines) != 1 or suffix.search(lines[0]) is None:
            raise ReleaseError("UNBOUND_MAIN_PUSH: unsupported direct/rebase/ambiguous commit message")
        title = suffix.sub("", lines[0]).strip()
    if not title or PLACEHOLDER_RE.search(title):
        raise ReleaseError("UNSAFE_NOTE: empty or placeholder title")
    render_note(number, title)
    return title


def render_note(number: int, title: str) -> str:
    if not title or any(ord(char) < 32 and char not in "\t" for char in title) or "\u2028" in title or "\u2029" in title:
        raise ReleaseError("UNSAFE_NOTE: disallowed control character")
    return f"- {title} (#{number})\n"


def classify_release(title: str, labels: Iterable[str], current_version: str = "0.1.0") -> Tuple[Optional[str], str]:
    release_labels = sorted(set(labels) & RELEASE_LABELS)
    bumps = [label.split(":", 1)[1] for label in release_labels if label != "release:skip"]
    if ("release:skip" in release_labels and bumps) or len(bumps) > 1:
        raise ReleaseError(f"LABEL_CONFLICT: {','.join(release_labels)}")
    if "release:skip" in release_labels:
        return None, "release_skip"

    normalized = title.strip()
    breaking = bool(re.match(r"^[a-zA-Z][\w-]*(?:\([^)]*\))?!:", normalized)) or "BREAKING CHANGE:" in normalized
    if breaking:
        floor = "minor" if version_tuple(current_version)[0] == 0 else "major"
        reason = "breaking_0x" if floor == "minor" else "breaking"
    elif re.match(r"^feat(?:\([^)]*\))?:", normalized):
        floor, reason = "minor", "feat"
    elif re.match(r"^fix(?:\([^)]*\))?:", normalized):
        floor, reason = "patch", "fix"
    else:
        floor, reason = "patch", "unknown_default_patch"
    if bumps:
        override = bumps[0]
        if BUMP_RANK[override] < BUMP_RANK[floor]:
            raise ReleaseError(f"LABEL_FLOOR_CONFLICT: {override} below {floor}")
        return override, f"override_{override}"
    return floor, reason


def labels_at_merge(
    events: Iterable[Dict[str, Any]],
    merge_time: str,
    allowed_actors: Set[str],
    *,
    pages_complete: bool,
) -> List[str]:
    if not pages_complete:
        raise ReleaseError("INCOMPLETE_EVIDENCE: label timeline pagination incomplete")
    boundary = parse_timestamp(merge_time)
    present: Set[str] = set()
    seen: Set[Any] = set()
    ordered = sorted(events, key=lambda event: (event.get("created_at", ""), str(event.get("id", ""))))
    for event in ordered:
        event_id = event.get("id")
        if event_id in seen:
            continue
        seen.add(event_id)
        label = (event.get("label") or {}).get("name")
        if label not in RELEASE_LABELS:
            continue
        occurred = parse_timestamp(event.get("created_at", ""))
        if occurred > boundary:
            raise ReleaseError(f"POST_MERGE_LABEL_MUTATION: {label}")
        actor = (event.get("actor") or {}).get("login")
        if actor not in allowed_actors:
            raise ReleaseError(f"LABEL_ACTOR_UNTRUSTED: {actor!r}")
        if event.get("event") == "labeled":
            present.add(label)
        elif event.get("event") == "unlabeled":
            present.discard(label)
    return sorted(present)


def bind_associated_pr(associations: Iterable[Dict[str, Any]], base: str) -> Dict[str, Any]:
    unique = {item.get("number"): item for item in associations if item.get("base") == base}
    merged = [item for item in unique.values() if item.get("merge_sha") and item.get("merge_time")]
    if len(merged) != 1:
        raise ReleaseError("UNBOUND_MAIN_PUSH: expected exactly one merged PR association")
    return merged[0]


def authorize_effect(event_origin: str, changed_paths: Iterable[str], base: str) -> str:
    if base != "main":
        raise ReleaseError("SENSITIVE_PATH_DENIED: effects require main")
    if event_origin != "protected-main" and any(
        path == prefix or path.startswith(prefix) for path in changed_paths for prefix in SENSITIVE_PATHS
    ):
        raise ReleaseError("SENSITIVE_PATH_DENIED: untrusted origin changed release control path")
    return "AUTHORIZED"


def compute_batch_id(
    schema_version: str, source_frontier: str, prior_tag: str, envelopes: Iterable[Dict[str, Any]]
) -> str:
    payload = {
        "schema_version": schema_version,
        "source_frontier": source_frontier,
        "prior_tag": prior_tag,
        "envelopes": [value_sha256(envelope) for envelope in envelopes],
    }
    return value_sha256(payload)


def reconcile_batch(
    current_version: str, envelopes: Iterable[Dict[str, Any]], covered: Set[int]
) -> Dict[str, Any]:
    released: List[int] = []
    skipped: List[int] = []
    notes: List[str] = []
    bumps: List[str] = []
    reasons: Dict[str, str] = {}
    for envelope in envelopes:
        number = int(envelope["number"])
        if number in covered:
            continue
        title = envelope.get("title") or immutable_title(envelope["commit_message"], number)
        bump, reason = classify_release(title, envelope.get("labels", []), current_version)
        reasons[str(number)] = reason
        if bump is None:
            skipped.append(number)
            continue
        bumps.append(bump)
        released.append(number)
        notes.append(render_note(number, title))
    if not released and not skipped:
        return {
            "status": "MATCHING_NOOP", "effect_count": 0, "candidate_version": current_version,
            "released_prs": [], "skipped_prs": [], "notes": "", "reasons": {},
        }
    if not released:
        return {
            "status": "SKIPPED_NO_CUT", "effect_count": 0, "candidate_version": current_version,
            "released_prs": [], "skipped_prs": skipped, "notes": "", "reasons": reasons,
        }
    bump = max(bumps, key=lambda value: BUMP_RANK[value])
    return {
        "status": "RELEASE_PENDING",
        "effect_count": 4,
        "candidate_version": next_version(current_version, bump),
        "bump": bump,
        "released_prs": released,
        "skipped_prs": skipped,
        "notes": "".join(notes),
        "reasons": reasons,
    }


def cas_decision(expected: str, observed: str, recomputes: int) -> str:
    if expected == observed:
        return "MATCH"
    if recomputes == 0:
        return "RECOMPUTE"
    raise ReleaseError("CAS_EXHAUSTED: remote state changed after one recomputation")


def resume_phase(expected: Dict[str, Any], observed: Dict[str, Any], phase: str) -> str:
    differing = [field for field in REPAIR_IDENTITY_FIELDS if expected.get(field) != observed.get(field)]
    if differing:
        raise ReleaseError(f"RESUME_IDENTITY_MISMATCH: {','.join(differing)}")
    phases = {"none": "METADATA_PENDING", "metadata": "TAG_PENDING", "tag": "RELEASE_PENDING", "release": "MATCHING_NOOP"}
    if phase not in phases:
        raise ReleaseError(f"UNKNOWN_PHASE: {phase}")
    return phases[phase]


def compare_release(expected: Dict[str, Any], observed: Optional[Dict[str, Any]]) -> str:
    if observed is None:
        return "CREATE_RELEASE"
    fields = ("tag_name", "target_commitish", "name", "body", "draft", "prerelease")
    differing = [field for field in fields if expected.get(field) != observed.get(field)]
    if differing:
        raise ReleaseError(f"PUBLIC_RELEASE_MISMATCH: {','.join(differing)}; refusing overwrite")
    return "MATCHING_NOOP"


def four_way_equal(version: str, changelog: str, tag: str, release_tag: str) -> str:
    values = (version, changelog, tag.removeprefix("v"), release_tag.removeprefix("v"))
    if len(set(values)) != 1:
        raise ReleaseError("FOUR_WAY_MISMATCH: VERSION, CHANGELOG, tag, Release")
    return "MATCH"


def queue_guard(pending: int) -> str:
    if pending > 100:
        raise ReleaseError("QUEUE_SATURATED: manual reconciliation required")
    return "OK"


def metadata_recursion(
    expected: Dict[str, Any], observed: Dict[str, Any], changed_paths: Iterable[str]
) -> str:
    differing = [field for field in REPAIR_IDENTITY_FIELDS if expected.get(field) != observed.get(field)]
    paths = set(changed_paths)
    if differing or not paths or not paths <= METADATA_PATHS:
        raise ReleaseError("UNTRUSTED_METADATA_PUSH: full identity/path allowlist mismatch")
    return "NOOP"


def redact(text: str, secrets: Iterable[str]) -> str:
    redacted = text
    supplied = [secret for secret in secrets if secret]
    for secret in supplied:
        redacted = redacted.replace(secret, "***")
    if re.search(r"(?i)(?:token|private[_ -]?key|passphrase)\s*[=:]\s*(?!\*{3})\S+", redacted):
        raise ReleaseError("REDACTION_FAILED: sensitive-looking value remains")
    return redacted


def render_metadata(changelog: str, version: str, notes: str, release_date: str, provenance: str) -> str:
    date.fromisoformat(release_date)
    if not SEMVER_RE.fullmatch(version):
        raise ReleaseError("invalid candidate version")
    marker = "## [Unreleased]"
    if marker not in changelog:
        raise ReleaseError("CHANGELOG.md is missing Unreleased heading")
    section = f"\n\n## [{version}] - {release_date}\n\n{notes.rstrip()}\n\n<!-- release-provenance: {provenance} -->"
    if f"## [{version}] " in changelog:
        raise ReleaseError("refusing to render duplicate release section")
    return changelog.replace(marker, marker + section, 1)


def verify_trusted_tag(root: Path, tag: str, allowed_signers: Path, expected_target: str) -> str:
    try:
        object_type = subprocess.run(
            ["git", "cat-file", "-t", tag], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        target = subprocess.run(
            ["git", "rev-list", "-n", "1", tag], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        verified = subprocess.run(
            ["git", "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={allowed_signers}",
             "verify-tag", tag], cwd=root, check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseError(f"TAG_TRUST_FAILED: {exc}") from exc
    if object_type != "tag" or target != expected_target or "Good" not in (verified.stdout + verified.stderr):
        raise ReleaseError("TAG_TRUST_FAILED: annotation, target, or signer mismatch")
    return "TRUSTED"


def write_notes(path: Path, notes: str) -> None:
    if not path.parent.is_dir():
        raise ReleaseError(f"output directory does not exist: {path.parent}")
    try:
        with path.open("x", encoding="utf-8") as output:
            output.write(notes)
    except FileExistsError as exc:
        raise ReleaseError(f"refusing to overwrite existing file: {path}") from exc


def read_json_input(path: Optional[Path]) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8") if path is not None else sys.stdin.read()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid JSON input: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError("JSON input must be an object")
    return value


def add_json_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", type=Path, help="read JSON object from a file (default: stdin)")


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

    classify_parser = subparsers.add_parser("classify", help="classify immutable PR title/labels from JSON")
    add_json_input(classify_parser)

    reconcile_parser = subparsers.add_parser("reconcile", help="reconcile ordered PR envelopes from JSON")
    add_json_input(reconcile_parser)

    authorize_parser = subparsers.add_parser("authorize", help="authorize a frozen effect envelope from JSON")
    add_json_input(authorize_parser)

    resume_parser = subparsers.add_parser("resume", help="validate full repair identity and phase from JSON")
    add_json_input(resume_parser)

    release_parser = subparsers.add_parser("compare-release", help="compare expected and observed Release JSON")
    add_json_input(release_parser)

    render_parser = subparsers.add_parser("render", help="render VERSION/CHANGELOG in a clean checkout")
    add_json_input(render_parser)
    render_parser.add_argument("--root", type=Path, default=ROOT)

    signer_parser = subparsers.add_parser("verify-tag", help="verify annotated trusted tag and exact target")
    signer_parser.add_argument("tag")
    signer_parser.add_argument("--target", required=True)
    signer_parser.add_argument(
        "--allowed-signers", type=Path, default=ROOT / ".github" / "release_allowed_signers"
    )
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
        elif args.command == "classify":
            payload = read_json_input(args.input)
            bump, reason = classify_release(
                str(payload["title"]), payload.get("labels", []), str(payload.get("current_version", "0.1.0"))
            )
            print(json.dumps({"bump": bump, "reason": reason}, sort_keys=True))
        elif args.command == "reconcile":
            payload = read_json_input(args.input)
            result = reconcile_batch(
                str(payload["current_version"]), payload.get("envelopes", []),
                {int(number) for number in payload.get("covered", [])},
            )
            print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        elif args.command == "authorize":
            payload = read_json_input(args.input)
            status = authorize_effect(
                str(payload["event_origin"]), payload.get("changed_paths", []), str(payload["base"])
            )
            print(json.dumps({"status": status}, sort_keys=True))
        elif args.command == "resume":
            payload = read_json_input(args.input)
            print(json.dumps({"status": resume_phase(payload["expected"], payload["observed"], payload["phase"])}, sort_keys=True))
        elif args.command == "compare-release":
            payload = read_json_input(args.input)
            print(json.dumps({"status": compare_release(payload["expected"], payload.get("observed"))}, sort_keys=True))
        elif args.command == "render":
            payload = read_json_input(args.input)
            root = args.root.resolve()
            version = str(payload["candidate_version"])
            changelog_path = root / "CHANGELOG.md"
            rendered = render_metadata(
                changelog_path.read_text(encoding="utf-8"), version, str(payload["notes"]),
                str(payload["release_date"]), str(payload["provenance"]),
            )
            (root / "VERSION").write_text(version + "\n", encoding="utf-8")
            changelog_path.write_text(rendered, encoding="utf-8")
            print(json.dumps({"status": "RENDERED", "version": version}, sort_keys=True))
        elif args.command == "verify-tag":
            print(verify_trusted_tag(ROOT, args.tag, args.allowed_signers, args.target))
        return 0
    except (KeyError, OSError, UnicodeError, ReleaseError) as exc:
        print(f"release refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
