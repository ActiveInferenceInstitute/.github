# The AII Agent Readiness Standard

This directory holds the organization-wide readiness standard that every
ActiveInferenceInstitute repository is measured against. It is published from the
org `.github` repository, so nothing here is repository-specific: the criteria
are shared vocabulary, and each repository declares its tier and waivers in its
own `.aii/readiness.json`.

| Path | What it is |
|---|---|
| `standards/tiers.yaml` | the criterion catalogue as **data** — tiers, criteria, `kind`, `blocking`, tier applicability, tier exclusions |
| `standards/README.md` | this document: the human contract |
| `standards/public-ledger.json` | the published ledger: the 45 public repositories in full, private repositories as counts only |
| `scripts/check_readiness.py` | the deterministic checker implementing the five evaluation kinds |
| `scripts/build_public_ledger.py` | builds the ledger from a fleet report and the scope table, refusing any private name |
| `scripts/check_ledger_boundary.py` | the `boundary` command: refuses a private name, quoted or not, before the ledger is published |
| `tests/test_check_readiness.py` | the checker's unit tests, including determinism, exit codes and `unavailable != pass` |
| `tests/test_public_ledger.py` | the ledger builder's and boundary command's unit tests |

Adding a criterion edits `standards/tiers.yaml`; it never requires editing
Python. That property is what keeps 65 repositories reviewable by someone who
does not read the checker.

## Standard versions

The catalogue carries its own `schema` and `version`. The version moves when a
criterion's measurement changes, and the published ledger records the version it
was built from.

| Version | Change |
|---|---|
| `1.1.0` | `CORE-CI-CONCURRENCY` no longer expects `cancel-in-progress: true`; it requires an explicit decision (see below). `CORE-CI-TIMEOUT`'s reusable-workflow limitation is documented. |
| `1.0.0` | First published catalogue: 87 criteria over ten tiers, evaluated by the five fixed kinds. |

## The five evaluation kinds

The checker knows exactly five ways to evaluate a criterion. An unknown `kind`
is a configuration error (exit 2), never a silent skip.

| Kind | Semantics | Reported `unavailable` when |
|---|---|---|
| `file-exists` | a path or glob is present in the tracked tree; `match: all` requires every declared path, and `non_empty: true` fails when a `match: all` path is empty (a single/any match passes if any matched file is non-empty) | the working copy could not be read |
| `file-contains` | a path is present and its regex / `all_of` / `any_of` / `forbid` rule matches; evidence names `path:line` for `match: any` and for `match: all` | the file exists but cannot be read or decoded |
| `yaml-path` | a dotted path resolves in a YAML document, with `*` meaning "every child" | the document does not parse |
| `command` | a **bounded** local command exits 0 | the tool is missing or the command times out |
| `github-api` | an opt-in REST read, enabled only by `--github` | the flag is absent, or the response is 403 / rate-limited |

An **absent** path is a `fail`, never `unavailable`. The criterion asks whether
the pattern is present, and a missing file is a definite answer; `unavailable`
is reserved for the case where no answer can be obtained at all. The
distinction is load-bearing: a waiver only converts `fail` to `waived`, so an
absent path reported as `unavailable` could never be waived.

A `command` criterion runs the specific gate the criterion names. It never runs
a repository's whole test suite.

### Spec keys

A criterion's `spec` *is* the measurement, so its keys and its enumerated values
are part of the contract the checker enforces. An unknown `kind` is already a
configuration error, and so is a value outside the vocabulary below: `match: All`
would otherwise be read as "any", `on_empty: n/a` would become `fail`,
`parses: Yaml` would skip the parse check, and a non-numeric `timeout` would
raise inside the runner instead of reporting a catalogue error. All of those are
exit `2`.

| Key | Kind | Meaning |
|---|---|---|
| `path` / `paths` | `file-exists`, `file-contains` | one path or glob, or a list of them |
| `file` / `files` | `yaml-path` | one YAML document or glob, or a list of them |
| `match` | all | `all` or `any`. `all` requires every declared path, `any` needs one. The default is `any`, except for `yaml-path`, which defaults to `all` |
| `on_empty` | all | `fail`, `pass`, `unavailable` or `not-applicable`, reported when no declared path matched at all. The default is `fail`, except for `yaml-path`, which defaults to `not-applicable` |
| `non_empty` | `file-exists` | `true` makes an empty matched file a `fail` |
| `parses` | `file-exists` | `yaml` or `json`: every matched file must parse |
| `declared_in` | `file-exists` | a manifest whose license field satisfies the criterion when no declared path matched |
| `timeout` | `command` | a positive whole number of seconds; a timeout reports `unavailable` |
| `skip_if_no_paths` | `command` | `not-applicable` when none of the declared paths matched |
| `require` / `expect` | `yaml-path` | dotted paths that must resolve, or a dotted path and the value it must equal |
| `ecosystems` / `precedence` | `yaml-path` | the lockfile-to-ecosystem map and its precedence groups |
| `endpoint` / `endpoints` | `github-api` | REST paths using `{repo}` and `{org}`, and no other placeholder |
| `regex` / `all_of` / `any_of` | `file-contains` | patterns the document must match |
| `forbid` | `file-contains` | a pattern the document must not match |
| `floor_in_ci` / `coverage_threshold_match` / `cname_match` / `node_pin` | `file-contains` | the cross-file rules below |

### The cross-file `file-contains` rules

Four keys compare two files instead of matching one, which is why they are keys
rather than patterns:

| Key | Criterion | Measurement |
|---|---|---|
| `floor_in_ci` | `PY-PYTHON-FLOOR` | the `requires-python` floor in `pyproject.toml` appears in a CI workflow |
| `coverage_threshold_match` | `PY-COVERAGE-MATCH` | the coverage threshold declared in `pyproject.toml` equals the one CI enforces |
| `cname_match` | `HTML-CNAME-MATCH` | the host in `CNAME` appears in a CI workflow |
| `node_pin` | `TS-NODE-PIN` | the Node version pinned by `.nvmrc` or `engines.node` appears in a CI workflow |

### Lockfile precedence (`CORE-DEPENDABOT-ECOSYSTEM`)

The dependabot criterion must measure the lockfile a repository *actually* uses,
so the catalogue declares lockfile **precedence groups**, most authoritative
first:

1. package-manager lockfiles — `uv.lock`, `poetry.lock`, `Pipfile.lock`,
   `pdm.lock`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`,
   `bun.lockb`, `Cargo.lock`, `go.sum`, `Manifest.toml`;
2. legacy `requirements.txt`.

The first group holding a present lockfile is the repository's real ecosystem,
and **every** lockfile present in it must be watched. Lower groups are ignored as
legacy while a higher group is present. Selection never depends on alphabetical
or declaration order: `requirements.txt` + `uv.lock` requires `uv`, and
`package-lock.json` + `uv.lock` requires both `npm` and `uv`. A repository whose
real lockfile ecosystem is unwatched still fails the criterion.

Lockfile presence is resolved from the **tracked tree** (`git ls-files`), like
every other kind. A lockfile that exists on disk but is ignored or untracked is
not one Dependabot can see, so it does not select an ecosystem; a repository
with no tracked lockfile fails with `no tracked lock file matches a known
ecosystem`, naming any ignored lockfile it found on disk.

## The five statuses

Every criterion resolves to exactly one of:

- `pass` — measured and satisfied.
- `fail` — measured and not satisfied.
- `waived` — a `fail` converted by a declared, non-expired waiver.
- `unavailable` — could not be measured, with the reason.
- `not-applicable` — the tier (or a tier exclusion) does not apply it.

`unavailable` is never a pass. A criterion that could not be measured says so,
and `--check` then fails to certify the repository. This honesty invariant is
enforced by the checker's tests.

## Exit codes

`--check` is the blocking gate; without it the checker is a report.

| Code | Meaning |
|---|---|
| `0` | every criterion passed, was waived or was not applicable |
| `1` | at least one blocking criterion failed |
| `2` | usage, catalogue or manifest error |
| `3` | at least one criterion was unevaluable (`unavailable`) |

`3` outranks `1`. `--check` certifies that the standard was **measured** as well
as met, so an unevaluable criterion leaves the run uncertifiable whether or not
it is blocking: `blocking` decides whether a definite *failure* is fatal, never
whether an unmeasurable criterion can be certified. The three `CORE-INHERIT-*`
criteria are `github-api` and enter non-blocking under the ratchet, so an offline
`--check` returns `3` by design; pass `--github` with a pinned token when a `0`/`1`
answer is required.

## Tiers

A repository's tier is decided from **tracked files** (`git ls-files`), never
from GitHub's `primaryLanguage` and never from a disk walk: disk counts are
inflated up to 100× by untracked generated trees. The rules are in
`library/readiness-standard.md` in the mission record and are implemented by
`classify_tier`.

| Tier | Includes | Applies |
|---|---|---|
| `core` | — | every repository: README, AGENTS, license, gitignore, lock, CI triggers, permissions, timeouts, pinning, concurrency, dependabot, readiness manifest, actionlint |
| `python-library` | `core` | Python projects with a root `pyproject.toml` and no app entrypoint: UV lock, frozen sync, lock check, Python floor, Ruff `target-version` + `extend-select`, Ruff/mypy/pytest CI, coverage configuration and execution |
| `python-service` | `core`, `python-library` | Python projects with an app entrypoint: adds health/readiness endpoints, structured logging, metrics, tracing, env template, runbook and OpenAPI diff |
| `typescript-node` | `core` | TypeScript/Node projects: strict `tsconfig`, `tsc --noEmit`, lint, formatter check, vitest coverage, build, lockfile and a matched Node pin |
| `typescript-bun` | `core` | Bun projects: `bun.lock`/`bunfig.toml` signal, pinned Bun version in CI, test, typecheck, lint and build |
| `static-html` | `core` | Static sites: HTML validation, prettier format check, link checking, an *inverse* criterion that forbids a coverage gate, and CNAME/host agreement |
| `julia` | `core` | Julia projects: `Project.toml`, JuliaFormatter, `Pkg.test()` in CI; the Python toolchain is not applied |
| `notebook` | `core` | Notebook-defined projects: Ruff-clean notebooks, output stripping, and exclusion from type checks and coverage. Declared for future use — no repository currently satisfies the rule |
| `content-data` | `core` | Content and data repositories: link checks, front-matter checks, and an *inverse* criterion that adding a test framework is a failure |
| `org` | `core` | the `ActiveInferenceInstitute/.github` repository itself: community-health preservation, CODEOWNERS, FUNDING, the catalogue, checker determinism/tests/exit codes, actionlint-clean workflows, the public-only ledger, and `standards/README.md` |

`ORG-*` criteria apply only to the `org` tier. A criterion never crosses a tier
boundary by accident: `within` names the tiers that own it.

### The org tier exclusion: `CORE-CI-ON`

`CORE-CI-ON` requires a repository's own workflows to trigger on
`pull_request` and on the default branch. The org `.github` repository ships
`workflow_call`-only reusable workflows **by design** and never declares those
triggers itself, so the criterion cannot be satisfied there without breaking the
standard. It is therefore recorded as a **tier exclusion** on `org` in
`standards/tiers.yaml`, with the reason, and reports `not-applicable`:

```yaml
org:
  includes: [core]
  excludes:
    - criterion: CORE-CI-ON
      reason: >-
        The standard repository ships workflow_call-only reusable workflows by
        design and never declares push or pull_request triggers itself, so a
        repository-trigger criterion is unsatisfiable here by construction.
        Adopters still declare their own push/pull_request triggers in the
        local workflow that calls the reusable one.
```

An exclusion is **not** a waiver. A waiver excuses a measured failure in one
repository; an exclusion says the criterion does not apply to a tier by
construction. Exclusions are declared once here and never leak to another tier.
The checker rejects an exclusion that names an unknown criterion or a criterion
that does not apply to the tier. Repositories outside the org tier still measure
`CORE-CI-ON` normally: delegating a gate to a reusable workflow does not remove
the adopter's obligation to declare its own `push`/`pull_request` triggers.

### Known limitation: `CORE-CI-ON` does not check the pushed branch

`CORE-CI-ON` measures that at least one workflow declares `on.pull_request` and
`on.push`. It does **not** require `push` to name or include the repository's
default branch, so a repository whose push trigger targets only a non-default
branch still passes. This is a **known limitation, deliberately not tightened**:
adding the branch filter would introduce a new blocking failure for every
repository that has not yet adopted the standard, which the ratchet forbids. If
branch targeting matters, it becomes a separate non-blocking criterion and is
promoted only after its tranche is green. The criterion's title describes the
intended state; the measurement is trigger presence.

### `CORE-CI-CONCURRENCY` asks for a decision, not a value

The criterion requires every workflow to declare `concurrency` and to state
`cancel-in-progress` explicitly. Catalogue 1.0.0 additionally demanded the value
`true`, which is wrong for the workflows that publish: cancelling a wheel upload,
a nightly publication branch or a reviewed Pages deployment mid-flight destroys
the artifact the run exists to produce. Those workflows declare `false` and say
so. Either value satisfies the criterion, because the value is the workflow
owner's decision and the failure it measures is silence — a workflow that
inherits whatever GitHub's default happens to be for its trigger.

### Known limitation: a reusable-workflow call job cannot declare `timeout-minutes`

`CORE-CI-TIMEOUT` requires every job to declare `timeout-minutes`. A job that
calls a reusable workflow (`uses:` at job level) accepts only `name`, `uses`,
`with`, `secrets`, `needs`, `if` and `permissions`; `actionlint` rejects the key
there, and the bound belongs to the called workflow. Where a checked workflow
contains such a job, the criterion keeps reporting a failure that cannot be fixed
inside the repository. The adopting repository adds `timeout-minutes` to the
reusable workflow it calls; `ActiveInferenceJournal` and `GEO_INFER` measure this
way today.

### Known limitation: the org tier does not measure the `workflow_call` trigger

`ORG-REUSABLE-WORKFLOWS` runs `actionlint -no-color` over `.github/workflows/`,
so it measures that every workflow parses and is lint-clean. It does **not**
measure that each workflow is `on: workflow_call` only. That property is why the
org repository is exempt from `CORE-CI-ON`, but it is enforced by review
(`AGENTS.md`, "Conventions") and not by a criterion: a workflow that added a
`push` trigger would stay lint-clean and pass. Tightening it needs a separate
criterion, adopted under the ratchet like any other.

## The ratchet

A new criterion enters with `blocking: false`. It is promoted to
`blocking: true` only after it is green across its whole adoption tranche. This
is the mechanical guarantee behind **never add a blocking gate to a red
repository**: adoption cannot make a repository worse, because nothing adopted
blocks until it passes. Non-blocking criteria are still evaluated and reported;
they simply do not decide the exit code.

## Per-repository manifest and waivers

Each repository declares its tier and waivers in `.aii/readiness.json`:

```json
{
  "schema": "aii-readiness/v1",
  "repo": "ActiveInferenceInstitute/.github",
  "tier": "python-library",
  "standard_version": "1.1.0",
  "default_branch": "main",
  "baseline": { "commit": "<40-hex>", "measured_at": "YYYY-MM-DD" },
  "waivers": [
    { "criterion": "HTML-VALIDATE", "reason": "generated pages; regeneration is the validator",
      "expires": "2027-06-30" }
  ]
}
```

| Field | Read by the checker | Meaning |
|---|---|---|
| `schema` | yes | must be `aii-readiness/v1`; any other value is a configuration error |
| `repo` | no | the `OWNER/REPO` id, for people and for the sidecar |
| `tier` | yes | the tier whose criteria apply; an unknown tier is a configuration error, and a declared tier overrides classification |
| `standard_version` | no | the catalogue revision the manifest was written against; informational, because a repository may lag a catalogue revision |
| `default_branch` | yes | the branch the report records for the repository; otherwise resolved from `origin/HEAD` or the scope row |
| `baseline` | only as text | `baseline.commit` is what `CORE-READINESS` matches as 40 hex; the checker itself does not read the object |
| `waivers` | yes | `criterion`, a non-empty `reason` and an optional `expires` date |

A waiver requires a non-empty `reason` and a known criterion id; a missing or
blank reason is a configuration error. A waiver only converts `fail` to
`waived`; it never turns a pass into anything else. An expired waiver is ignored
and the criterion is measured normally.

Readiness metadata deliberately does **not** live in `.aii/config.yaml`: that
file's `aii-sidecar/v1` schema is validated by `aii-platform`, and adding a key
there would let a readiness change break another repository's gate.

## Determinism

Two `--json` runs on the same commit must be byte-identical. Therefore the
report contains repository-relative paths, sorted keys and sorted repository
order, no wall-clock timestamps, no Actions run ids, no `pushedAt`, and no
floating version strings fetched over the network. Declared versions are read
from files. The test suite enforces this contract.

## Public/private boundary

The org `.github` repository is public. The publishable ledger,
`standards/public-ledger.json`, contains the 45 public repositories in full plus
aggregate counts for the 20 private repositories; no private repository name,
URL or content may appear in it. That file is produced by the ledger milestone,
not by this standard's data files, and the boundary is machine-checked by
`ORG-LEDGER-PUBLIC-ONLY` and the `boundary` command.

The ledger's member set is fixed by `scripts/build_public_ledger.py`:

| Member | Content |
|---|---|
| `schema` | `aii-readiness-ledger/v1` |
| `standard` | the catalogue `schema` and `version` the ledger was built from |
| `scope` | how many scope rows are `public`, `private` and `total` |
| `public` | `count`, an aggregate `summary`, and `repos`: every public repository in full |
| `private` | `count` and an aggregate `summary` only |

A public repository entry carries its `tier`, `tier_source`, `commit`,
`default_branch`, per-criterion `criteria` and `summary`, exactly as the checker
reports them.

## Running the checker

```bash
# Report for one repository (deterministic JSON).
uv run python scripts/check_readiness.py \
  --repo OWNER/REPO --path PATH_TO_REPO --json

# Blocking gate. Add --github with a pinned token for a 0/1 answer.
uv run python scripts/check_readiness.py \
  --repo OWNER/REPO --path PATH_TO_REPO --check

# Fleet report from the configured 65-row scope (never enumerate the org).
uv run python scripts/check_readiness.py \
  --all --scope /path/to/scope.tsv --json
```

Other flags: `--tier` overrides classification, `--standard` selects an
alternate catalogue, `--today YYYY-MM-DD` pins waiver expiry for deterministic
tests, and `--root` sets where scope-relative paths resolve.
