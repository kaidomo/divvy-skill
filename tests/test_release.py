#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
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
        self.assertIn("permissions:\n  contents: write", release)
        self.assertIn("runs-on: macos-latest", release)
        self.assertIn('- "v[0-9]+.[0-9]+.[0-9]+"', release)
        self.assertIn("python3 scripts/release.py check --tag", release)
        self.assertIn('--tag "$GITHUB_REF_NAME" --history', release)
        self.assertIn('test "$GITHUB_SHA" = "$(git rev-parse origin/main)"', release)
        self.assertIn("git cat-file -t", release)
        self.assertIn("group: release\n", release)
        self.assertIn("fetch-depth: 2", ci)
        self.assertIn("gh release create", release)
        self.assertIn("--verify-tag", release)
        self.assertEqual(ci.count("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"), 1)
        self.assertEqual(release.count("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"), 1)


if __name__ == "__main__":
    unittest.main()
