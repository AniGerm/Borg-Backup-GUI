# Contributing

Thanks for helping improve Borg Backup GUI.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
./QA.sh          # must score >= 95
.venv/bin/pytest -q
```

## Guidelines

- Prefer changes in `borg_model.py` for pure logic (easier to test).
- Keep the Tk GUI as the shipping UI for v2 (no Qt/GTK rewrite unless agreed).
- Do not commit secrets, `config.json`, or personal paths in `.desktop` files.
- Run `ruff format` / `ruff check` and `mypy` before opening a PR.
- Add or extend tests under `tests/` for model behaviour you change.

## Pull requests

1. Fork and branch from `main`.
2. Keep PRs focused (one concern per PR when possible).
3. Describe user-visible behaviour and how you tested it.
