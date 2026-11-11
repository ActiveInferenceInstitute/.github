# ActiveInferenceInstitute/.github

Organization-wide defaults for the [Active Inference Institute](https://github.com/ActiveInferenceInstitute) on GitHub.

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

## InstituteOS sidecar

This repository carries a committed [`.aii/`](.aii/README.md) sidecar with curated
InstituteOS metadata (`aii-sidecar/v1`): category, maturity, ecosystem relations,
key artifacts, provenance, and portable tasks. It is read by
[InstituteOS](https://github.com/ActiveInferenceInstitute/InstituteOS) tooling and
does not affect the project's build or runtime.
