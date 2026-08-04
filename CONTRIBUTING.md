# Contributing

Create a focused branch, keep changes scoped, and run:

```bash
env -u CODEX_SANDBOX python3 tests/run_tests.py
python3 scripts/ledger_distribution.py --check templates/LEDGER.md
git diff --check
```

## Release preparation

`VERSION` is the version source of truth. Keep its stable SemVer value and the matching dated section in
`CHANGELOG.md` in the same focused PR. Follow [`RELEASING.md`](./RELEASING.md) for the pre-1.0 policy,
exact validation and signed-tag commands, publication checks, and recovery rules.

```bash
python3 scripts/release.py current
python3 scripts/release.py next patch
python3 scripts/release.py check
python3 tests/test_release.py
```

After that PR is merged, use the signed-tag procedure in `RELEASING.md`. Tag creation and push remain a
separate public release approval boundary.

Do not commit personal task records, credentials, absolute home-directory paths, local configuration dumps,
or runner output. Use synthetic fixtures for tests and redact reproduction logs before opening an issue.
