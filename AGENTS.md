# AGENTS.md — ActiveInferenceInstitute/.github

This repository publishes the organization's agent-readiness standard: the criteria every
ActiveInferenceInstitute repository is measured against, the deterministic checker that evaluates
them, its tests, and the reusable workflows adopters call. It is itself one of the configured
repositories in scope and is measured against its own `org` tier, so the files here must satisfy the
same standard they define.

Two properties hold everywhere in this repository:

- **Criteria are data.** Adding or changing a criterion edits `standards/tiers.yaml`; it never
  requires editing Python. The five evaluation kinds are fixed.
- **`unavailable` is never `pass`.** A criterion that could not be measured says so, and `--check`
  then refuses to certify the repository.

## Layout

| Path | Purpose |
|---|---|
| `standards/tiers.yaml` | the tier and criterion catalogue as data |
| `standards/README.md` | the human contract: kinds, statuses, exit codes, tiers, exclusions, waivers |
| `scripts/check_readiness.py` | the checker: CLI, evaluation and deterministic JSON |
| `scripts/build_public_ledger.py` | builds `standards/public-ledger.json` from a fleet report and the scope table |
| `scripts/check_ledger_boundary.py` | the boundary command: proves the published ledger names no private repository |
| `standards/public-ledger.json` | the published ledger: 45 public repositories in full, private repositories as counts only |
| `tests/test_check_readiness.py` | checker tests (determinism, exit codes, `unavailable != pass`) |
| `tests/test_public_ledger.py` | ledger and boundary tests (`OWNER/REPO` ids, unquoted names, exit 2 on bad input) |
| `.github/workflows/` | `on: workflow_call`-only reusable workflows, unreferenced until a repository adopts one |
| `.github/actions/setup-python-uv/` | the composite action that installs uv and a pinned Python |
| `.github/dependabot.yml` | watches `uv.lock` (uv) and the pinned workflow actions |
| `.aii/readiness.json` | this repository's declared tier, baseline commit and waivers |
| `docs/` | the modular documentation: standard, checker, ledger, workflows, testing |
| `TODO.md` | known gaps, unverified behaviour and owner decisions |

## Toolchain

Python 3.15 driven by `uv`; `pytest`, `ruff`, `mypy` and `pyyaml` are declared in `pyproject.toml`
and pinned by `uv.lock`. Workflows are linted with `actionlint` 1.7.12 and `shellcheck` 0.11.0.
Run every Python command through `uv run` so it uses the locked versions; do not install tools
globally.

## Install

```bash
uv sync --frozen
```

## Lint

```bash
uv run ruff check .
uv run ruff format --check .
actionlint
```

`actionlint` reads `.github/workflows/`; it exits non-zero when a workflow is malformed or a pinned
`uses:` does not match its version comment.

## Typecheck

```bash
uv run mypy scripts
```

## Test

```bash
uv run pytest -q
```

The suite covers one criterion of each `kind`, byte-identical JSON across two runs, exit codes
`0`/`1`/`2`/`3`, waiver handling, unknown-kind rejection and `unavailable != pass`.

## Readiness

This repository's own blocking gate. `--github` is required here: the three `CORE-INHERIT-*`
criteria are `github-api`, and an offline `--check` exits `3` by design rather than claiming a pass.

```bash
export GH_TOKEN="$(gh auth token --user ActInfInstitute)"
uv run python scripts/check_readiness.py \
  --repo ActiveInferenceInstitute/.github --path . --check --github
```

A report (never a gate) for any repository, using its working-copy path:

```bash
uv run python scripts/check_readiness.py --repo OWNER/REPO --path PATH_TO_REPO --json
```

The fleet report needs the configured scope table supplied explicitly; the checker never enumerates
the GitHub organization:

```bash
uv run python scripts/check_readiness.py --all --scope /path/to/scope.tsv --json
```

Determinism is part of the contract: two `--json` runs on one commit must be byte-identical.

```bash
uv run python scripts/check_readiness.py --all --scope /path/to/scope.tsv --json > /tmp/ledger-a.json
uv run python scripts/check_readiness.py --all --scope /path/to/scope.tsv --json > /tmp/ledger-b.json
diff /tmp/ledger-a.json /tmp/ledger-b.json
```

## Conventions

- New criteria enter with `blocking: false` and are promoted only after their adoption tranche is
  green. Never add a blocking gate to a red repository.
- A tier exclusion in `standards/tiers.yaml` (`excludes:`) is not a waiver. `CORE-CI-ON` is excluded
  on the `org` tier only, because this repository deliberately ships `workflow_call`-only workflows
  and never declares `push`/`pull_request` triggers itself.
- Waivers live in each repository's `.aii/readiness.json`, require a non-empty reason, and only turn
  `fail` into `waived`; they never turn an untested criterion green.
- Workflows: `on: workflow_call` only, top-level `permissions:`, `concurrency:` with
  `cancel-in-progress`, `timeout-minutes` on every job, and every `uses:` pinned to a 40-hex commit
  SHA with a `# vX.Y.Z` comment.
- Never rename a job that can reproduce an existing required status-check context; that rename
  permanently blocks pull requests in the adopter repository.
- Preserve the community-health defaults (`CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`,
  `PULL_REQUEST_TEMPLATE.md`, `SECURITY.md`, `SUPPORT.md`, `.github/ISSUE_TEMPLATE/`,
  `profile/README.md`): every repository in the organization inherits them.

## Licence

MIT, the licence the sibling code repositories use. `LICENSE` covers this repository's own files;
each repository this standard measures keeps its own licence.
