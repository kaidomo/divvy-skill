#!/usr/bin/env python3
"""Validate divvy release metadata and render release notes."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib import request


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
    "stop_owner",
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
    normalized = []
    for event in events:
        event_id = event.get("id")
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 0:
            raise ReleaseError("INVALID_TIMELINE_EVENT_ID")
        normalized.append(event)
    ordered = sorted(normalized, key=lambda event: (event.get("created_at", ""), event["id"]))
    for event in ordered:
        event_id = event.get("id")
        if event_id in seen:
            continue
        seen.add(event_id)
        label = (event.get("label") or {}).get("name")
        if label not in RELEASE_LABELS:
            continue
        occurred = parse_timestamp(event.get("created_at", ""))
        if occurred >= boundary:
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
    unique = {
        item.get("number"): item
        for item in associations
        if (item.get("base", {}).get("ref") if isinstance(item.get("base"), dict) else item.get("base")) == base
    }
    merged = [item for item in unique.values() if item.get("merge_sha") and item.get("merge_time")]
    if not merged:
        merged = [
            item for item in unique.values()
            if item.get("merge_commit_sha") and item.get("merged_at") and item.get("state") == "closed"
        ]
    if len(merged) != 1:
        raise ReleaseError("UNBOUND_MAIN_PUSH: expected exactly one merged PR association")
    return merged[0]


def flatten_pages(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise ReleaseError("INCOMPLETE_EVIDENCE: paginated response is not an array")
    flattened: List[Dict[str, Any]] = []
    for item in value:
        if isinstance(item, list):
            if not all(isinstance(entry, dict) for entry in item):
                raise ReleaseError("INCOMPLETE_EVIDENCE: invalid page item")
            flattened.extend(item)
        elif isinstance(item, dict):
            flattened.append(item)
        else:
            raise ReleaseError("INCOMPLETE_EVIDENCE: invalid page")
    return flattened


def plan_event(
    event: Dict[str, Any],
    associations: Any,
    timeline: Any,
    commit_message: str,
    allowed_actors: Set[str],
    current_version: str,
    prior_tag: str,
    covered: Set[int],
) -> Dict[str, Any]:
    return plan_batch(
        event,
        [{"sha": event.get("after"), "associations": associations, "timeline": timeline,
          "commit_message": commit_message}],
        allowed_actors,
        current_version,
        prior_tag,
        covered,
    )


def plan_batch(
    event: Dict[str, Any],
    evidence: Iterable[Dict[str, Any]],
    allowed_actors: Set[str],
    current_version: str,
    prior_tag: str,
    covered: Set[int],
) -> Dict[str, Any]:
    repository = (event.get("repository") or {}).get("full_name")
    frontier = event.get("after")
    if repository != "kaidomo/divvy-skill" or event.get("ref") != "refs/heads/main":
        raise ReleaseError("UNBOUND_MAIN_PUSH: repository/ref mismatch")
    if not isinstance(frontier, str) or not re.fullmatch(r"[0-9a-f]{40}", frontier):
        raise ReleaseError("UNBOUND_MAIN_PUSH: invalid source frontier")
    envelopes = []
    seen_shas = set()
    for item in evidence:
        sha = item.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or sha in seen_shas:
            raise ReleaseError("UNBOUND_MAIN_PUSH: invalid or duplicate batch commit")
        seen_shas.add(sha)
        pr = bind_associated_pr(flatten_pages(item.get("associations")), "main")
        number = int(pr["number"])
        merge_sha = pr.get("merge_sha") or pr.get("merge_commit_sha")
        merge_time = pr.get("merge_time") or pr.get("merged_at")
        if merge_sha != sha:
            raise ReleaseError("UNBOUND_MAIN_PUSH: associated PR does not bind batch commit")
        commit_message = str(item.get("commit_message", ""))
        title = immutable_title(commit_message, number)
        labels = labels_at_merge(
            flatten_pages(item.get("timeline")), str(merge_time), allowed_actors, pages_complete=True
        )
        envelopes.append({
            "number": number,
            "base": "main",
            "merge_sha": merge_sha,
            "merge_time": merge_time,
            "merger": (pr.get("merged_by") or {}).get("login"),
            "commit_message_digest": hashlib.sha256(commit_message.encode("utf-8")).hexdigest(),
            "labels": labels,
            "title": title,
            "note_digest": hashlib.sha256(render_note(number, title).encode("utf-8")).hexdigest(),
        })
    if not envelopes or envelopes[-1]["merge_sha"] != frontier:
        raise ReleaseError("UNBOUND_MAIN_PUSH: batch does not end at event frontier")
    numbers = [envelope["number"] for envelope in envelopes]
    if len(numbers) != len(set(numbers)):
        raise ReleaseError("UNBOUND_MAIN_PUSH: duplicate PR in batch")
    result = reconcile_batch(current_version, envelopes, covered)
    result.update({
        "schema_version": 1,
        "repository": repository,
        "source_frontier": frontier,
        "prior_tag": prior_tag,
        "envelopes": envelopes,
        "pr_set_digest": value_sha256(numbers),
        "label_note_digest": value_sha256(
            [[envelope["labels"], envelope["note_digest"]] for envelope in envelopes]
        ),
        "label_actor_allowlist_digest": value_sha256(sorted(allowed_actors)),
        "event_digest": value_sha256(event),
        "batch_id": compute_batch_id("1", frontier, prior_tag, envelopes),
    })
    return result


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


def canonical_release_body(body: str) -> str:
    """Normalize all newline forms before comparing Release bodies.

    Matches docauth `scripts/check_release.py` and docloop
    `tools/check_release.py`: CRLF/CR are folded to LF and the result is
    made to end in exactly one trailing newline.
    """
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.rstrip("\n") + "\n"


def compare_release(expected: Dict[str, Any], observed: Optional[Dict[str, Any]]) -> str:
    if observed is None:
        return "CREATE_RELEASE"
    fields = ("tag_name", "name", "body", "draft", "prerelease")

    def normalized_field(payload: Dict[str, Any], field: str) -> Any:
        value = payload.get(field)
        if field == "body" and isinstance(value, str):
            return canonical_release_body(value)
        return value

    differing = [field for field in fields if normalized_field(expected, field) != normalized_field(observed, field)]
    if differing:
        raise ReleaseError(f"PUBLIC_RELEASE_MISMATCH: {','.join(differing)}; refusing overwrite")
    return "MATCHING_NOOP"


def find_release_by_tag(pages: Any, tag: str) -> Optional[Dict[str, Any]]:
    """Select a GitHub Release by tag from one or more `gh api --paginate --slurp` pages.

    Accepts either a flat list of Release objects (a single, unpaginated
    `gh api` response) or a list of per-page lists (`--paginate --slurp`
    output), via the same `flatten_pages` tolerance used for timeline
    evidence. Returns the first match, or None if the tag has no Release yet.
    """
    for release in flatten_pages(pages):
        if release.get("tag_name") == tag:
            return release
    return None


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
    section = f"\n\n## [{version}] - {release_date}\n\n{notes.rstrip()}"
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


def base64url(value: bytes) -> bytes:
    import base64
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def mint_app_token(
    app_id: str,
    installation_id: str,
    private_key: str,
    repository: str,
    api_url: str = "https://api.github.com",
) -> Dict[str, Any]:
    if repository != "kaidomo/divvy-skill":
        raise ReleaseError("APP_SCOPE_MISMATCH: repository")
    now = int(time.time())
    signing_input = b".".join((
        base64url(canonical_json({"alg": "RS256", "typ": "JWT"})),
        base64url(canonical_json({"iat": now - 60, "exp": now + 540, "iss": app_id})),
    ))
    with tempfile.TemporaryDirectory() as raw:
        key_path = Path(raw) / "app.pem"
        key_path.write_text(private_key, encoding="utf-8")
        key_path.chmod(0o600)
        try:
            signed = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", str(key_path)], input=signing_input,
                check=True, capture_output=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ReleaseError(f"APP_TOKEN_MINT_FAILED: JWT signing: {exc}") from exc
    jwt = (signing_input + b"." + base64url(signed)).decode("ascii")
    body = canonical_json({
        "repositories": ["divvy-skill"],
        "permissions": {"contents": "write", "metadata": "read"},
    })
    req = request.Request(
        f"{api_url}/app/installations/{installation_id}/access_tokens",
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read())
    except Exception as exc:
        raise ReleaseError(f"APP_TOKEN_MINT_FAILED: {exc}") from exc
    token = payload.get("token")
    expiry = parse_timestamp(str(payload.get("expires_at")))
    repositories = {item.get("full_name") for item in payload.get("repositories", [])}
    permissions = payload.get("permissions", {})
    if not token or repositories != {repository}:
        raise ReleaseError("APP_SCOPE_MISMATCH: returned repository set")
    if permissions != {"contents": "write", "metadata": "read"}:
        raise ReleaseError("APP_PERMISSION_MISMATCH")
    ttl = int((expiry - datetime.now(timezone.utc)).total_seconds())
    if ttl <= 0 or ttl > 3600:
        raise ReleaseError("APP_TTL_MISMATCH")
    return {"token": token, "expires_at": payload["expires_at"], "permissions": permissions,
            "repositories": sorted(repositories), "ttl_seconds": ttl}


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


def read_json_array_input(path: Optional[Path]) -> Any:
    try:
        raw = path.read_text(encoding="utf-8") if path is not None else sys.stdin.read()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid JSON input: {exc}") from exc
    if not isinstance(value, list):
        raise ReleaseError("JSON input must be an array")
    return value


def add_json_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", type=Path, help="read JSON object from a file (default: stdin)")


def add_json_array_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", type=Path, help="read a JSON array from a file (default: stdin)")


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

    find_release_parser = subparsers.add_parser(
        "find-release", help="select a Release by tag from a gh api releases list (single- or multi-page)"
    )
    add_json_array_input(find_release_parser)
    find_release_parser.add_argument("--tag", required=True)

    render_parser = subparsers.add_parser("render", help="render VERSION/CHANGELOG in a clean checkout")
    add_json_input(render_parser)
    render_parser.add_argument("--root", type=Path, default=ROOT)

    signer_parser = subparsers.add_parser("verify-tag", help="verify annotated trusted tag and exact target")
    signer_parser.add_argument("tag")
    signer_parser.add_argument("--target", required=True)
    signer_parser.add_argument(
        "--allowed-signers", type=Path, default=ROOT / ".github" / "release_allowed_signers"
    )

    plan_parser = subparsers.add_parser("plan-event", help="bind a push event to immutable PR evidence")
    plan_parser.add_argument("--event", type=Path, required=True)
    plan_parser.add_argument("--associations", type=Path, required=True)
    plan_parser.add_argument("--timeline", type=Path, required=True)
    plan_parser.add_argument("--commit-message", type=Path, required=True)
    plan_parser.add_argument("--allowed-actors", type=Path, default=ROOT / ".github" / "release_label_actors")
    plan_parser.add_argument("--prior-tag", required=True)
    plan_parser.add_argument("--covered", type=Path)
    plan_parser.add_argument("--output", type=Path, required=True)

    batch_parser = subparsers.add_parser("plan-batch", help="reconcile ordered first-parent PR evidence")
    batch_parser.add_argument("--event", type=Path, required=True)
    batch_parser.add_argument("--evidence-dir", type=Path, required=True)
    batch_parser.add_argument("--allowed-actors", type=Path, default=ROOT / ".github" / "release_label_actors")
    batch_parser.add_argument("--prior-tag", required=True)
    batch_parser.add_argument("--covered", type=Path)
    batch_parser.add_argument("--output", type=Path, required=True)

    token_parser = subparsers.add_parser("mint-app-token", help="mint one short-lived repository-scoped App token")
    token_parser.add_argument("--repository", required=True)
    token_parser.add_argument("--github-output", type=Path, required=True)
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
        elif args.command == "find-release":
            pages = read_json_array_input(args.input)
            print(json.dumps({"release": find_release_by_tag(pages, args.tag)}, sort_keys=True))
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
        elif args.command == "plan-event":
            event = json.loads(args.event.read_text(encoding="utf-8"))
            associations = json.loads(args.associations.read_text(encoding="utf-8"))
            timeline = json.loads(args.timeline.read_text(encoding="utf-8"))
            allowed_actors = {
                line.strip() for line in args.allowed_actors.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
            covered = set()
            if args.covered is not None:
                covered = {int(value) for value in json.loads(args.covered.read_text(encoding="utf-8"))}
            result = plan_event(
                event, associations, timeline, args.commit_message.read_text(encoding="utf-8"),
                allowed_actors, read_version(), args.prior_tag, covered,
            )
            write_notes(args.output, json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")
            print(f"release plan written: {args.output}")
        elif args.command == "plan-batch":
            event = json.loads(args.event.read_text(encoding="utf-8"))
            allowed_actors = {
                line.strip() for line in args.allowed_actors.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
            covered = set()
            if args.covered is not None:
                covered = {int(value) for value in json.loads(args.covered.read_text(encoding="utf-8"))}
            evidence = []
            directories = sorted(
                (path for path in args.evidence_dir.iterdir() if path.is_dir()),
                key=lambda path: int(path.name),
            )
            for directory in directories:
                evidence.append({
                    "sha": (directory / "sha.txt").read_text(encoding="ascii").strip(),
                    "associations": json.loads((directory / "associations.json").read_text(encoding="utf-8")),
                    "timeline": json.loads((directory / "timeline.json").read_text(encoding="utf-8")),
                    "commit_message": (directory / "commit-message.txt").read_text(encoding="utf-8"),
                })
            result = plan_batch(event, evidence, allowed_actors, read_version(), args.prior_tag, covered)
            write_notes(args.output, json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")
            print(f"release batch plan written: {args.output}")
        elif args.command == "mint-app-token":
            app_id = os.environ.get("RELEASE_APP_ID", "")
            installation_id = os.environ.get("RELEASE_APP_INSTALLATION_ID", "")
            private_key = os.environ.get("RELEASE_APP_PRIVATE_KEY", "")
            if not app_id or not installation_id or not private_key:
                raise ReleaseError("APP_TOKEN_MINT_FAILED: required environment is missing")
            result = mint_app_token(app_id, installation_id, private_key, args.repository)
            print(f"::add-mask::{result['token']}")
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(f"token={result['token']}\n")
                output.write(f"expires_at={result['expires_at']}\n")
                output.write(f"ttl_seconds={result['ttl_seconds']}\n")
                output.write(f"repositories={json.dumps(result['repositories'], separators=(',', ':'))}\n")
                output.write(f"permissions={json.dumps(result['permissions'], sort_keys=True, separators=(',', ':'))}\n")
            print("repository-scoped App token minted and masked")
        return 0
    except (KeyError, OSError, UnicodeError, ReleaseError) as exc:
        print(f"release refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
