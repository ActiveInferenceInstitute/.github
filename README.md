# ActiveInferenceInstitute/.github

Organization-wide defaults for the [Active Inference Institute](https://github.com/ActiveInferenceInstitute) on GitHub.

- [Documentation](docs/README.md): the readiness standard, the checker, the ledger and the reusable workflows
- [Agent readiness standard](#agent-readiness-standard): what every Institute repository is measured against
- [Community-health defaults](#community-health-defaults): files every repository inherits
- [Development](#development): install, lint, typecheck and test
- [`TODO.md`](TODO.md): known gaps and owner decisions

## Documentation

The modular documentation lives in [`docs/`](docs/README.md):

| Document | Covers |
| --- | --- |
| [`docs/standard.md`](docs/standard.md) | the criterion catalogue, the five kinds, tiers and adoption |
| [`docs/checker.md`](docs/checker.md) | `check_readiness.py`: CLI, report schema, exit codes, determinism |
| [`docs/ledger.md`](docs/ledger.md) | the public-only ledger, its builder and the boundary command |
| [`docs/workflows.md`](docs/workflows.md) | the reusable `workflow_call` workflows and the composite action |
| [`docs/testing.md`](docs/testing.md) | the offline suite, lint and typecheck |

The criterion contract itself stays beside its data: [`standards/README.md`](standards/README.md).

## Community-health defaults

- [`profile/README.md`](profile/README.md): the public organization profile shown at github.com/ActiveInferenceInstitute
- [`CONTRIBUTING.md`](CONTRIBUTING.md), [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md), [`SECURITY.md`](SECURITY.md), [`SUPPORT.md`](SUPPORT.md): default community health files
- [`PULL_REQUEST_TEMPLATE.md`](PULL_REQUEST_TEMPLATE.md) and [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE): default pull request and issue templates

These defaults apply to any repository in the organization that does not have its own version of a file. To override one for a single repository, add a file with the same name to that repository.

## Agent readiness standard

This repository publishes the organization-wide readiness standard every
ActiveInferenceInstitute repository is measured against: the criterion catalogue,
a deterministic checker, and the published ledger. Criteria are data
([`standards/tiers.yaml`](standards/tiers.yaml)); the human contract — the five
kinds, the five statuses, exit codes, tiers and the ratchet — is in
[`standards/README.md`](standards/README.md), and the public-only ledger is
[`standards/public-ledger.json`](standards/public-ledger.json).

```bash
# Report for one repository (deterministic JSON).
uv run python scripts/check_readiness.py --repo OWNER/REPO --path PATH_TO_REPO --json

# Blocking gate for one repository.
uv run python scripts/check_readiness.py --repo OWNER/REPO --path PATH_TO_REPO --check

# Fleet report from the configured scope (never enumerate the organization).
uv run python scripts/check_readiness.py --all --scope /path/to/scope.tsv --json

# Publish the public-only ledger, then prove its boundary.
uv run python scripts/build_public_ledger.py \
  --report /tmp/fleet.json --scope /path/to/scope.tsv --out standards/public-ledger.json
uv run python scripts/check_ledger_boundary.py \
  --ledger standards/public-ledger.json --scope /path/to/scope.tsv
```

## Development

Python 3.15, driven by `uv`; the toolchain is pinned by `uv.lock`. See
[docs/testing.md](docs/testing.md) for the full gate list.

```bash
uv sync --frozen                 # install the locked toolchain
uv run pytest -q                 # offline suite
uv run ruff check .              # lint
uv run mypy scripts              # typecheck
actionlint                       # lint the reusable workflows
```

The suite is fully offline, including the `github-api` kind (exercised through an injected
client). The fleet report and the ledger need the owner's scope table and are not runnable from
a bare checkout; the readiness gate needs `--github` with an authenticated token because the
`CORE-INHERIT-*` criteria are API reads.

## InstituteOS sidecar

This repository carries a committed [`.aii/`](.aii/README.md) sidecar with curated
InstituteOS metadata (`aii-sidecar/v1`): category, maturity, ecosystem relations,
key artifacts, provenance, and portable tasks. It is read by
[InstituteOS](https://github.com/ActiveInferenceInstitute/InstituteOS) tooling and
does not affect the project's build or runtime.
