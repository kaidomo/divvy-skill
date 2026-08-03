#!/usr/bin/env python3
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "scripts" / "roster_probe.py"
TEMPLATE = ROOT / "templates" / "ROSTER.md"
PROBE_SPEC = importlib.util.spec_from_file_location("divvy_roster_probe", PROBE)
assert PROBE_SPEC and PROBE_SPEC.loader
PROBE_MODULE = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(PROBE_MODULE)


def roster(host="host-a", rows=None):
    rows = rows or [
        ("T-1", "usable"),
        ("T-2", "usable"),
        ("T-3", "configured"),
        ("T-4", None),
    ]
    host_line = f"- host: `{host}`\n" if host is not None else ""
    body = ["# fixture", "", host_line.rstrip(), "", "| ID | item | CLAUDE | CODEX |", "|---|---|---|---|"]
    for row_id, claim in rows:
        marker = f" <!-- divvy-capability: {claim} -->" if claim else ""
        body.append(f"| {row_id} | fixture | fixture | claim{marker} |")
    return "\n".join(body) + "\n"


def observation(row_id, state, summary="fixture observation"):
    return {
        "row_id": row_id,
        "state": state,
        "summary": summary,
        "evidence_command": f"fixture-check {row_id}",
        "recommended_text": f"recommended {row_id}",
    }


class RosterProbeTests(unittest.TestCase):
    def run_probe(self, roster_text, observations, env=None, explicit=True):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        roster_path = root / "ROSTER.md"
        observations_path = root / "observations.json"
        roster_path.write_text(roster_text, encoding="utf-8")
        observations_path.write_text(json.dumps(observations), encoding="utf-8")
        args = [sys.executable, str(PROBE), "--observations", str(observations_path)]
        if explicit:
            args.extend(["--roster", str(roster_path)])
        run_env = dict(os.environ)
        run_env.update(env or {})
        result = subprocess.run(args, capture_output=True, text=True, env=run_env)
        return result, roster_path

    def test_four_statuses_and_auth_failure_is_not_usable(self):
        observations = {
            "host": "host-a",
            "observations": [
                observation("T-1", "usable"),
                observation("T-2", "callable"),
                observation("T-3", "auth-failed", "configured but authentication failed"),
                observation("T-4", "unverified"),
            ],
        }
        result, _ = self.run_probe(roster(), observations)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {item["row_id"]: item for item in json.loads(result.stdout)["results"]}
        self.assertEqual(rows["T-1"]["status"], "match")
        self.assertEqual(rows["T-2"]["status"], "drift")
        self.assertEqual(rows["T-3"]["status"], "auth-failed")
        self.assertNotEqual(rows["T-3"]["status"], "match")
        self.assertEqual(rows["T-4"]["status"], "unverified")

    def test_missing_host_fails_closed(self):
        result, _ = self.run_probe(
            roster(host=None, rows=[("T-1", "usable")]),
            {"host": "host-a", "observations": [observation("T-1", "usable")]},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["results"][0]["status"], "unverified")

    def test_mismatched_host_fails_closed(self):
        result, _ = self.run_probe(
            roster(rows=[("T-1", "usable")]),
            {"host": "host-b", "observations": [observation("T-1", "usable")]},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["results"][0]["status"], "unverified")

    def test_duplicate_host_declarations_are_rejected(self):
        roster_text = roster(rows=[("T-1", "usable")]) + "\n- host: `host-b`\n"
        value = {"host": "host-a", "observations": [observation("T-1", "usable")]}
        result, _ = self.run_probe(roster_text, value)
        self.assertEqual(result.returncode, 2)
        self.assertIn("at most one host", result.stderr)

    def test_duplicate_roster_rows_are_rejected(self):
        value = {"host": "host-a", "observations": [observation("T-1", "usable")]}
        result, _ = self.run_probe(roster(rows=[("T-1", "usable"), ("T-1", "configured")]), value)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate ROSTER row ID: T-1", result.stderr)

    def test_missing_marker_and_incomplete_observation_fail_closed(self):
        value = {"host": "host-a", "observations": [{"row_id": "T-4", "state": "usable", "summary": "free prose"}]}
        result, _ = self.run_probe(roster(rows=[("T-4", None)]), value)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["results"][0]["status"], "unverified")

    def test_input_hash_is_unchanged(self):
        value = {"host": "host-a", "observations": [observation("T-1", "usable")]}
        result, roster_path = self.run_probe(roster(rows=[("T-1", "usable")]), value)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        digest = hashlib.sha256(roster_path.read_bytes()).hexdigest()
        self.assertTrue(payload["roster_unchanged"])
        self.assertEqual(payload["roster_sha256_before"], digest)
        self.assertEqual(payload["roster_sha256_after"], digest)

    def test_xdg_path_comes_from_init_state_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            roster_path = root / "config" / "divvy" / "ROSTER.md"
            roster_path.parent.mkdir(parents=True)
            roster_path.write_text(roster(rows=[("T-1", "usable")]), encoding="utf-8")
            obs = root / "observations.json"
            obs.write_text(json.dumps({"host": "host-a", "observations": [observation("T-1", "usable")]}))
            env = dict(os.environ)
            env.update({"XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state")})
            for key in ("DIVVY_CONFIG_DIR", "DIVVY_ROSTER"):
                env.pop(key, None)
            result = subprocess.run(
                [sys.executable, str(PROBE), "--observations", str(obs)],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["results"][0]["status"], "match")

    def test_direct_override_comes_from_init_state_paths(self):
        value = {"host": "host-a", "observations": [observation("T-1", "usable")]}
        result, roster_path = self.run_probe(roster(rows=[("T-1", "usable")]), value, explicit=False)
        obs_path = Path(result.args[result.args.index("--observations") + 1])
        env = dict(os.environ)
        env["DIVVY_ROSTER"] = str(roster_path)
        rerun = subprocess.run(
            [sys.executable, str(PROBE), "--observations", str(obs_path)],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(json.loads(rerun.stdout)["results"][0]["status"], "match")

    def test_public_template_is_never_live_truth(self):
        with tempfile.TemporaryDirectory() as temp:
            obs = Path(temp) / "observations.json"
            obs.write_text(json.dumps({"host": "host-a", "observations": []}))
            env = dict(os.environ)
            env["DIVVY_ROSTER"] = str(TEMPLATE)
            result = subprocess.run(
                [sys.executable, str(PROBE), "--observations", str(obs)],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("example, not live host truth", result.stderr)

    def test_missing_public_template_does_not_hide_a_valid_live_roster(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            roster_path = root / "ROSTER.md"
            observations_path = root / "observations.json"
            roster_path.write_text(roster(rows=[("T-1", "usable")]), encoding="utf-8")
            observations_path.write_text(
                json.dumps({"host": "host-a", "observations": [observation("T-1", "usable")]}),
                encoding="utf-8",
            )
            original_template = PROBE_MODULE.ROSTER_TEMPLATE
            self.addCleanup(setattr, PROBE_MODULE, "ROSTER_TEMPLATE", original_template)
            PROBE_MODULE.ROSTER_TEMPLATE = root / "missing-template.md"
            payload = PROBE_MODULE.run(roster_path, observations_path)
            self.assertEqual(payload["results"][0]["status"], "match")

    def test_duplicate_observations_fail_closed(self):
        value = {
            "host": "host-a",
            "observations": [observation("T-1", "usable"), observation("T-1", "configured")],
        }
        result, _ = self.run_probe(roster(rows=[("T-1", "usable")]), value)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["results"][0]["status"], "unverified")

    def test_native_child_and_tmux_workflow_remain_separate(self):
        rows = [("T-5a", "usable"), ("T-5b", "callable")]
        value = {"host": "host-a", "observations": [observation("T-5a", "usable"), observation("T-5b", "callable")]}
        result, _ = self.run_probe(roster(rows=rows), value)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["results"]
        self.assertEqual([item["row_id"] for item in output], ["T-5a", "T-5b"])
        self.assertTrue(all(item["status"] == "match" for item in output))


if __name__ == "__main__":
    unittest.main()
