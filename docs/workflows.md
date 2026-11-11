# Reusable workflows and the shared action

`.github/workflows/` ships **`on: workflow_call`-only** workflows. They are published for
adopters and are not triggered by this repository: nothing here declares `push` or
`pull_request`, which is exactly why `CORE-CI-ON` is a tier exclusion on the `org` tier (see
[`standard.md`](standard.md)). A repository adopts a workflow by calling it from its own
workflow, where it declares its own triggers.

Every workflow follows the same shape: top-level `permissions:`, a `concurrency:` group with an
explicit `cancel-in-progress`, `timeout-minutes` on every job, and every `uses:` pinned to a
40-hex commit SHA with a `# vX.Y.Z` comment. `actionlint` (and `shellcheck` on `run:` scripts)
keeps them honest.

| Workflow | Job(s) | Purpose |
| --- | --- | --- |
| `python-ci.yml` | `Lint & Type Check`, `checks`, `test (<version>)` | Python lint, type check and a test matrix; `test-job-name` sets the base of the matrix job name. |
| `node-ci.yml` | `build`, `build-and-check` | Node build and test with a deterministic install; matrix legs for Node and CPython. |
| `julia-ci.yml` | `tests and notebooks (julia <v>)` | Julia instantiate, `Pkg.test()` and optional notebook execution. |
| `html-ci.yml` | `validate`, `links` | Command-driven static-HTML/content validation, formatting and link checks. |
| `gnn-conformance.yml` | `gnn conformance (pinned)` | Installs a **pinned** Generalized Notation Notation revision and runs a conformance command. |
| `readiness.yml` | `readiness` | Runs this repository's checker against the caller with `--check`. |
| `actionlint.yml` | `actionlint` | Lints the caller's `.github/workflows/` with actionlint. |

## Required-context reproduction

Several jobs exist because an organization ruleset requires a status-check context whose
**exact string** must be reproduced. The callee job's `name:` is that string, and the caller job
must use the same `name:`. Whether GitHub reports the caller `name:` or a composite
`<caller> / <callee>` string is tracked as an experiment
(`prove-reusable-workflow-naming-and-canary`); until it reports, adopting these on a gated
repository is unverified. Each workflow's header comment lists the contexts it reproduces and
the ones it deliberately does **not**. Never rename such a job: a rename permanently blocks
pull requests in the adopter repository.

## The composite action

`.github/actions/setup-python-uv/action.yml` installs `uv` and a pinned Python, so a workflow
gets a reproducible interpreter without repeating the install dance. Call it with
`uses: ActiveInferenceInstitute/.github/.github/actions/setup-python-uv@<40-hex>`.

## Adopting a workflow

```yaml
# .github/workflows/ci.yml in the adopter repository
on:
  pull_request:
  push:
    branches: [main]

jobs:
  python:
    name: test (3.12)          # reproduce a required context if one exists
    uses: ActiveInferenceInstitute/.github/.github/workflows/python-ci.yml@<40-hex>
    with:
      python-versions: '["3.12"]'
    permissions:
      contents: read
```

Pin the `uses:` to a 40-hex commit SHA, not a branch or tag: a floating ref silently changes the
gate, and `ORG-WORKFLOWS` / `actionlint` reject a pin whose version comment disagrees.
