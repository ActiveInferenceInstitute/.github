# Replaying a readiness assessment

The checker accepts either an identified repository and checkout or a caller-provided fleet scope table. Choose a checkout revision and waiver assessment date before comparing reports. JSON reports use repository-relative paths and sorted keys and omit timestamps, run identifiers, and absolute checkout paths. Tests exercise each evaluation kind, report determinism, waiver expiry, and the distinction between unavailable and failed criteria [@aii-readiness-tests].

```sh
uv sync --frozen
uv run pytest -q
uv run python scripts/check_readiness.py \
  --repo ActiveInferenceInstitute/.github --path . --json
```

This yields a report. For a blocking verdict use `--check`, and use `--github` only when authenticated API measurements are in scope. The offline suite supplies an injected API client for the API evaluation kind. Its success establishes the behavior under the exercised inputs.
