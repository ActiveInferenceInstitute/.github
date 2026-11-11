# Documentation — ActiveInferenceInstitute/.github

This repository publishes the organization-wide **agent readiness standard** that every
ActiveInferenceInstitute repository is measured against, the deterministic checker that
evaluates it, and the reusable GitHub Actions workflows adopters call. This `docs/` tree is
the entry point for people who want to understand, run or extend that standard.

| Document | What it covers |
| --- | --- |
| [`standard.md`](standard.md) | What the standard is, the five evaluation kinds, tiers and exclusions, and how a repository adopts it. |
| [`checker.md`](checker.md) | The `check_readiness.py` checker: CLI, report schema, exit codes and determinism. |
| [`ledger.md`](ledger.md) | The public-only ledger: build, boundary rules and the machine-checked public/private split. |
| [`workflows.md`](workflows.md) | The reusable `workflow_call` workflows and the shared composite action. |
| [`testing.md`](testing.md) | The offline test suite, lint and typecheck gates, and how to run them. |

The human contract for the standard itself lives next to the data it describes:
[`../standards/README.md`](../standards/README.md). The criterion catalogue is
[`../standards/tiers.yaml`](../standards/tiers.yaml), and the published ledger is
[`../standards/public-ledger.json`](../standards/public-ledger.json).

## Repository-level documents

- [`../README.md`](../README.md) — overview, the org profile pointer and the standard's commands.
- [`../AGENTS.md`](../AGENTS.md) — layout, toolchain and conventions for agents working here.
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md), [`../SECURITY.md`](../SECURITY.md),
  [`../SUPPORT.md`](../SUPPORT.md), [`../CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md) — inherited
  community-health defaults for the whole organization.
- [`../profile/README.md`](../profile/README.md) — the public organization profile.
- [`../TODO.md`](../TODO.md) — known gaps and owner decisions.

## Working copy layout

```
standards/   criterion catalogue (tiers.yaml), the human contract and the public ledger
scripts/     the checker, the ledger builder and the boundary command
tests/       offline unit tests for the checker, the ledger builder and the boundary
.github/     reusable workflow_call workflows, issue/PR templates and the composite action
profile/     the public organization profile README
```

## Quick start

```bash
uv sync --frozen
uv run pytest -q                                   # 113 offline tests
uv run python scripts/check_readiness.py \
  --repo OWNER/REPO --path PATH_TO_REPO --json     # deterministic report
```

The blocking gate and the fleet report need extra inputs; see [`checker.md`](checker.md).
