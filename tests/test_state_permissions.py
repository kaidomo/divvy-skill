#!/usr/bin/env python3
"""Security contract tests for divvy's host-local state.

Every filesystem fixture lives below a TemporaryDirectory.  In particular, this
module never resolves, reads, or changes the caller's real ROSTER/LEDGER paths.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "init_state.py"
SPEC = importlib.util.spec_from_file_location("divvy_init_state_permissions", SCRIPT)
assert SPEC and SPEC.loader
STATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATE)

FIELDS = (
    "schema", "command", "target", "path", "path_label", "status",
    "mode_before", "mode_after", "content_unchanged", "reason_code",
    "detail", "resume_stage",
)
STATUSES = {"compliant", "noncompliant", "created", "preserved", "migrated", "no-op", "refused", "PARTIAL"}
REASONS = {
    "ok", "mode_mismatch", "unsafe_type", "owner_mismatch",
    "symlink_refused", "hardlink_refused", "duplicate_target",
    "unsupported_safe_primitive", "partial_rollback", "content_changed",
}


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir(mode=0o700)
        self.ledger = root / "ledger-parent" / "LEDGER.md"
        self.roster = root / "roster-parent" / "ROSTER.md"
        self.ledger.parent.mkdir(mode=0o750)
        self.roster.parent.mkdir(mode=0o750)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "DIVVY_LEDGER": str(self.ledger),
            "DIVVY_ROSTER": str(self.roster),
        }
        env.update(extra)
        return env


def invoke(command: str, env: dict[str, str], *patches: mock._patch) -> tuple[int, str, str, BaseException | None]:
    stdout, stderr = io.StringIO(), io.StringIO()
    error = None
    rc = -1
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        stack.enter_context(mock.patch.object(sys, "argv", [str(SCRIPT), command]))
        stack.enter_context(contextlib.redirect_stdout(stdout))
        stack.enter_context(contextlib.redirect_stderr(stderr))
        for item in patches:
            stack.enter_context(item)
        try:
            rc = STATE.main()
        except BaseException as exc:  # deliberate crash injection is evidence, not a test error
            error = exc
    return rc, stdout.getvalue(), stderr.getvalue(), error


def run_cli(command: str, env: dict[str, str], umask: int | None = None) -> subprocess.CompletedProcess[str]:
    code = (
        "import os,runpy,sys;"
        + (f"os.umask({umask});" if umask is not None else "")
        + f"sys.argv=[{str(SCRIPT)!r},{command!r}];runpy.run_path({str(SCRIPT)!r},run_name='__main__')"
    )
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False
    )


class CreationAndOverrideTests(unittest.TestCase):
    def test_new_files_and_owned_leaves_have_exact_modes_for_all_supported_umasks(self) -> None:
        for mask in (0o000, 0o022, 0o077):
            with self.subTest(umask=oct(mask)), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                home = root / "home"
                home.mkdir(mode=0o700)
                state_base, config_base = root / "state", root / "config"
                state_base.mkdir(mode=0o750)
                config_base.mkdir(mode=0o750)
                env = {
                    "HOME": str(home),
                    "XDG_STATE_HOME": str(state_base),
                    "XDG_CONFIG_HOME": str(config_base),
                }
                result = run_cli("init", env, mask)
                ledger = root / "state" / "divvy" / "LEDGER.md"
                roster = root / "config" / "divvy" / "ROSTER.md"
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((mode(ledger.parent), mode(roster.parent)), (0o700, 0o700))
                self.assertEqual((mode(ledger), mode(roster)), (0o600, 0o600))

    def test_missing_default_home_ancestors_are_created_componentwise_at_0700(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            home.mkdir(mode=0o700)
            result = run_cli("init", {"HOME": str(home)})
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = (home / ".local", home / ".local" / "state", home / ".local" / "state" / "divvy", home / ".config", home / ".config" / "divvy")
            self.assertTrue(all(path.is_dir() for path in expected))
            self.assertTrue(all(mode(path) == 0o700 for path in expected))

    def test_init_preserves_compliant_existing_file_bytes_inode_hash_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            fx.ledger.write_bytes(b"personal-ledger\n")
            fx.roster.write_bytes(b"personal-roster\n")
            os.chmod(fx.ledger, 0o600)
            os.chmod(fx.roster, 0o600)
            before = (fx.ledger.stat().st_ino, digest(fx.ledger), mode(fx.ledger))
            result = run_cli("init", fx.env())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((fx.ledger.stat().st_ino, digest(fx.ledger), mode(fx.ledger)), before)

    def test_init_refuses_insecure_existing_mode_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            fx.ledger.write_bytes(b"private\n")
            fx.roster.write_bytes(b"private\n")
            os.chmod(fx.ledger, 0o644)
            os.chmod(fx.roster, 0o600)
            before = (digest(fx.ledger), mode(fx.ledger))
            result = run_cli("init", fx.env())
            self.assertEqual(result.returncode, 2)
            self.assertEqual((digest(fx.ledger), mode(fx.ledger)), before)
            self.assertIn("migrate-permissions", result.stderr)

    def test_direct_file_override_never_chmods_arbitrary_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            before = (mode(fx.ledger.parent), mode(fx.roster.parent))
            result = run_cli("init", fx.env())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((mode(fx.ledger.parent), mode(fx.roster.parent)), before)

    def test_directory_override_owns_only_selected_divvy_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_parent, config_parent = root / "state-parent", root / "config-parent"
            state_parent.mkdir(mode=0o750)
            config_parent.mkdir(mode=0o750)
            state_leaf, config_leaf = state_parent / "divvy", config_parent / "divvy"
            env = {"HOME": str(root), "DIVVY_STATE_DIR": str(state_leaf), "DIVVY_CONFIG_DIR": str(config_leaf)}
            result = run_cli("init", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((mode(state_parent), mode(config_parent)), (0o750, 0o750))
            self.assertEqual((mode(state_leaf), mode(config_leaf)), (0o700, 0o700))

    def test_missing_xdg_parent_is_refused_before_target_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            missing_state, missing_config = root / "missing-state", root / "missing-config"
            result = run_cli("init", {"HOME": str(root), "XDG_STATE_HOME": str(missing_state), "XDG_CONFIG_HOME": str(missing_config)})
            self.assertEqual(result.returncode, 2)
            self.assertFalse(missing_state.exists())
            self.assertFalse(missing_config.exists())

    def test_symlinked_parent_component_is_refused_before_target_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)
            env = {"HOME": str(root), "DIVVY_LEDGER": str(linked / "LEDGER.md"), "DIVVY_ROSTER": str(root / "ROSTER.md")}
            result = run_cli("init", env)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((real / "LEDGER.md").exists())


class ValidationAndMigrationTests(unittest.TestCase):
    def test_check_permissions_is_read_only_and_reports_mode_mismatch_rc3(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            for path in (fx.ledger, fx.roster):
                path.write_bytes(b"private\n")
                os.chmod(path, 0o644)
            before = [(digest(path), mode(path), path.stat().st_ino) for path in (fx.ledger, fx.roster)]
            result = run_cli("check-permissions", fx.env())
            self.assertEqual(result.returncode, 3)
            self.assertIn("reason_code=mode_mismatch", result.stdout)
            self.assertEqual([(digest(path), mode(path), path.stat().st_ino) for path in (fx.ledger, fx.roster)], before)

    def test_migrate_permissions_changes_only_modes_and_second_run_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            for path, data in ((fx.ledger, b"ledger\n"), (fx.roster, b"roster\n")):
                path.write_bytes(data)
                os.chmod(path, 0o644)
            hashes = tuple(digest(path) for path in (fx.ledger, fx.roster))
            first = run_cli("migrate-permissions", fx.env())
            second = run_cli("migrate-permissions", fx.env())
            self.assertEqual((first.returncode, second.returncode), (0, 0))
            self.assertEqual(tuple(digest(path) for path in (fx.ledger, fx.roster)), hashes)
            self.assertEqual((mode(fx.ledger), mode(fx.roster)), (0o600, 0o600))
            self.assertIn("status=no-op", second.stdout)

    def test_symlink_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            victim = Path(raw) / "victim"
            victim.write_bytes(b"victim")
            fx.ledger.symlink_to(victim)
            result = run_cli("check-permissions", fx.env())
            self.assertEqual(result.returncode, 2)
            self.assertIn("reason_code=symlink_refused", result.stdout + result.stderr)

    def test_nonregular_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            os.mkfifo(fx.ledger, 0o600)
            result = run_cli("check-permissions", fx.env())
            self.assertEqual(result.returncode, 2)
            self.assertIn("reason_code=unsafe_type", result.stdout + result.stderr)

    def test_hardlinked_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            fx.ledger.write_bytes(b"private")
            alias = Path(raw) / "alias"
            os.link(fx.ledger, alias)
            result = run_cli("check-permissions", fx.env())
            self.assertEqual(result.returncode, 2)
            self.assertIn("reason_code=hardlink_refused", result.stdout + result.stderr)

    def test_wrong_owner_validator_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            fx.ledger.write_bytes(b"private")
            fx.roster.write_bytes(b"private")
            rc, out, err, error = invoke("check-permissions", fx.env(), mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1))
            self.assertIsNone(error)
            self.assertEqual(rc, 2)
            self.assertIn("reason_code=owner_mismatch", out + err)

    def test_duplicate_exact_targets_are_refused_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            fx.ledger.write_bytes(b"shared")
            os.chmod(fx.ledger, 0o644)
            before = mode(fx.ledger)
            env = fx.env(DIVVY_ROSTER=str(fx.ledger))
            result = run_cli("migrate-permissions", env)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(mode(fx.ledger), before)
            self.assertIn("reason_code=duplicate_target", result.stdout + result.stderr)

    def test_same_inode_aliased_targets_are_refused_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            fx.ledger.write_bytes(b"shared")
            os.link(fx.ledger, fx.roster)
            os.chmod(fx.ledger, 0o644)
            result = run_cli("migrate-permissions", fx.env())
            self.assertEqual(result.returncode, 2)
            self.assertEqual(mode(fx.ledger), 0o644)
            self.assertIn("reason_code=duplicate_target", result.stdout + result.stderr)

    def test_unsupported_no_follow_capability_refuses_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            rc, out, err, error = invoke("init", fx.env(), mock.patch.object(os, "O_NOFOLLOW", 0, create=True))
            self.assertIsNone(error)
            self.assertEqual(rc, 2)
            self.assertFalse(fx.ledger.exists())
            self.assertIn("reason_code=unsupported_safe_primitive", out + err)

    def test_failure_on_second_migration_target_rolls_back_first(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            for path in (fx.ledger, fx.roster):
                path.write_bytes(b"private")
                os.chmod(path, 0o644)
            real_fchmod = os.fchmod
            calls = 0

            def fail_second(fd: int, new_mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second-target failure")
                real_fchmod(fd, new_mode)

            rc, out, err, error = invoke("migrate-permissions", fx.env(), mock.patch.object(os, "fchmod", side_effect=fail_second))
            self.assertIsNone(error)
            self.assertIn(rc, (2, 4))
            self.assertEqual((mode(fx.ledger), mode(fx.roster)), (0o644, 0o644))
            self.assertIn("resume_stage=", out + err)

    def test_rollback_failure_reports_partial_with_exact_resume_stage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            for path in (fx.ledger, fx.roster):
                path.write_bytes(b"private")
                os.chmod(path, 0o644)
            real_fchmod = os.fchmod
            calls = 0

            def fail_second_and_rollback(fd: int, new_mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls in (2, 3):
                    raise OSError(f"injected fchmod failure {calls}")
                real_fchmod(fd, new_mode)

            rc, out, err, error = invoke("migrate-permissions", fx.env(), mock.patch.object(os, "fchmod", side_effect=fail_second_and_rollback))
            self.assertIsNone(error)
            self.assertEqual(rc, 4)
            self.assertIn("status=PARTIAL", out + err)
            self.assertIn("reason_code=partial_rollback", out + err)
            self.assertRegex(out + err, r"resume_stage=[^\s]+")

    def test_failure_after_each_directory_or_file_target_rolls_back_prior_modes(self) -> None:
        for fail_at in (1, 2, 3, 4):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                state_leaf, config_leaf = root / "state" / "divvy", root / "config" / "divvy"
                state_leaf.mkdir(parents=True, mode=0o755)
                config_leaf.mkdir(parents=True, mode=0o755)
                ledger, roster = state_leaf / "LEDGER.md", config_leaf / "ROSTER.md"
                ledger.write_bytes(b"ledger\n")
                roster.write_bytes(b"roster\n")
                os.chmod(ledger, 0o644)
                os.chmod(roster, 0o644)
                env = {
                    "HOME": str(root),
                    "DIVVY_STATE_DIR": str(state_leaf),
                    "DIVVY_CONFIG_DIR": str(config_leaf),
                }
                real_fchmod = os.fchmod
                calls = 0

                def fail_once(fd: int, new_mode: int) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError("injected deterministic target failure")
                    real_fchmod(fd, new_mode)

                rc, _out, _err, error = invoke(
                    "migrate-permissions", env,
                    mock.patch.object(os, "fchmod", side_effect=fail_once),
                )
                self.assertIsNone(error)
                self.assertEqual(rc, 2)
                self.assertEqual(
                    (mode(state_leaf), mode(config_leaf), mode(ledger), mode(roster)),
                    (0o755, 0o755, 0o644, 0o644),
                )

    def test_concurrent_content_change_is_partial_and_modes_are_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            for path in (fx.ledger, fx.roster):
                path.write_bytes(b"private")
                os.chmod(path, 0o644)
            real_fchmod = os.fchmod
            injected = False

            def mutate_during_migration(fd: int, new_mode: int) -> None:
                nonlocal injected
                real_fchmod(fd, new_mode)
                if not injected:
                    injected = True
                    fx.ledger.write_bytes(b"changed concurrently")

            rc, out, err, error = invoke(
                "migrate-permissions", fx.env(),
                mock.patch.object(os, "fchmod", side_effect=mutate_during_migration),
            )
            self.assertIsNone(error)
            self.assertEqual(rc, 4)
            self.assertEqual((mode(fx.ledger), mode(fx.roster)), (0o644, 0o644))
            self.assertIn("status=PARTIAL", out + err)
            self.assertIn("reason_code=content_changed", out + err)
            self.assertIn("content_unchanged=false", out + err)


class ReceiptContractTests(unittest.TestCase):
    def test_receipt_schema_has_ordered_fields_status_vocabulary_and_rc_taxonomy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            result = run_cli("check-permissions", fx.env())
            text = result.stdout + result.stderr
            positions = [text.find(field + "=") for field in FIELDS]
            self.assertTrue(all(position >= 0 for position in positions), text)
            self.assertEqual(positions, sorted(positions))
            self.assertIn("schema=divvy-state-permissions/v1", text)
            self.assertTrue(any(f"status={status}" in text for status in STATUSES))
            self.assertIn(result.returncode, {0, 2, 3, 4})

    def test_public_receipt_redacts_sensitive_fields_for_every_reason_code(self) -> None:
        render = getattr(STATE, "render_permission_receipt", None)
        self.assertTrue(callable(render), "render_permission_receipt(record, public=True) is required")
        for reason in REASONS:
            with self.subTest(reason=reason):
                record = {
                    "schema": "divvy-state-permissions/v1", "command": "check-permissions",
                    "target": "ledger", "path": "/synthetic/home/private/LEDGER.md",
                    "path_label": "ledger", "status": "refused", "mode_before": "0644",
                    "mode_after": "0644", "content_unchanged": "true", "reason_code": reason,
                    "detail": "synthetic-host uid=424242 sha256=deadbeef", "resume_stage": "validation",
                }
                rendered = render(record, public=True)
                public = rendered if isinstance(rendered, str) else "\n".join(f"{k}={v}" for k, v in rendered.items())
                for secret in (
                    record["path"], record["detail"], record["mode_before"],
                    record["mode_after"], record["resume_stage"], "synthetic-host", "424242",
                    "deadbeef", "HOME", "UID", "sha256",
                ):
                    self.assertNotIn(secret, public)
                self.assertNotIn("target=", public)
                self.assertNotIn("mode_before=", public)
                self.assertNotIn("mode_after=", public)
                self.assertNotIn("resume_stage=", public)
                self.assertIn(f"reason_code={reason}", public)
                self.assertIn("path_label=ledger", public)
                self.assertNotIn("/", reason)
                self.assertNotIn("\\", reason)


class ConcurrencyRaceAndCrashTests(unittest.TestCase):
    def test_concurrent_initializers_create_once_without_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            barrier = threading.Barrier(3)
            results: list[subprocess.CompletedProcess[str]] = []

            def worker() -> None:
                barrier.wait()
                results.append(run_cli("init", fx.env()))

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertIn(sorted(result.returncode for result in results), ([0, 0], [0, 2]))
            output = "".join(result.stdout for result in results)
            self.assertEqual(output.count("ledger_status=created"), 1)
            self.assertEqual(
                output.count("ledger_status=preserved")
                + sum("reason_code=hardlink_refused" in result.stderr for result in results),
                1,
            )
            self.assertEqual(fx.ledger.read_bytes(), (ROOT / "templates" / "LEDGER.md").read_bytes())

    def test_replacement_at_no_clobber_publication_boundary_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            real_link = os.link
            injected = False

            def replace_before_link(src, dst, *args, **kwargs):
                nonlocal injected
                if not injected and Path(dst).name == "LEDGER.md":
                    injected = True
                    fx.ledger.write_bytes(b"attacker")
                return real_link(src, dst, *args, **kwargs)

            rc, out, err, error = invoke("init", fx.env(), mock.patch.object(os, "link", side_effect=replace_before_link))
            self.assertIsNone(error)
            self.assertEqual(rc, 2)
            self.assertEqual(fx.ledger.read_bytes(), b"attacker")
            self.assertIn("reason_code=", out + err)

    def test_replacement_between_migration_validation_and_mutation_is_descriptor_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            for path in (fx.ledger, fx.roster):
                path.write_bytes(b"private")
                os.chmod(path, 0o644)
            original_inode = fx.ledger.stat().st_ino
            attacker = fx.ledger.parent / "attacker"
            attacker.write_bytes(b"attacker")
            os.chmod(attacker, 0o644)
            real_fchmod = os.fchmod
            injected = False

            def replace_before_mutation(fd: int, new_mode: int) -> None:
                nonlocal injected
                if not injected:
                    injected = True
                    fx.ledger.rename(fx.ledger.with_suffix(".validated"))
                    attacker.rename(fx.ledger)
                real_fchmod(fd, new_mode)

            rc, _out, _err, error = invoke("migrate-permissions", fx.env(), mock.patch.object(os, "fchmod", side_effect=replace_before_mutation))
            self.assertIsNone(error)
            self.assertIn(rc, (0, 2))
            self.assertEqual(fx.ledger.read_bytes(), b"attacker")
            self.assertEqual(mode(fx.ledger), 0o644)
            validated = fx.ledger.with_suffix(".validated")
            self.assertEqual(validated.stat().st_ino, original_inode)
            self.assertEqual(mode(validated), 0o600 if rc == 0 else 0o644)

    def test_crash_after_temporary_creation_leaves_final_absent_and_residue_unowned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            real_open = os.open

            def crash_after_create(path, flags, mode_arg=0o777, *args, **kwargs):
                fd = real_open(path, flags, mode_arg, *args, **kwargs)
                if flags & os.O_EXCL and Path(path).name.startswith(".LEDGER.md."):
                    raise RuntimeError("crash-after-temp-create")
                return fd

            _rc, _out, _err, error = invoke("init", fx.env(), mock.patch.object(os, "open", side_effect=crash_after_create))
            self.assertIsInstance(error, RuntimeError)
            self.assertFalse(fx.ledger.exists())
            residues = list(fx.ledger.parent.glob(".LEDGER.md.*"))
            self.assertEqual(len(residues), 1)
            self.assertEqual(residues[0].stat().st_nlink, 1)

    def test_prepublication_residue_is_not_auto_cleaned_without_owner_token_liveness_proof(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            residue = fx.ledger.parent / ".LEDGER.md.synthetic-token"
            residue.write_bytes(b"possibly-live-creator")
            result = run_cli("init", fx.env())
            text = result.stdout + result.stderr
            self.assertEqual(result.returncode, 2)
            self.assertTrue(residue.exists())
            self.assertIn("residue_requires_explicit_cleanup", text)
            for prerequisite in ("owner", "token", "liveness"):
                self.assertIn(prerequisite, text)

    def test_crash_after_fsync_before_link_never_exposes_partial_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            real_fsync = os.fsync
            injected = False

            def crash_after_fsync(fd: int) -> None:
                nonlocal injected
                real_fsync(fd)
                if not injected and stat.S_ISREG(os.fstat(fd).st_mode):
                    injected = True
                    raise RuntimeError("crash-after-write-fsync")

            _rc, _out, _err, error = invoke("init", fx.env(), mock.patch.object(os, "fsync", side_effect=crash_after_fsync))
            self.assertIsInstance(error, RuntimeError)
            self.assertFalse(fx.ledger.exists())
            residues = list(fx.ledger.parent.glob(".LEDGER.md.*"))
            self.assertEqual(len(residues), 1)
            self.assertEqual(residues[0].read_bytes(), (ROOT / "templates" / "LEDGER.md").read_bytes())

    def test_crash_after_link_before_unlink_leaves_complete_same_inode_residue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            real_unlink = os.unlink

            def crash_before_temp_unlink(path, *args, **kwargs):
                if Path(path).name.startswith(".LEDGER.md.") and fx.ledger.exists():
                    raise RuntimeError("crash-after-link")
                return real_unlink(path, *args, **kwargs)

            _rc, _out, _err, error = invoke("init", fx.env(), mock.patch.object(os, "unlink", side_effect=crash_before_temp_unlink))
            self.assertIsInstance(error, RuntimeError)
            self.assertEqual(fx.ledger.read_bytes(), (ROOT / "templates" / "LEDGER.md").read_bytes())
            residues = list(fx.ledger.parent.glob(".LEDGER.md.*"))
            self.assertEqual(len(residues), 1)
            self.assertEqual(residues[0].stat().st_ino, fx.ledger.stat().st_ino)
            self.assertEqual(fx.ledger.stat().st_nlink, 2)

            retry = run_cli("init", fx.env())
            self.assertEqual(retry.returncode, 2)
            self.assertTrue(residues[0].exists())
            self.assertEqual(fx.ledger.stat().st_nlink, 2)
            self.assertIn("residue_requires_explicit_cleanup", retry.stderr)

    def test_crafted_same_inode_residue_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fx = Fixture(Path(raw))
            fx.ledger.write_bytes((ROOT / "templates" / "LEDGER.md").read_bytes())
            fx.roster.write_bytes((ROOT / "templates" / "ROSTER.md").read_bytes())
            os.chmod(fx.ledger, 0o600)
            os.chmod(fx.roster, 0o600)
            alias = fx.ledger.parent / ".LEDGER.md.999999-crafted"
            os.link(fx.ledger, alias)

            result = run_cli("init", fx.env())
            self.assertEqual(result.returncode, 2)
            self.assertTrue(alias.exists())
            self.assertEqual(fx.ledger.stat().st_nlink, 2)
            self.assertIn("residue_requires_explicit_cleanup", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
