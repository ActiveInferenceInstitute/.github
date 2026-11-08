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
| `scripts/check_readiness.py` | the deterministic checker implementing the five evaluation kinds |
| `tests/test_check_readiness.py` | the checker's unit tests, including determinism, exit codes and `unavailable != pass` |

Adding a criterion edits `standards/tiers.yaml`; it never requires editing
Python. That property is what keeps 65 repositories reviewable by someone who
does not read the checker.

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
| `0` | every blocking criterion passed, was waived or was not applicable |
| `1` | at least one blocking criterion failed |
| `2` | usage, catalogue or manifest error |
| `3` | at least one blocking criterion was unevaluable (`unavailable`) |

`3` outranks `1`. Because the three `CORE-INHERIT-*` criteria are `github-api`,
an offline `--check` returns `3` by design; pass `--github` with a pinned token
when a `0`/`1` answer is required.

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
| `org` | `core` | the `ActiveInferenceInstitute/.github` repository itself: community-health preservation, CODEOWNERS, FUNDING, the catalogue, checker determinism/tests/exit codes, actionlint-clean reusable workflows, the public-only ledger, and `standards/README.md` |

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
  "tier": "python-library",
  "baseline": { "commit": "<40-hex>", "measured_at": "YYYY-MM-DD" },
  "waivers": [
    { "criterion": "HTML-VALIDATE", "reason": "generated pages; regeneration is the validator",
      "expires": "2027-06-30" }
  ]
}
```

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
