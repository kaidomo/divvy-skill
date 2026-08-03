#!/usr/bin/env python3
"""Compare a host-local ROSTER with explicit, reproducible observations.

The probe only reads its inputs. It never edits the ROSTER and never treats the
public template as live host truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
INIT_STATE = ROOT / "scripts" / "init_state.py"
ROSTER_TEMPLATE = ROOT / "templates" / "ROSTER.md"
HOST_RE = re.compile(r"^- host:\s*`([^`]+)`\s*$", re.MULTILINE)
CAPABILITY_RE = re.compile(r"<!--\s*divvy-capability:\s*([^\s]+)\s*-->")
ROW_RE = re.compile(r"^\|\s*(T-[^|\s]+)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$")
CLAIM_LEVELS = {"configured", "callable", "usable"}
OBSERVED_LEVELS = CLAIM_LEVELS | {"absent", "auth-failed", "unverified"}


class ProbeError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_live_roster() -> Path:
    result = subprocess.run(
        [sys.executable, str(INIT_STATE), "paths"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ProbeError(f"init_state.py paths failed: {result.stderr.strip()}")
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    if not values.get("roster"):
        raise ProbeError("init_state.py paths did not return roster=")
    return Path(values["roster"]).resolve()


def load_observations(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProbeError(f"cannot read observations: {exc}") from exc
    if not isinstance(value, dict):
        raise ProbeError("observations root must be an object")
    return value


def parse_roster(text: str) -> tuple[str | None, dict[str, str]]:
    hosts = HOST_RE.findall(text)
    if len(hosts) > 1:
        raise ProbeError(f"ROSTER must declare at most one host; found {len(hosts)}")
    host = hosts[0] if hosts else None
    rows: dict[str, str] = {}
    for line in text.splitlines():
        match = ROW_RE.match(line)
        if match:
            row_id = match.group(1).strip()
            if row_id in rows:
                raise ProbeError(f"duplicate ROSTER row ID: {row_id}")
            rows[row_id] = match.group(4).strip()
    return host, rows


def observation_map(value: dict[str, Any]) -> dict[str, Any]:
    observations = value.get("observations")
    if not isinstance(observations, list):
        return {}
    mapped: dict[str, Any] = {}
    for item in observations:
        if isinstance(item, dict) and isinstance(item.get("row_id"), str):
            row_id = item["row_id"]
            mapped[row_id] = None if row_id in mapped else item
    return mapped


def report_row(
    row_id: str,
    host: str | None,
    host_matches: bool,
    roster_claim: str,
    observation: Any,
) -> dict[str, Any]:
    markers = CAPABILITY_RE.findall(roster_claim)
    claim_level = markers[0] if len(markers) == 1 and markers[0] in CLAIM_LEVELS else None

    valid_observation = isinstance(observation, dict)
    observed_level = observation.get("state") if valid_observation else None
    summary = observation.get("summary") if valid_observation else None
    evidence = observation.get("evidence_command") if valid_observation else None
    recommended = observation.get("recommended_text") if valid_observation else None
    fields_valid = all(isinstance(value, str) and value.strip() for value in (summary, evidence, recommended))
    observed_level_valid = observed_level in OBSERVED_LEVELS

    if not host or not host_matches or not claim_level or not fields_valid or not observed_level_valid:
        status = "unverified"
        observed_state = "unverified"
        if isinstance(summary, str) and summary.strip():
            observed_state += f": {summary.strip()}"
        evidence_command = evidence.strip() if isinstance(evidence, str) and evidence.strip() else "not available"
        recommended_text = (
            recommended.strip()
            if isinstance(recommended, str) and recommended.strip()
            else "Add an exact host, one divvy-capability marker, and a complete observation before deciding."
        )
    elif observed_level == "auth-failed":
        status = "auth-failed"
        observed_state = f"auth-failed: {summary.strip()}"
        evidence_command = evidence.strip()
        recommended_text = recommended.strip()
    elif observed_level == "unverified":
        status = "unverified"
        observed_state = f"unverified: {summary.strip()}"
        evidence_command = evidence.strip()
        recommended_text = recommended.strip()
    else:
        status = "match" if claim_level == observed_level else "drift"
        observed_state = f"{observed_level}: {summary.strip()}"
        evidence_command = evidence.strip()
        recommended_text = recommended.strip()

    return {
        "row_id": row_id,
        "host": host or "",
        "roster_claim": roster_claim,
        "observed_state": observed_state,
        "evidence_command": evidence_command,
        "status": status,
        "recommended_text": recommended_text,
        "requires_human_decision": status != "match",
    }


def run(roster: Path, observations_path: Path) -> dict[str, Any]:
    roster = roster.resolve()
    if not roster.is_file():
        raise ProbeError(f"ROSTER does not exist or is not a regular file: {roster}")
    if ROSTER_TEMPLATE.is_file() and roster.samefile(ROSTER_TEMPLATE):
        raise ProbeError("public templates/ROSTER.md is an example, not live host truth")

    before = sha256(roster)
    roster_text = roster.read_text(encoding="utf-8")
    host, rows = parse_roster(roster_text)
    raw_observations = load_observations(observations_path)
    observed_host = raw_observations.get("host")
    host_matches = isinstance(observed_host, str) and bool(host) and observed_host == host
    mapped = observation_map(raw_observations)

    row_ids = list(rows)
    row_ids.extend(row_id for row_id in mapped if row_id not in rows)
    results = [
        report_row(row_id, host, host_matches, rows.get(row_id, ""), mapped.get(row_id))
        for row_id in row_ids
    ]
    after = sha256(roster)
    if before != after:
        raise ProbeError("ROSTER changed while the read-only probe was running")
    return {
        "roster_sha256_before": before,
        "roster_sha256_after": after,
        "roster_unchanged": True,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="read-only host-local ROSTER drift probe")
    parser.add_argument("--roster", type=Path, help="explicit fixture or host-local ROSTER path")
    parser.add_argument("--observations", type=Path, required=True, help="JSON observations file")
    args = parser.parse_args()
    try:
        roster = args.roster.resolve() if args.roster else resolve_live_roster()
        print(json.dumps(run(roster, args.observations), ensure_ascii=False, indent=2))
        return 0
    except (OSError, UnicodeError, ProbeError) as exc:
        print(f"probe refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
