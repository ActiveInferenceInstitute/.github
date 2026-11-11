# The readiness standard

The standard is the shared vocabulary every ActiveInferenceInstitute repository is measured
against. It is published from this repository, but nothing in it is repository-specific: the
criteria are shared, and each repository declares the tier it belongs to and any waivers in
its own `.aii/readiness.json`.

The authoritative human contract is [`../standards/README.md`](../standards/README.md); this
document is a map of it. Read that contract before changing a criterion — it is the document
reviewers are held to.

## Criteria are data

Adding or changing a criterion edits [`../standards/tiers.yaml`](../standards/tiers.yaml); it
never requires editing Python. The catalogue carries its own `schema` and `version`, and the
checker knows exactly **five fixed evaluation kinds**:

| Kind | Measures |
| --- | --- |
| `file-exists` | a path or glob is present in the tracked tree (optionally parsing, non-empty) |
| `file-contains` | a file's regex / `all_of` / `any_of` / `forbid` rule matches |
| `yaml-path` | a dotted path resolves in a YAML document |
| `command` | a bounded local command exits 0 |
| `github-api` | an opt-in REST read, enabled only by `--github` |

An unknown `kind`, or a spec value outside the catalogue's vocabulary, is a configuration
error (exit `2`) — never a silent skip. The five statuses are `pass`, `fail`, `waived`,
`unavailable` and `not-applicable`. The load-bearing invariant is **`unavailable` is never
`pass`**: a criterion that could not be measured says so, and the gate then refuses to certify
the repository.

## Tiers

A repository's tier is decided from **tracked files** (`git ls-files`), never from GitHub's
`primaryLanguage` and never from a disk walk. The ten tiers are `core`, `python-library`,
`python-service`, `typescript-node`, `typescript-bun`, `static-html`, `julia`, `notebook`,
`content-data` and `org`. `org` is this repository's own tier and adds the `ORG-*` criteria
(community-health preservation, the catalogue, checker determinism, the public-only ledger).

A tier **exclusion** (`excludes:`) says a criterion does not apply to a tier by construction —
for example `CORE-CI-ON` on `org`, because this repository deliberately ships
`workflow_call`-only workflows. An exclusion is not a waiver.

## Waivers and the ratchet

- A waiver lives in the repository's `.aii/readiness.json`, requires a non-empty `reason`, and
  only converts `fail` to `waived`. It never turns an untested criterion green, and an expired
  waiver is ignored.
- New criteria enter with `blocking: false` and are promoted only after their adoption tranche
  is green. This is the guarantee behind **never add a blocking gate to a red repository**.

## Declaring a repository's tier

Each repository writes `.aii/readiness.json`:

```json
{
  "schema": "aii-readiness/v1",
  "repo": "ActiveInferenceInstitute/<repo>",
  "tier": "python-library",
  "standard_version": "1.1.0",
  "default_branch": "main",
  "baseline": { "commit": "<40-hex>", "measured_at": "YYYY-MM-DD" },
  "waivers": []
}
```

The checker reads `schema`, `tier`, `default_branch` and `waivers`; `standard_version` is
informational (a repository may lag a catalogue revision). Readiness metadata deliberately does
**not** live in `.aii/config.yaml`, whose `aii-sidecar/v1` schema is validated by a different
tool.

## Adopting the standard

1. Measure the repository with a report (never a gate):
   `uv run python scripts/check_readiness.py --repo OWNER/REPO --path PATH_TO_REPO --json`
   (see [`checker.md`](checker.md)).
2. Add or correct `.aii/readiness.json` with the measured tier.
3. Call the reusable [`workflows.md`](workflows.md) gate, or the repository's own CI, so the
   criterion set is enforced on pull requests.
4. Record a waiver only for a measured `fail` with a real reason, not to work around an
   `unavailable`.

## Known limitations

The contract documents the limitations deliberately left in place — `CORE-CI-ON` does not check
the pushed branch, `CORE-CI-CONCURRENCY` asks for a decision rather than the value `true`, a
reusable-workflow call job cannot declare `timeout-minutes`, and the `org` tier does not measure
the `workflow_call` trigger. See [`../standards/README.md`](../standards/README.md) for each.
