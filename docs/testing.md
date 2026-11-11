# Testing, lint and typecheck

The suite is small, offline and deterministic. It is the strongest evidence that a change here
does not break the standard, and it is what the `org` tier's criteria check for.

## Test

```bash
uv sync --frozen
uv run pytest -q
```

`tests/` holds two files:

| File | Covers |
| --- | --- |
| `tests/test_check_readiness.py` | one criterion of each `kind`, byte-identical JSON across two runs, exit codes `0`/`1`/`2`/`3`, waiver handling, unknown-kind rejection, `unavailable != pass` |
| `tests/test_public_ledger.py` | the ledger builder and the boundary command: `OWNER/REPO` ids, unquoted names, exit `2` on bad input |

No test needs the network. The `github-api` kind is exercised through an injected client, so the
suite never calls GitHub. If a change needs a live API to test, it is not testable here and the
kind or the fixture is wrong.

## Lint

```bash
uv run ruff check .
uv run ruff format --check .
actionlint
```

`actionlint` reads `.github/workflows/` and exits non-zero when a workflow is malformed or a
pinned `uses:` disagrees with its version comment. It must stay clean; `ORG-ACTIONLINT` measures
it.

## Typecheck

```bash
uv run mypy scripts
```

Runs through `uv run` so it uses the locked `mypy`. Do not install tools globally.

## Readiness (the repository's own gate)

This repository is measured against the `org` tier. `--github` is required for a `0`/`1`
answer: the `CORE-INHERIT-*` criteria are `github-api`, so an offline `--check` exits `3` by
design rather than claiming a pass.

```bash
export GH_TOKEN="$(gh auth token --user ActInfInstitute)"
uv run python scripts/check_readiness.py \
  --repo ActiveInferenceInstitute/.github --path . --check --github
```

The parent task for this pass used the repository's pinned virtual environment directly
(`.venv/bin/python -m pytest -q`). Either entry point is acceptable offline; use `uv run` in CI
so versions come from `uv.lock`. `uv sync --frozen` and `uv run --frozen` verify the lock rather
than updating it.
