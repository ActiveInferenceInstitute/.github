# scripts/ — the readiness tooling

The Python tools behind the standard. They are run through `uv run` so they use the versions
pinned in `uv.lock`; see [`../docs/checker.md`](../docs/checker.md) and
[`../docs/ledger.md`](../docs/ledger.md) for the operator view.

| Script | What it does |
| --- | --- |
| `check_readiness.py` | Evaluates the criterion catalogue against one repository (`--repo`) or a fleet (`--all`), as a report or a blocking gate. |
| `build_public_ledger.py` | Builds `../standards/public-ledger.json` from a fleet report and the scope table, refusing any private repository name. |
| `check_ledger_boundary.py` | The `boundary` command: scans a ledger against the scope table and fails on any private name or URL, quoted or not. |

## Common invocations

```bash
uv run python scripts/check_readiness.py --repo OWNER/REPO --path PATH --json
uv run python scripts/check_readiness.py --all --scope /path/to/scope.tsv --root /Volumes/GitVault --json
uv run python scripts/build_public_ledger.py --report /tmp/fleet.json --scope /path/to/scope.tsv --out ../standards/public-ledger.json
uv run python scripts/check_ledger_boundary.py --ledger ../standards/public-ledger.json --scope /path/to/scope.tsv
```

`check_ledger_boundary.py` imports `build_public_ledger` as a sibling module
(`sys.path[0]` is this directory when either runs), so the two must be moved together.

## Tests

The suite lives in `../tests/`; run it with `uv run pytest -q`. Do not generate or download
fixtures here — every test is offline by design.

> `audit_required_contexts.py` is untracked owner work-in-progress and is intentionally not part
> of this repository's committed tooling. Do not stage or modify it.
