# Contributing

Create a focused branch, keep changes scoped, and run:

```bash
env -u CODEX_SANDBOX python3 tests/run_tests.py
python3 scripts/ledger_distribution.py --check LEDGER.md
git diff --check
```

Do not commit personal task records, credentials, absolute home-directory paths, local configuration dumps,
or runner output. Use synthetic fixtures for tests and redact reproduction logs before opening an issue.
