# The readiness checker

`scripts/check_readiness.py` evaluates the criterion catalogue in
[`../standards/tiers.yaml`](../standards/tiers.yaml) against one repository or a whole fleet. It
is the only program that knows the five evaluation kinds; the criteria themselves are data.
The module docstring at the top of the script is the source of truth for its contract — this
page is the operator view.

## Command line

```bash
# Single repository: a report (no gate)
uv run python scripts/check_readiness.py \
  --repo ActiveInferenceInstitute/.github --path . --json

# Single repository: the blocking gate (exit codes below)
uv run python scripts/check_readiness.py \
  --repo OWNER/REPO --path PATH_TO_REPO --check

# The whole fleet, from a scope table
uv run python scripts/check_readiness.py --all --scope /path/to/scope.tsv --root /Volumes/GitVault --json

# Enable the github-api criteria (needs an authenticated gh)
uv run python scripts/check_readiness.py --repo OWNER/REPO --path PATH --github
```

| Flag | Meaning |
| --- | --- |
| `--repo OWNER/REPO` | a single-repository run (mutually exclusive with `--all`) |
| `--path PATH` | the working copy to measure; required with `--repo` |
| `--all` | evaluate every scope row; requires `--scope` |
| `--scope PATH` | the scope TSV (`repo<TAB>rel`) used by `--all` |
| `--root PATH` | root the scope's relative paths resolve against (default `/Volumes/GitVault`) |
| `--standard PATH` | alternate catalogue; defaults to the in-repository `standards/tiers.yaml` |
| `--tier TIER` | override the tier for a single-repository run |
| `--json` | emit deterministic JSON instead of the human report |
| `--check` | apply the blocking gate and return its exit code |
| `--github` | opt into `github-api` criteria |
| `--today YYYY-MM-DD` | the date used for waiver expiry (default: today) |

Without `--check` the run is a **report** and always exits `0` after a successful build; use
`--check` when you need the gate's verdict. `--path` and `--tier` are rejected with `--all`.

## Exit codes (`--check` only)

| Code | Meaning |
| --- | --- |
| `0` | every blocking criterion passed, was waived or was not applicable |
| `1` | a blocking criterion failed |
| `2` | usage or configuration error: bad arguments, bad catalogue, bad manifest |
| `3` | a criterion could not be evaluated, so the gate cannot be certified |

Exit `3` is the important one: an `unavailable` measurement never becomes a pass and never
becomes a plain failure. A repository with an unreachable API, a missing optional tool or a
timed-out command exits `3` and stays visibly uncertified.

## Determinism

The report schema is `aii-readiness-report/v1`. Two `--json` runs on one commit are
byte-identical: output uses sorted keys, repository-relative paths, no wall-clock timestamps,
no run identifiers and no absolute vault paths. That is why the report is safe to commit, diff
and compare between commits. Do not add timestamps or host paths to the report — the
`ORG-CHECKER-DETERMINISTIC` criterion protects this property.

## Report shape

The report names the repository, the resolved tier, the catalogue version and a status per
criterion; each criterion carries its kind, its blocking flag, the evidence it measured and an
optional waiver. Aggregate counts by status appear at the top so a human can scan a fleet run.
Waiver expiry is decided from `--today`, so a pinned date makes an old report reproducible.

## Related commands

- `scripts/build_public_ledger.py` turns a fleet report into the publishable ledger — see
  [`ledger.md`](ledger.md).
- `scripts/check_ledger_boundary.py` proves a ledger names no private repository.

Neither ledger script runs in CI: the reusable `readiness.yml` workflow only calls
`check_readiness.py` with `--check`. The ledger is rebuilt by the owner from a fleet report and the
scope table; see [`ledger.md`](ledger.md) and [`workflows.md`](workflows.md).
