# Contributing

Create a focused branch, keep changes scoped, and run:

```bash
env -u CODEX_SANDBOX python3 tests/run_tests.py
python3 scripts/ledger_distribution.py --check templates/LEDGER.md
git diff --check
```

## Merge-triggered releases

`VERSION` remains the stable SemVer source of truth. A reviewed PR merged to protected `main` is released by
default; only an allowlisted actor's merge-time `release:skip` suppresses that merge's immediate cut. Do not
manually edit or move public tags/Releases. Follow [`RELEASING.md`](./RELEASING.md) for immutable commit-note
evidence, 0.x floors, tagged-snapshot equality, signer trust, partial recovery, and exact authority gates.

```bash
python3 scripts/release.py current
python3 scripts/release.py next patch
python3 scripts/release.py check
python3 tests/test_release.py
```

Repository settings, signer provisioning, the first real merge, tag, and Release remain separately approved
public-effect boundaries. Local helpers and PR tests never grant that authority.

Do not commit personal task records, credentials, absolute home-directory paths, local configuration dumps,
or runner output. Use synthetic fixtures for tests and redact reproduction logs before opening an issue.
