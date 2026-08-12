#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release.py"
SPEC = importlib.util.spec_from_file_location("divvy_release", SCRIPT)
assert SPEC and SPEC.loader
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)


def fixture(root: Path, version: str = "1.2.3", notes: str = "### Fixed\n\n- A durable fix.\n") -> None:
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2026-08-04\n\n{notes}",
        encoding="utf-8",
    )


class ReleaseMetadataTests(unittest.TestCase):
    def test_repository_release_metadata_is_valid(self) -> None:
        version = RELEASE.read_version(ROOT)
        self.assertEqual(RELEASE.check_release(ROOT, f"v{version}"), version)

    def test_check_requires_exact_tag(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            self.assertEqual(RELEASE.check_release(root, "v1.2.3"), "1.2.3")
            with self.assertRaises(RELEASE.ReleaseError):
                RELEASE.check_release(root, "v1.2.4")

    def test_version_file_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            (root / "VERSION").write_text("01.2.3\n", encoding="utf-8")
            with self.assertRaises(RELEASE.ReleaseError):
                RELEASE.read_version(root)

    def test_version_file_rejects_prereleases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root, version="1.2.3-rc.1")
            with self.assertRaises(RELEASE.ReleaseError):
                RELEASE.read_version(root)

    def test_release_heading_requires_a_real_date(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            changelog = root / "CHANGELOG.md"
            changelog.write_text(changelog.read_text().replace("2026-08-04", "2026-99-99"))
            with self.assertRaises(RELEASE.ReleaseError):
                RELEASE.check_release(root)

    def test_release_notes_reject_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root, notes="- TODO: write notes.\n")
            with self.assertRaises(RELEASE.ReleaseError):
                RELEASE.check_release(root)

    def test_release_notes_reject_duplicate_current_headings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            changelog = root / "CHANGELOG.md"
            changelog.write_text(
                changelog.read_text(encoding="utf-8")
                + "\n## [1.2.3] - 2026-08-05\n\n- Duplicate.\n",
                encoding="utf-8",
            )
            with self.assertRaises(RELEASE.ReleaseError):
                RELEASE.check_release(root)

    def test_release_history_requires_a_strictly_newer_version(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            with mock.patch.object(
                RELEASE,
                "git_release_versions",
                return_value=[("v1.2.2", (1, 2, 2))],
            ):
                self.assertEqual(RELEASE.check_release(root, "v1.2.3", history=True), "1.2.3")
            with mock.patch.object(
                RELEASE,
                "git_release_versions",
                return_value=[("v1.2.4", (1, 2, 4))],
            ):
                with self.assertRaises(RELEASE.ReleaseError):
                    RELEASE.check_release(root, "v1.2.3", history=True)

    def test_release_history_requires_a_tag(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            with self.assertRaises(RELEASE.ReleaseError):
                RELEASE.check_release(root, history=True)

    def test_next_version(self) -> None:
        self.assertEqual(RELEASE.next_version("1.2.3", "patch"), "1.2.4")
        self.assertEqual(RELEASE.next_version("1.2.3", "minor"), "1.3.0")
        self.assertEqual(RELEASE.next_version("1.2.3", "major"), "2.0.0")

    def test_release_notes_output_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "notes.md"
            RELEASE.write_notes(path, "first\n")
            with self.assertRaises(RELEASE.ReleaseError):
                RELEASE.write_notes(path, "second\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "first\n")

    def test_release_notes_output_requires_existing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "missing" / "notes.md"
            with self.assertRaises(RELEASE.ReleaseError):
                RELEASE.write_notes(path, "notes\n")

    def test_workflows_validate_before_publishing(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/release.py check", ci)
        self.assertIn("runs-on: macos-latest", ci)
        self.assertIn("permissions:\n  contents: read", release)
        self.assertIn("runs-on: macos-latest", release)
        self.assertIn("branches:\n      - main", release)
        self.assertIn("workflow_dispatch:", release)
        self.assertIn("group: release-${{ github.repository }}-main", release)
        self.assertIn("queue: max", release)
        self.assertNotIn("cancel-in-progress", release)
        self.assertIn("environment: release-automation", release)
        self.assertIn("pull-requests: read", release)
        self.assertNotIn("issues: write", release)
        publish = release.split("  publish:\n", 1)[1]
        self.assertIn("environment: release-automation", publish)
        self.assertIn("permissions:\n      contents: read", publish)
        self.assertNotIn("github.token", publish)
        self.assertIn("RELEASE_APP_PRIVATE_KEY", publish)
        self.assertIn("ref: ${{ needs.commit-sign-tag.outputs.metadata_sha }}", publish)
        self.assertIn("persist-credentials: false", publish)
        self.assertIn("--repository \"$GITHUB_REPOSITORY\"", publish)
        self.assertIn("--github-output \"$GITHUB_OUTPUT\"", publish)
        self.assertIn("GH_TOKEN: ${{ steps.app.outputs.token }}", publish)
        self.assertIn("RELEASE_APP_PRIVATE_KEY", release)
        self.assertIn("RELEASE_SIGNING_KEY", release)
        self.assertNotIn("BLOCKED_REPOSITORY_POLICY", release)
        self.assertEqual(release.count("mint-app-token"), 2)
        self.assertIn("GH_TOKEN: ${{ steps.app.outputs.token }}", release)
        self.assertIn("Exact Release reconciliation with App authorization", release)
        self.assertIn("git push --atomic", release)
        self.assertIn("PUBLIC_RELEASE_MISMATCH", release)
        self.assertIn("Release-Expected-Parent", release)
        self.assertIn("github.event.repository.full_name", release)
        self.assertNotIn("pull_request_target", release)
        self.assertIn("fetch-depth: 2", ci)
        self.assertEqual(ci.count("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"), 1)
        self.assertEqual(release.count("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"), 3)


class AutomaticReleaseContractTests(unittest.TestCase):
    def envelope(self, title: str = "fix: durable repair", labels=None, number: int = 7):
        return {
            "number": number,
            "base": "main",
            "merge_sha": f"{number:040x}",
            "merge_time": "2026-08-06T10:00:00Z",
            "commit_message": f"{title} (#{number})\n",
            "labels": labels or [],
            "title": title,
        }

    def test_semantic_floor_matrix(self) -> None:
        self.assertEqual(RELEASE.classify_release("feat: add routing", []), ("minor", "feat"))
        self.assertEqual(RELEASE.classify_release("fix: repair state", []), ("patch", "fix"))
        self.assertEqual(RELEASE.classify_release("feat!: replace contract", []), ("minor", "breaking_0x"))
        self.assertEqual(RELEASE.classify_release("docs: clarify", []), ("patch", "unknown_default_patch"))

    def test_valid_overrides_and_lower_floor_conflict(self) -> None:
        self.assertEqual(RELEASE.classify_release("fix: repair", ["release:minor"])[0], "minor")
        self.assertEqual(RELEASE.classify_release("fix: repair", ["release:major"])[0], "major")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "LABEL_FLOOR_CONFLICT"):
            RELEASE.classify_release("feat: capability", ["release:patch"])

    def test_label_conflicts_fail_closed(self) -> None:
        for labels in (
            ["release:skip", "release:patch"],
            ["release:minor", "release:patch"],
        ):
            with self.subTest(labels=labels):
                with self.assertRaisesRegex(RELEASE.ReleaseError, "LABEL_CONFLICT"):
                    RELEASE.classify_release("fix: repair", labels)

    def test_skip_is_an_audited_no_cut(self) -> None:
        self.assertEqual(
            RELEASE.classify_release("feat: deferred", ["release:skip"]),
            (None, "release_skip"),
        )

    def test_merge_time_labels_require_allowlisted_actor_and_complete_pages(self) -> None:
        events = [
            {"id": 1, "event": "labeled", "label": {"name": "release:minor"},
             "actor": {"login": "kaidomo"}, "created_at": "2026-08-06T09:00:00Z"},
            {"id": 1, "event": "labeled", "label": {"name": "release:minor"},
             "actor": {"login": "kaidomo"}, "created_at": "2026-08-06T09:00:00Z"},
        ]
        self.assertEqual(
            RELEASE.labels_at_merge(events, "2026-08-06T10:00:00Z", {"kaidomo"}, pages_complete=True),
            ["release:minor"],
        )
        with self.assertRaisesRegex(RELEASE.ReleaseError, "LABEL_ACTOR_UNTRUSTED"):
            RELEASE.labels_at_merge([{**events[0], "actor": {"login": "mallory"}}],
                                    "2026-08-06T10:00:00Z", {"kaidomo"}, pages_complete=True)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "INCOMPLETE_EVIDENCE"):
            RELEASE.labels_at_merge(events, "2026-08-06T10:00:00Z", {"kaidomo"}, pages_complete=False)

    def test_post_merge_release_label_mutation_is_rejected(self) -> None:
        events = [{"id": 2, "event": "labeled", "label": {"name": "release:skip"},
                   "actor": {"login": "kaidomo"}, "created_at": "2026-08-06T11:00:00Z"}]
        with self.assertRaisesRegex(RELEASE.ReleaseError, "POST_MERGE_LABEL_MUTATION"):
            RELEASE.labels_at_merge(events, "2026-08-06T10:00:00Z", {"kaidomo"}, pages_complete=True)

    def test_commit_message_binding_accepts_merge_and_squash_only(self) -> None:
        self.assertEqual(
            RELEASE.immutable_title("Merge pull request #7 from x/y\n\nfeat: safe title\n", 7),
            "feat: safe title",
        )
        self.assertEqual(RELEASE.immutable_title("fix: safe literal (#7)\n", 7), "fix: safe literal")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "UNBOUND_MAIN_PUSH"):
            RELEASE.immutable_title("direct push", 7)

    def test_ambiguous_association_and_fork_sensitive_change_fail(self) -> None:
        with self.assertRaisesRegex(RELEASE.ReleaseError, "UNBOUND_MAIN_PUSH"):
            RELEASE.bind_associated_pr([self.envelope(number=1), self.envelope(number=2)], "main")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "SENSITIVE_PATH_DENIED"):
            RELEASE.authorize_effect("fork", [".github/workflows/release.yml"], "main")

    def test_literal_note_safety_and_controls(self) -> None:
        note = RELEASE.render_note(9, "fix: `$(touch nope)` ${HOME} | literal")
        self.assertIn("`$(touch nope)`", note)
        self.assertIn("${HOME}", note)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "UNSAFE_NOTE"):
            RELEASE.render_note(9, "fix: hidden\x00control")

    def test_batch_reconciliation_skip_then_feat_and_mixed(self) -> None:
        skipped = self.envelope("chore: defer", ["release:skip"], 7)
        feature = self.envelope("feat: capability", [], 8)
        result = RELEASE.reconcile_batch("0.1.0", [skipped, feature], covered=set())
        self.assertEqual(result["candidate_version"], "0.2.0")
        self.assertEqual(result["released_prs"], [8])
        self.assertEqual(result["skipped_prs"], [7])
        self.assertNotIn("defer", result["notes"])

    def test_duplicate_rerun_is_zero_effect(self) -> None:
        item = self.envelope(number=8)
        result = RELEASE.reconcile_batch("0.1.0", [item], covered={8})
        self.assertEqual(result["effect_count"], 0)
        self.assertEqual(result["status"], "MATCHING_NOOP")

    def test_batch_id_is_ordered_and_deterministic(self) -> None:
        first = self.envelope(number=8)
        second = self.envelope(number=9)
        a = RELEASE.compute_batch_id("v1", "a" * 40, "v0.1.0", [first, second])
        self.assertEqual(a, RELEASE.compute_batch_id("v1", "a" * 40, "v0.1.0", [first, second]))
        self.assertNotEqual(a, RELEASE.compute_batch_id("v1", "a" * 40, "v0.1.0", [second, first]))

    def test_cas_allows_one_recompute_then_stops(self) -> None:
        self.assertEqual(RELEASE.cas_decision("a", "b", 0), "RECOMPUTE")
        self.assertEqual(RELEASE.cas_decision("a", "a", 1), "MATCH")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "CAS_EXHAUSTED"):
            RELEASE.cas_decision("a", "b", 1)

    def test_partial_resume_requires_full_identity(self) -> None:
        expected = {"schema_version": 1, "batch_id": "b", "frontier": "f", "prior_tag": "v0.1.0",
                    "pr_set_digest": "p", "label_note_digest": "n", "metadata_tree": "t",
                    "candidate_version": "0.1.1", "expected_parent": "f"}
        self.assertEqual(RELEASE.resume_phase(expected, dict(expected), "metadata"), "TAG_PENDING")
        wrong = dict(expected, metadata_tree="wrong")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "RESUME_IDENTITY_MISMATCH"):
            RELEASE.resume_phase(expected, wrong, "metadata")

    def test_release_four_way_equality_and_no_overwrite(self) -> None:
        expected = {"tag_name": "v0.1.1", "target_commitish": "a" * 40,
                    "name": "v0.1.1", "body": "- fix (#7)\n", "draft": False, "prerelease": False}
        self.assertEqual(RELEASE.compare_release(expected, dict(expected)), "MATCHING_NOOP")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "PUBLIC_RELEASE_MISMATCH"):
            RELEASE.compare_release(expected, dict(expected, body="wrong"))

    def test_four_way_mismatch_names_differing_fields(self) -> None:
        with self.assertRaisesRegex(RELEASE.ReleaseError, "VERSION, CHANGELOG"):
            RELEASE.four_way_equal("0.1.1", "0.1.2", "v0.1.2", "v0.1.2")

    def test_queue_saturation_and_direct_push_stop(self) -> None:
        with self.assertRaisesRegex(RELEASE.ReleaseError, "QUEUE_SATURATED"):
            RELEASE.queue_guard(101)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "UNBOUND_MAIN_PUSH"):
            RELEASE.bind_associated_pr([], "main")

    def test_metadata_recursion_requires_full_trailers_and_paths(self) -> None:
        identity = {name: name for name in RELEASE.REPAIR_IDENTITY_FIELDS}
        self.assertEqual(RELEASE.metadata_recursion(identity, identity, ["VERSION", "CHANGELOG.md"]), "NOOP")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "UNTRUSTED_METADATA_PUSH"):
            RELEASE.metadata_recursion(identity, dict(identity, batch_id="wrong"), ["VERSION"])

    def test_redaction_failure_closes(self) -> None:
        self.assertEqual(RELEASE.redact("token=abcd", ["abcd"]), "token=***")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "REDACTION_FAILED"):
            RELEASE.redact("token=abcd", [])

    def test_conformance_files_are_versioned_and_digestible(self) -> None:
        schema = ROOT / ".github" / "release_conformance" / "v1" / "schema.json"
        vectors = ROOT / ".github" / "release_conformance" / "v1" / "vectors.json"
        self.assertEqual(json.loads(schema.read_text())["schema_version"], 1)
        self.assertGreaterEqual(len(json.loads(vectors.read_text())["vectors"]), 8)
        self.assertEqual(len(RELEASE.file_sha256(schema)), 64)

    def test_github_event_plan_binds_immutable_evidence(self) -> None:
        sha = "a" * 40
        event = {"repository": {"full_name": "kaidomo/divvy-skill"}, "ref": "refs/heads/main", "after": sha}
        associations = [[{"number": 12, "base": {"ref": "main"}, "state": "closed",
                          "merge_commit_sha": sha, "merged_at": "2026-08-06T10:00:00Z",
                          "merged_by": {"login": "kaidomo"}}]]
        timeline = [[{"id": 1, "event": "labeled", "label": {"name": "release:minor"},
                     "actor": {"login": "kaidomo"}, "created_at": "2026-08-06T09:00:00Z"}]]
        result = RELEASE.plan_event(event, associations, timeline, "fix: repair (#12)\n",
                                    {"kaidomo"}, "0.1.0", "v0.1.0", set())
        self.assertEqual(result["candidate_version"], "0.2.0")
        self.assertEqual(result["source_frontier"], sha)
        self.assertEqual(result["released_prs"], [12])

    def test_release_comparison_ignores_mutable_target_commitish_field(self) -> None:
        expected = {"tag_name": "v0.2.0", "target_commitish": "a" * 40, "name": "v0.2.0",
                    "body": "notes\n", "draft": False, "prerelease": False}
        observed = dict(expected, target_commitish="main")
        self.assertEqual(RELEASE.compare_release(expected, observed), "MATCHING_NOOP")

    def test_plan_batch_coalesces_ordered_skip_and_feature(self) -> None:
        first_sha, second_sha = "a" * 40, "b" * 40
        event = {"repository": {"full_name": "kaidomo/divvy-skill"}, "ref": "refs/heads/main",
                 "after": second_sha}
        def evidence(number, sha, title, label=None):
            timeline = [] if label is None else [{"id": number, "event": "labeled",
                "label": {"name": label}, "actor": {"login": "kaidomo"},
                "created_at": "2026-08-06T09:00:00Z"}]
            return {"sha": sha, "commit_message": f"{title} (#{number})\n", "timeline": [timeline],
                    "associations": [[{"number": number, "base": {"ref": "main"}, "state": "closed",
                    "merge_commit_sha": sha, "merged_at": "2026-08-06T10:00:00Z",
                    "merged_by": {"login": "kaidomo"}}]]}
        result = RELEASE.plan_batch(event, [
            evidence(20, first_sha, "chore: deferred", "release:skip"),
            evidence(21, second_sha, "feat: capability"),
        ], {"kaidomo"}, "0.1.0", "v0.1.0", set())
        self.assertEqual(result["skipped_prs"], [20])
        self.assertEqual(result["released_prs"], [21])
        self.assertEqual(result["candidate_version"], "0.2.0")

    def test_rendered_changelog_section_equals_release_body(self) -> None:
        body = "- fix: repair (#7)\n\n<!-- release-provenance: abc -->\n"
        rendered = RELEASE.render_metadata("# Changelog\n\n## [Unreleased]\n", "0.1.1", body,
                                           "2026-08-06", "abc")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root, version="0.1.1")
            (root / "CHANGELOG.md").write_text(rendered, encoding="utf-8")
            self.assertEqual(RELEASE.release_notes(root, "0.1.1"), body)

    def test_app_token_scope_fails_before_network_for_other_repository(self) -> None:
        with self.assertRaisesRegex(RELEASE.ReleaseError, "APP_SCOPE_MISMATCH"):
            RELEASE.mint_app_token("1", "2", "private", "kaidomo/other")


if __name__ == "__main__":
    unittest.main()
