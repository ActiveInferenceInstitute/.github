# Testing, lint and typecheck

The suite is small, offline and deterministic. It is the strongest evidence that a change here
does not break the standard, and it is what the `org` tier's criteria check for.

## Test

```bash
uv sync --frozen
uv run pytest -q
```

The focused offline suite covers:

| File | Covers |
| --- | --- |
| `tests/test_check_readiness.py` | evaluation kinds, determinism, statuses and waivers; unavailable discovery/YAML, indexed licenses, decimal thresholds and catalogue regex errors |
| `tests/test_public_ledger.py` | shared scope parsing, duplicate/case admission, private-name refusal and invalid-count errors |
| `tests/test_inverse_policy.py` | violating peers in either order, all-clean and absent inputs, and nonblocking inverse criteria |

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

Historical runs qualify only their recorded source and runtime. Current
acceptance requires Python >=3.15; a Python 3.14 receipt does not establish that
floor. Use the locked environment in CI, or explicitly bind an existing
compatible interpreter for an offline source check:

```bash
python -B -m pytest -q -o addopts= -p no:cacheprovider \
  tests/test_check_readiness.py tests/test_public_ledger.py tests/test_inverse_policy.py
```

Cold-cache synchronization and locked-environment qualification remain separate
from a focused check; do not present a borrowed runtime as a frozen-lock install.
