#!/usr/bin/env python3
"""Audit every required status-check context against default-branch workflow YAML.

This program compares effective ruleset contexts for a repository's default
branch with check names derived from its workflow definitions.

Two rules make the measurement trustworthy:

* The required string comes from the live ruleset API, never from a captured file.
  ``--capture`` compares a captured baseline against live state so that a stale
  capture is proven wrong rather than trusted.
* The produced string is derived from ``git show origin/<default>:<path>``.  A
  working tree may be checked out on a preserved branch; reading it is how the
  2026-11-06 capture wrongly recorded two contexts as unsatisfiable.  The working
  tree is never read for derivation.

Derivation follows GitHub's four naming cases:

``name``            a job with ``name:`` reports that string;
``job_id``          a job with no ``name:`` reports its job id (GEN24's ``tests``);
``matrix``          an unnamed matrix job reports ``<job id> (<legs>)``, and a named
                    matrix job substitutes ``${{ matrix.<key> }}`` into ``name:``;
``reusable-caller`` a ``uses:`` caller reports ``<caller display> / <callee display>``
                    for the supported caller and callee naming shapes.

Exit codes:
    0  every required context is produced exactly, the capture agrees, and every
       named matrix leg exists
    1  a context is missing or changed, a job that derived its context from its
       job id gained a ``name:``, a matrix leg is gone, or the capture disagrees
    2  usage, identity or configuration error
    3  something could not be measured (missing checkout, stale origin ref, API
       failure, unparseable workflow, non-literal matrix) - never a pass

The report is deterministic for one fleet state: sorted keys, repository-relative
paths, no wall-clock timestamps, no run identifiers and no absolute vault paths.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

ORG = "ActiveInferenceInstitute"
REPORT_SCHEMA = "aii-required-context-audit/v1"
WORKFLOW_SUFFIXES = (".yml", ".yaml")
WORKFLOW_DIR = ".github/workflows"
DEFAULT_EXPECT_REPOS = 36
DEFAULT_EXPECT_DISTINCT = 22
DEFAULT_EXPECT_LOGIN = "ActInfInstitute"
API_TIMEOUT = 120
GIT_TIMEOUT = 120
FETCH_TIMEOUT = 600

# VAL-CTX-006 names these four contexts explicitly.  Three are produced by an
# unnamed matrix job; the fourth is a literal job name in its adopter (the org
# reusable workflow models it as a matrix, but ActiveInferenceImplementations
# does not).  The report records which shape was actually observed.
CONTRACT_MATRIX_CONTEXTS = (
    "validate (3.11)",
    "probes (ubuntu-latest, 3.15)",
    "build-and-check (24, 3.12)",
    "tests and notebooks (julia 1.13)",
)

MATRIX_CONTEXT_RE = re.compile(r"^(?P<base>.+) \((?P<legs>[^()]*)\)$")
EXPRESSION_RE = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)
MATRIX_DOT_RE = re.compile(r"matrix\.(?P<key>[A-Za-z0-9_-]+)")
MATRIX_INDEX_RE = re.compile(r"matrix\[\s*['\"](?P<key>[^'\"]+)['\"]\s*\]")


class AuditError(Exception):
    """A usage or configuration error (exit code 2)."""


@dataclass(frozen=True)
class DerivedDisplay:
    """One check-run name a job can report."""

    display: str
    mechanism: str
    matrix_values: tuple[tuple[str, str], ...] | None = None
    note: str | None = None


@dataclass(frozen=True)
class JobDerivation:
    """One job, with every display name it can report."""

    workflow: str
    job_id: str
    raw_name: str | None
    has_name: bool
    displays: tuple[DerivedDisplay, ...]
    combos: tuple[tuple[tuple[str, str], ...], ...] | None = None
    matrix_keys: tuple[str, ...] = ()
    unexpanded: str | None = None


@dataclass(frozen=True)
class WorkflowDerivation:
    """One workflow file and its jobs."""

    path: str
    jobs: tuple[JobDerivation, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class CaptureBaseline:
    """The 2026-11-06 capture, parsed so it can be proven rather than trusted."""

    declared_repos: int | None
    declared_distinct: int | None
    repo_contexts: dict[str, tuple[str, ...]]
    historical: tuple[dict[str, str], ...]


# ---------------------------------------------------------------------------
# YAML parsing and display-name derivation
# ---------------------------------------------------------------------------


def _stringify(value: Any) -> str | None:
    """Render a matrix scalar the way GitHub renders it, or reject ambiguity."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # 3.10 and 3.1 would render identically; never guess a leg value.
        return None
    if isinstance(value, str):
        return value
    return None


def _render_matrix_entry(
    entry: Mapping[Any, Any], label: str
) -> tuple[dict[str, str] | None, str | None]:
    rendered: dict[str, str] = {}
    for key, value in entry.items():
        item = _stringify(value)
        if item is None:
            return None, f"matrix {label} value {value!r} is ambiguous"
        rendered[str(key)] = item
    return rendered, None


def enumerate_matrix(
    matrix: Any,
) -> tuple[tuple[tuple[tuple[str, str], ...], ...], tuple[str, ...], str | None]:
    """Expand a matrix into ordered (key, value) combinations.

    Returns ``(combos, keys, error)``.  ``error`` means the matrix is not a
    literal enumeration (an expression, a non-list value, or an ambiguous float);
    the caller must report that repository as unavailable rather than invent legs.
    """
    if not isinstance(matrix, dict):
        return (), (), "matrix is not a mapping"
    include = matrix.get("include") or []
    exclude = matrix.get("exclude") or []
    if not isinstance(include, list) or not isinstance(exclude, list):
        return (), (), "matrix include/exclude is not a list"

    base_keys = [key for key in matrix if key not in {"include", "exclude"}]
    value_lists: dict[str, list[str]] = {}
    for key in base_keys:
        values = matrix[key]
        if not isinstance(values, list):
            return (), (), f"matrix key {key!r} is not a literal list"
        rendered: list[str] = []
        for value in values:
            item = _stringify(value)
            if item is None:
                return (), (), f"matrix key {key!r} has an ambiguous value {value!r}"
            rendered.append(item)
        value_lists[key] = rendered

    combos: list[dict[str, str]] = []
    if base_keys:
        for product in itertools.product(*(value_lists[key] for key in base_keys)):
            combos.append(dict(zip(base_keys, product, strict=True)))

    def _matches(combo: Mapping[str, str], entry: Mapping[str, str]) -> bool:
        return all(combo.get(key) == value for key, value in entry.items())

    # Exclusions remove base combinations before include entries are applied.
    for entry in exclude:
        if not isinstance(entry, dict):
            return (), (), "matrix exclude entry is not a mapping"
        excluded, error = _render_matrix_entry(entry, "exclude")
        if excluded is None:
            return (), (), error
        combos = [combo for combo in combos if not _matches(combo, excluded)]

    extra_combos: list[dict[str, str]] = []
    extra_keys: list[str] = []
    for entry in include:
        if not isinstance(entry, dict):
            return (), (), "matrix include entry is not a mapping"
        included, error = _render_matrix_entry(entry, "include")
        if included is None:
            return (), (), error

        # An include entry augments a matching combination when it overwrites no
        # base value; otherwise GitHub creates an additional combination.  Keys
        # that exist only in the include entry are added, not compared.
        def _can_augment(combo: Mapping[str, str], entry: Mapping[str, str]) -> bool:
            return all(combo[key] == value for key, value in entry.items() if key in combo)

        target = next((combo for combo in combos if _can_augment(combo, included)), None)
        if target is not None:
            target.update({key: value for key, value in included.items() if key not in target})
            continue
        extra_combos.append(included)
        for key in included:
            if key not in extra_keys:
                extra_keys.append(key)

    all_keys = tuple(base_keys) + tuple(key for key in extra_keys if key not in base_keys)
    combos.extend(extra_combos)
    ordered = tuple(tuple(combo.items()) for combo in combos)
    return ordered, all_keys, None


def _expand_name(template: str, values: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Substitute whole ``${{ matrix.<key> }}`` expressions; reject the rest.

    Only a bare matrix reference is expanded.  A richer expression (a comparison,
    ``format()``, a ``&&``/``||`` chain) is left in place and reported as
    unexpanded, because guessing its result could invent a context string.
    """

    def _replace(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        simple = MATRIX_DOT_RE.fullmatch(body) or MATRIX_INDEX_RE.fullmatch(body)
        if simple is None:
            return match.group(0)
        key = simple.group("key")
        return values.get(key, match.group(0))

    expanded = EXPRESSION_RE.sub(_replace, template)
    residual = EXPRESSION_RE.search(expanded)
    if residual:
        return None, f"unexpanded expression: {residual.group(0).strip()}"
    return expanded, None


def _job_displays(
    job_id: str,
    job: Mapping[str, Any],
    combos: tuple[tuple[tuple[str, str], ...], ...] | None,
    matrix_error: str | None,
    uses_resolver: Callable[[str], tuple[tuple[str, ...] | None, str | None]] | None,
) -> tuple[tuple[DerivedDisplay, ...], str | None]:
    raw_name = job.get("name")
    has_name = isinstance(raw_name, str) and bool(raw_name)
    name = raw_name if has_name else None

    if "uses" in job:
        caller = name or job_id
        uses = job["uses"]
        if not isinstance(uses, str):
            return (), "uses value is not a string"
        if uses_resolver is None:
            return (
                DerivedDisplay(
                    display=f"{caller} / <unresolved callee>",
                    mechanism="reusable-caller",
                    note="remote reusable workflow; its job displays are not in this repository",
                ),
            ), None
        callees, error = uses_resolver(uses)
        if callees is None:
            return (
                DerivedDisplay(
                    display=f"{caller} / <unresolved callee>",
                    mechanism="reusable-caller",
                    note=error or "callee not resolvable",
                ),
            ), None
        return (
            tuple(
                DerivedDisplay(
                    display=f"{caller} / {callee}",
                    mechanism="reusable-caller",
                    note="measured composite naming rule",
                )
                for callee in callees
            ),
            None,
        )

    if matrix_error is not None:
        return (), matrix_error

    if has_name:
        assert name is not None
        if matrix_error is None and combos is not None and combos:
            displays: list[DerivedDisplay] = []
            for combo in combos:
                values = dict(combo)
                display, error = _expand_name(name, values)
                if display is None:
                    return (), error
                displays.append(
                    DerivedDisplay(
                        display=display,
                        mechanism="matrix-name",
                        matrix_values=combo,
                    )
                )
            return tuple(displays), None
        display, error = _expand_name(name, {})
        if display is None:
            return (), error
        return (
            DerivedDisplay(
                display=display,
                mechanism="name",
                matrix_values=None,
            ),
        ), None

    if combos is not None and combos:
        displays = []
        for combo in combos:
            legs = ", ".join(value for _key, value in combo)
            displays.append(
                DerivedDisplay(
                    display=f"{job_id} ({legs})",
                    mechanism="matrix-job-id",
                    matrix_values=combo,
                )
            )
        return tuple(displays), None

    return (DerivedDisplay(display=job_id, mechanism="job_id"),), None


def derive_workflow(
    path: str,
    text: str,
    uses_resolver: Callable[[str], tuple[tuple[str, ...] | None, str | None]] | None = None,
) -> WorkflowDerivation:
    """Derive every check-run name a workflow can report."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return WorkflowDerivation(path=path, error=f"YAML parse error: {exc}")
    if not isinstance(document, dict):
        return WorkflowDerivation(path=path, error="workflow is not a mapping")
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return WorkflowDerivation(path=path, error="workflow has no jobs mapping")

    derived: list[JobDerivation] = []
    for job_id in sorted(str(key) for key in jobs):
        job = jobs[job_id]
        if not isinstance(job, dict):
            derived.append(
                JobDerivation(
                    workflow=path,
                    job_id=job_id,
                    raw_name=None,
                    has_name=False,
                    displays=(),
                    unexpanded="job is not a mapping",
                )
            )
            continue
        strategy = job.get("strategy")
        matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
        combos: tuple[tuple[tuple[str, str], ...], ...] | None = None
        matrix_keys: tuple[str, ...] = ()
        matrix_error: str | None = None
        if matrix is not None:
            combos, matrix_keys, matrix_error = enumerate_matrix(matrix)
        displays, unexpanded = _job_displays(job_id, job, combos, matrix_error, uses_resolver)
        raw_name = job.get("name")
        derived.append(
            JobDerivation(
                workflow=path,
                job_id=job_id,
                raw_name=raw_name if isinstance(raw_name, str) else None,
                has_name=isinstance(raw_name, str) and bool(raw_name),
                displays=displays,
                combos=combos,
                matrix_keys=matrix_keys,
                unexpanded=unexpanded,
            )
        )
    return WorkflowDerivation(path=path, jobs=tuple(derived))


def derive_repo_workflows(contents: Mapping[str, str]) -> tuple[WorkflowDerivation, ...]:
    """Derive all workflows in a repository, resolving local reusable callees."""

    def resolver(uses: str) -> tuple[tuple[str, ...] | None, str | None]:
        if not uses.startswith("./"):
            return None, "remote reusable workflow; callee job displays are not resolvable here"
        callee_path = uses[2:]
        text = contents.get(callee_path)
        if text is None:
            return None, f"local reusable workflow {callee_path} was not read"
        callee = derive_workflow(callee_path, text, uses_resolver=None)
        if callee.error is not None:
            return None, callee.error
        displays: list[str] = []
        for job in callee.jobs:
            displays.extend(display.display for display in job.displays)
        if not displays:
            return None, f"local reusable workflow {callee_path} exposes no job display"
        return tuple(sorted(displays)), None

    return tuple(
        derive_workflow(path, contents[path], uses_resolver=resolver) for path in sorted(contents)
    )


# ---------------------------------------------------------------------------
# Requirement matching
# ---------------------------------------------------------------------------


def _all_displays(workflows: Sequence[WorkflowDerivation]) -> list[dict[str, Any]]:
    produced: list[dict[str, Any]] = []
    for workflow in workflows:
        for job in workflow.jobs:
            for display in job.displays:
                produced.append(
                    {
                        "workflow": workflow.path,
                        "job_id": job.job_id,
                        "display": display.display,
                        "mechanism": display.mechanism,
                        "job_name": job.raw_name,
                        "matrix_values": (
                            dict(display.matrix_values) if display.matrix_values else None
                        ),
                        "note": display.note,
                    }
                )
    produced.sort(key=lambda item: (item["workflow"], item["job_id"], item["display"]))
    return produced


def _unexpanded_jobs(workflows: Sequence[WorkflowDerivation]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for workflow in workflows:
        if workflow.error is not None:
            out.append({"workflow": workflow.path, "reason": workflow.error})
            continue
        for job in workflow.jobs:
            if job.unexpanded is not None:
                out.append(
                    {
                        "workflow": workflow.path,
                        "job_id": job.job_id,
                        "reason": job.unexpanded,
                    }
                )
    return out


def matrix_leg_check(
    context: str, workflows: Sequence[WorkflowDerivation]
) -> dict[str, Any] | None:
    """Check every leg named in a context against the job's real matrix.

    Applies to contexts of the shape ``<base> (<leg>, <leg>)`` where an unnamed
    job with id ``<base>`` declares a matrix.  Returns the observed leg mapping,
    or ``None`` when no such job exists (for example a literal job name).
    """
    match = MATRIX_CONTEXT_RE.match(context)
    if match is None:
        return None
    base = match.group("base")
    legs = [leg.strip() for leg in match.group("legs").split(",")]
    candidates: list[dict[str, Any]] = []
    for workflow in workflows:
        for job in workflow.jobs:
            if job.has_name or job.job_id != base or job.combos is None:
                continue
            keys = job.matrix_keys
            combos = [[value for _key, value in combo] for combo in job.combos]
            exact = any(combo == legs for combo in combos)
            present: list[bool] = []
            for index, leg in enumerate(legs):
                if index < len(keys):
                    values = {combo[index] for combo in combos if index < len(combo)}
                else:
                    values = set()
                present.append(leg in values)
            candidates.append(
                {
                    "workflow": workflow.path,
                    "job_id": job.job_id,
                    "matrix_keys": list(keys),
                    "matrix_combos": combos,
                    "legs": legs,
                    "legs_present": present,
                    "exact_combination": exact,
                }
            )
    if not candidates:
        return None
    return {"context": context, "candidates": candidates}


def audit_repo(
    repo: str,
    default_branch: str,
    rulesets: Sequence[Mapping[str, Any]],
    workflows: Sequence[WorkflowDerivation],
    source_commit: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compare one repository's required contexts with its produced strings."""
    required: list[str] = []
    ruleset_detail: list[dict[str, Any]] = []
    for ruleset in rulesets:
        contexts = required_contexts_from_ruleset(ruleset)
        ruleset_detail.append(
            {
                "id": ruleset.get("id"),
                "name": ruleset.get("name"),
                "required_contexts": contexts,
            }
        )
        required.extend(contexts)
    required = sorted(set(required))

    produced = _all_displays(workflows)
    by_display: dict[str, list[dict[str, Any]]] = {}
    for entry in produced:
        by_display.setdefault(entry["display"], []).append(entry)

    matches: list[dict[str, Any]] = []
    missing: list[str] = []
    findings: list[dict[str, Any]] = []
    unexpanded = _unexpanded_jobs(workflows)

    for context in required:
        producers = by_display.get(context, [])
        if not producers:
            missing.append(context)
            if unexpanded:
                findings.append(
                    {
                        "severity": "unavailable",
                        "repo": repo,
                        "code": "context_unmeasured",
                        "context": context,
                        "detail": (
                            f"required context {context!r} was not among the derived displays, "
                            "and at least one job could not be expanded; this is unavailable, "
                            "not a mismatch"
                        ),
                    }
                )
            else:
                findings.append(
                    {
                        "severity": "high",
                        "repo": repo,
                        "code": "context_missing",
                        "context": context,
                        "detail": (
                            f"no job on origin/{default_branch} produces required context "
                            f"{context!r}"
                        ),
                    }
                )
            continue

        for producer in producers:
            entry = {
                "context": context,
                "workflow": producer["workflow"],
                "job_id": producer["job_id"],
                "mechanism": producer["mechanism"],
                "job_name": producer["job_name"],
                "matrix_values": producer["matrix_values"],
            }
            if producer["mechanism"] in {"job_id", "matrix-job-id"}:
                entry["derived_from_job_id"] = True
            if producer["mechanism"] == "name" and producer["job_name"] == producer["job_id"]:
                entry["name_equals_job_id"] = True
                findings.append(
                    {
                        "severity": "medium",
                        "repo": repo,
                        "code": "name_equals_job_id",
                        "context": context,
                        "detail": (
                            f"job {producer['job_id']!r} carries name: {producer['job_name']!r}; "
                            "the context string is unchanged but a job that derived its context "
                            "from its job id must keep no name:"
                        ),
                    }
                )
            matches.append(entry)

        # GEN24 is the recorded case: its producing job had no name and must not
        # gain one.  A name equal to the job id produces the same string, so this
        # invariant cannot be enforced by string comparison alone.
        if repo == "GEN24" and context == "tests":
            unnamed = [p for p in producers if p["mechanism"] == "job_id"]
            if not unnamed:
                findings.append(
                    {
                        "severity": "high",
                        "repo": repo,
                        "code": "gen24_job_gained_name",
                        "context": context,
                        "detail": (
                            "GEN24's 'tests' context must come from a job with no name:, "
                            f"but the producer reports mechanism {producers[0]['mechanism']!r} "
                            f"and name {producers[0]['job_name']!r}"
                        ),
                    }
                )

    leg_reports: list[dict[str, Any]] = []
    for context in required:
        leg = matrix_leg_check(context, workflows)
        if leg is None:
            continue
        leg_reports.append(leg)
        producers = by_display.get(context, [])
        matrix_producers = [
            producer
            for producer in producers
            if producer["mechanism"] in {"matrix-job-id", "matrix-name"}
        ]
        # A literal job may legitimately produce a matrix-shaped name.  The leg
        # rule applies only when the context is missing or a matrix job produces
        # it; otherwise an unrelated job with the same base id would raise a
        # false positive.
        if producers and not matrix_producers:
            continue
        for candidate in leg["candidates"]:
            if candidate["exact_combination"]:
                continue
            findings.append(
                {
                    "severity": "high",
                    "repo": repo,
                    "code": "matrix_leg_missing",
                    "context": context,
                    "detail": (
                        f"job {candidate['job_id']!r} has matrix keys "
                        f"{candidate['matrix_keys']} and combos "
                        f"{candidate['matrix_combos']}, which cannot produce the legs "
                        f"{candidate['legs']!r} named in required context {context!r}"
                    ),
                }
            )

    report = {
        "default_branch": default_branch,
        "source_commit": source_commit,
        "rulesets": ruleset_detail,
        "required_contexts": required,
        "produced_contexts": sorted(by_display),
        "matches": matches,
        "missing_contexts": missing,
        "matrix_legs": leg_reports,
        "unexpanded_jobs": unexpanded,
        "workflows_read": sorted(workflow.path for workflow in workflows),
    }
    return report, findings


# ---------------------------------------------------------------------------
# Capture parsing and comparison
# ---------------------------------------------------------------------------


def _section(text: str, heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        return ""
    start += len(heading)
    end = text.find("\n## ", start)
    return text[start:] if end < 0 else text[start:end]


def _backticks(line: str) -> list[str]:
    return re.findall(r"`([^`]+)`", line)


def _declared_int(section: str, label: str) -> int | None:
    match = re.search(rf"{re.escape(label)}\s*:\s*\**\s*(\d+)", section)
    return int(match.group(1)) if match else None


def parse_capture(text: str) -> CaptureBaseline:
    """Parse ``library/required-contexts.md`` as a claim to be tested."""
    table = _section(text, "## Contexts and their repositories")
    repo_contexts: dict[str, set[str]] = {}
    for line in table.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        context_names = _backticks(cells[0])
        if len(context_names) != 1:
            continue
        context = context_names[0]
        for repo in _backticks(cells[2]):
            repo_contexts.setdefault(repo, set()).add(context)

    historical_section = _section(text, "## Known unsatisfiable contexts")
    historical: list[dict[str, str]] = []
    for line in historical_section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        names = _backticks(cells[0])
        required = _backticks(cells[1])
        emitted = _backticks(cells[2])
        if names and required and emitted:
            historical.append(
                {
                    "repo": names[0],
                    "required": required[0],
                    "claimed_emitted": emitted[0],
                }
            )

    return CaptureBaseline(
        declared_repos=_declared_int(_section(text, "## Summary"), "Repos with a required context"),
        declared_distinct=_declared_int(_section(text, "## Summary"), "Distinct context strings"),
        repo_contexts={repo: tuple(sorted(contexts)) for repo, contexts in repo_contexts.items()},
        historical=tuple(historical),
    )


def compare_capture(
    capture: CaptureBaseline,
    live: Mapping[str, Sequence[str]],
    repo_reports: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compare a capture against live rulesets and the observed emitted strings."""
    differences: list[dict[str, Any]] = []
    for repo in sorted(set(capture.repo_contexts) | set(live)):
        captured = set(capture.repo_contexts.get(repo, ()))
        current = set(live.get(repo, ()))
        if captured != current:
            differences.append(
                {
                    "repo": repo,
                    "capture": sorted(captured),
                    "live": sorted(current),
                    "capture_only": sorted(captured - current),
                    "live_only": sorted(current - captured),
                }
            )

    historical: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for claim in capture.historical:
        repo = claim["repo"]
        report = repo_reports.get(repo, {})
        produced = set(report.get("produced_contexts", ()))
        current_required = (
            claim["required"] if claim["required"] in report.get("required_contexts", ()) else None
        )
        current_emitted = claim["required"] if claim["required"] in produced else None
        resolved = current_required is not None and current_emitted == current_required
        historical.append(
            {
                "repo": repo,
                "captured_required": claim["required"],
                "captured_emitted": claim["claimed_emitted"],
                "live_required": current_required,
                "current_emitted": current_emitted,
                "capture_was_stale": resolved and claim["claimed_emitted"] != current_emitted,
                "resolved": resolved,
            }
        )
        if not resolved:
            findings.append(
                {
                    "severity": "high",
                    "repo": repo,
                    "code": "capture_historical_claim_still_true",
                    "detail": (
                        f"the capture recorded {claim['claimed_emitted']!r} while the ruleset "
                        f"requires {claim['required']!r}; live derivation still disagrees"
                    ),
                }
            )

    comparison = {
        "declared_repos": capture.declared_repos,
        "declared_distinct": capture.declared_distinct,
        "parsed_repos": len(capture.repo_contexts),
        "parsed_distinct": len(
            {context for contexts in capture.repo_contexts.values() for context in contexts}
        ),
        "matches_live": not differences,
        "differences": differences,
        "historical": historical,
    }
    if differences:
        findings.append(
            {
                "severity": "high",
                "repo": None,
                "code": "capture_table_mismatch",
                "detail": (
                    f"the captured context table disagrees with live rulesets in "
                    f"{len(differences)} repositories"
                ),
            }
        )
    return comparison, findings


# ---------------------------------------------------------------------------
# Live collection
# ---------------------------------------------------------------------------


def _run(command: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )


def _stderr(proc: subprocess.CompletedProcess[str]) -> str:
    message = (proc.stderr or proc.stdout or "").strip().splitlines()
    return message[-1] if message else f"exit {proc.returncode}"


def gh_json(path: str, timeout: int = API_TIMEOUT) -> Any:
    proc = _run(["gh", "api", path], timeout)
    if proc.returncode != 0:
        raise AuditError(f"gh api {path} failed: {_stderr(proc)}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AuditError(f"gh api {path} returned invalid JSON: {exc}") from exc


def check_identity(expected: str, timeout: int = API_TIMEOUT) -> str:
    proc = _run(["gh", "api", "user", "--jq", ".login"], timeout)
    if proc.returncode != 0:
        raise AuditError(f"gh api user failed: {_stderr(proc)}")
    login = proc.stdout.strip()
    if login != expected:
        raise AuditError(
            f"gh authenticates as {login!r}, expected {expected!r}; "
            'export GH_TOKEN="$(gh auth token --user ActInfInstitute)"'
        )
    return login


def required_contexts_from_ruleset(detail: Mapping[str, Any]) -> list[str]:
    contexts: list[str] = []
    rules = detail.get("rules")
    if not isinstance(rules, list):
        return contexts
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            continue
        checks = parameters.get("required_status_checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if isinstance(check, dict) and isinstance(check.get("context"), str):
                contexts.append(check["context"])
    return sorted(set(contexts))


def fetch_rulesets(slug: str, branch: str) -> list[dict[str, Any]]:
    branch_name = quote(branch, safe="")
    rules = gh_json(f"repos/{slug}/rules/branches/{branch_name}?per_page=100")
    if not isinstance(rules, list) or len(rules) >= 100:
        raise AuditError(f"effective rules for {slug}:{branch} are incomplete")

    effective: dict[int, list[dict[str, Any]]] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            raise AuditError(f"invalid effective rule for {slug}:{branch}")
        if rule.get("type") != "required_status_checks":
            continue
        ruleset_id = rule.get("ruleset_id")
        if not isinstance(ruleset_id, int):
            raise AuditError(f"required status checks for {slug}:{branch} lack a ruleset ID")
        effective.setdefault(ruleset_id, []).append(rule)
    return [{"id": ruleset_id, "rules": effective[ruleset_id]} for ruleset_id in sorted(effective)]


def git_text(
    repo_path: Path, *args: str, timeout: int = GIT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repo_path), *args], timeout)


def read_default_workflows(
    repo_path: Path,
    default_branch: str,
    *,
    fetch: bool,
    timeout: int = GIT_TIMEOUT,
) -> tuple[dict[str, str], str, str | None]:
    """Read workflow files from ``origin/<default>``; never from a working tree."""
    local = git_text(repo_path, "rev-parse", "--verify", "-q", f"origin/{default_branch}")
    if local.returncode != 0:
        return {}, "", f"origin/{default_branch} is not available locally"
    local_sha = local.stdout.strip()

    remote = git_text(
        repo_path, "ls-remote", "origin", f"refs/heads/{default_branch}", timeout=timeout
    )
    if remote.returncode != 0:
        return {}, local_sha, f"git ls-remote failed: {_stderr(remote)}"
    fields = remote.stdout.split()
    remote_sha = fields[0] if fields else ""

    if remote_sha and remote_sha != local_sha:
        if not fetch:
            return (
                {},
                local_sha,
                (
                    f"origin/{default_branch} is stale ({local_sha[:12]} != {remote_sha[:12]}); "
                    "re-run with --fetch inside a repository lease"
                ),
            )
        fetched = git_text(
            repo_path, "fetch", "--no-tags", "origin", default_branch, timeout=FETCH_TIMEOUT
        )
        if fetched.returncode != 0:
            return {}, local_sha, f"git fetch failed: {_stderr(fetched)}"
        local = git_text(repo_path, "rev-parse", "--verify", "-q", f"origin/{default_branch}")
        local_sha = local.stdout.strip()
        if remote_sha != local_sha:
            return (
                {},
                local_sha,
                (
                    f"origin/{default_branch} is still stale after fetch "
                    f"({local_sha[:12]} != {remote_sha[:12]})"
                ),
            )

    listing = git_text(
        repo_path, "ls-tree", "-r", "--name-only", f"origin/{default_branch}", "--", WORKFLOW_DIR
    )
    if listing.returncode != 0:
        return {}, local_sha, f"git ls-tree failed: {_stderr(listing)}"

    contents: dict[str, str] = {}
    for path in sorted(line.strip() for line in listing.stdout.splitlines() if line.strip()):
        if not path.endswith(WORKFLOW_SUFFIXES):
            continue
        shown = git_text(repo_path, "show", f"origin/{default_branch}:{path}")
        if shown.returncode != 0:
            return {}, local_sha, f"git show {path} failed: {_stderr(shown)}"
        contents[path] = shown.stdout
    return contents, local_sha, None


def load_scope(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fieldnames = set(reader.fieldnames or ())
            if not {"repo", "visibility", "rel"} <= fieldnames:
                raise AuditError(
                    "scope table must have tab-separated repo, visibility and rel columns"
                )
            return [row for row in reader if row.get("repo")]
    except OSError as exc:
        raise AuditError(f"cannot read scope table {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def build_report(
    scope_path: Path,
    root: Path,
    *,
    capture_path: Path | None,
    expect_repos: int,
    expect_distinct: int,
    fetch: bool,
    org: str = ORG,
    git_reader: Callable[..., tuple[dict[str, str], str, str | None]] = read_default_workflows,
) -> dict[str, Any]:
    """Run the whole audit and return its deterministic report."""
    rows = load_scope(scope_path)
    public = sorted(
        (row for row in rows if row["visibility"].upper() == "PUBLIC"),
        key=lambda row: row["repo"],
    )
    repo_reports: dict[str, Any] = {}
    workflows_by_repo: dict[str, tuple[WorkflowDerivation, ...]] = {}
    findings: list[dict[str, Any]] = []
    without_required: list[str] = []
    unmeasured: list[str] = []
    unknown_contexts: list[str] = []

    for row in public:
        repo = row["repo"]
        slug = f"{org}/{repo}"
        try:
            info = gh_json(f"repos/{slug}", timeout=API_TIMEOUT)
            if not isinstance(info, dict) or not isinstance(info.get("default_branch"), str):
                raise AuditError(f"repos/{slug} has no default_branch")
            branch = info["default_branch"]
            rulesets = fetch_rulesets(slug, branch)
        except AuditError as exc:
            findings.append(
                {
                    "severity": "unavailable",
                    "repo": repo,
                    "code": "rulesets_unavailable",
                    "detail": str(exc),
                }
            )
            unmeasured.append(repo)
            unknown_contexts.append(repo)
            repo_reports[repo] = {"unavailable": str(exc), "required_contexts": []}
            continue

        required = sorted(
            {context for ruleset in rulesets for context in required_contexts_from_ruleset(ruleset)}
        )
        if not required:
            without_required.append(repo)
            continue

        repo_path = root / row["rel"]
        if not repo_path.is_dir():
            detail = f"working copy not found at {row['rel']}"
            findings.append(
                {
                    "severity": "unavailable",
                    "repo": repo,
                    "code": "working_copy_missing",
                    "detail": detail,
                }
            )
            unmeasured.append(repo)
            repo_reports[repo] = {
                "unavailable": detail,
                "default_branch": branch,
                "required_contexts": required,
            }
            continue

        contents, source_commit, error = git_reader(
            repo_path, branch, fetch=fetch, timeout=GIT_TIMEOUT
        )
        if error is not None:
            findings.append(
                {
                    "severity": "unavailable",
                    "repo": repo,
                    "code": "default_branch_unreadable",
                    "detail": error,
                }
            )
            unmeasured.append(repo)
            repo_reports[repo] = {
                "unavailable": error,
                "default_branch": branch,
                "required_contexts": required,
            }
            continue

        workflows = derive_repo_workflows(contents)
        workflows_by_repo[repo] = workflows
        repo_report, repo_findings = audit_repo(repo, branch, rulesets, workflows, source_commit)
        repo_reports[repo] = repo_report
        findings.extend(repo_findings)

    live = {repo: entry.get("required_contexts", ()) for repo, entry in repo_reports.items()}
    distinct = {context for contexts in live.values() for context in contexts}
    known_repos = [repo for repo, contexts in live.items() if contexts]

    if len(known_repos) != expect_repos and not unknown_contexts:
        findings.append(
            {
                "severity": "high",
                "repo": None,
                "code": "contract_repo_count",
                "detail": (
                    f"expected {expect_repos} public repositories with a required context, "
                    f"found {len(known_repos)}"
                ),
            }
        )
    if len(distinct) != expect_distinct and not unknown_contexts:
        findings.append(
            {
                "severity": "high",
                "repo": None,
                "code": "contract_distinct_count",
                "detail": (
                    f"expected {expect_distinct} distinct required contexts, found {len(distinct)}"
                ),
            }
        )

    comparison: dict[str, Any]
    if capture_path is not None:
        try:
            capture_text = capture_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AuditError(f"cannot read capture {capture_path}: {exc}") from exc
        capture = parse_capture(capture_text)
        comparison, capture_findings = compare_capture(capture, live, repo_reports)
        comparison["sha256"] = _sha256(capture_path)
        comparison["file"] = capture_path.name
        findings.extend(capture_findings)
    else:
        comparison = {"skipped": True, "reason": "--capture not supplied"}

    matrix_contract: list[dict[str, Any]] = []
    for context in CONTRACT_MATRIX_CONTEXTS:
        required_by = sorted(
            repo
            for repo, entry in repo_reports.items()
            if context in entry.get("required_contexts", ())
        )
        producers = [
            match
            for repo in required_by
            for match in repo_reports[repo].get("matches", ())
            if match["context"] == context
        ]
        legs = matrix_leg_check(
            context,
            [workflow for repo in required_by for workflow in workflows_by_repo.get(repo, ())],
        )
        entry: dict[str, Any] = {
            "context": context,
            "required_by": required_by,
            "producers": producers,
            "matrix_legs": legs,
        }
        if not producers:
            entry["matrix_produced"] = None
            entry["legs_present"] = None
            entry["note"] = "no producing job was found"
        elif producers[0]["mechanism"] in {"matrix-job-id", "matrix-name"}:
            entry["matrix_produced"] = True
            entry["legs_present"] = bool(
                legs and any(candidate["exact_combination"] for candidate in legs["candidates"])
            )
        else:
            entry["matrix_produced"] = False
            entry["legs_present"] = None
            entry["note"] = (
                "produced by a literal job name; the adopter declares no matrix, so the "
                "named leg is part of the name rather than a matrix value"
            )
        matrix_contract.append(entry)

    high = [finding for finding in findings if finding["severity"] == "high"]
    unavailable = [finding for finding in findings if finding["severity"] == "unavailable"]
    measured = [entry for repo, entry in repo_reports.items() if repo not in unmeasured]
    matched = sum(
        1
        for entry in measured
        for context in entry.get("required_contexts", ())
        if any(match["context"] == context for match in entry.get("matches", ()))
    )
    required_total = sum(len(entry.get("required_contexts", ())) for entry in repo_reports.values())
    unmeasured_total = sum(
        len(repo_reports[repo].get("required_contexts", ()))
        for repo in unmeasured
        if repo_reports[repo].get("required_contexts")
    )

    if high:
        result = "fail"
    elif unavailable:
        result = "unavailable"
    else:
        result = "pass"

    return {
        "schema": REPORT_SCHEMA,
        "org": org,
        "scope": {
            "file": scope_path.name,
            "sha256": _sha256(scope_path),
            "public_repos": len(public),
        },
        "contract": {
            "expected_repos": expect_repos,
            "expected_distinct": expect_distinct,
            "observed_repos": len(known_repos),
            "observed_distinct": len(distinct),
            "repos_without_required_context": without_required,
            "unmeasured_repos": unmeasured,
        },
        "repos": repo_reports,
        "capture": comparison,
        "matrix_contract": matrix_contract,
        "findings": sorted(
            findings,
            key=lambda item: (
                item.get("severity", ""),
                item.get("repo") or "",
                item.get("code", ""),
                item.get("context") or "",
            ),
        ),
        "summary": {
            "repos_with_required_context": len(known_repos),
            "contexts_required": required_total,
            "contexts_matched": matched,
            "contexts_missing": required_total - matched - unmeasured_total,
            "high_severity": len(high),
            "unavailable": len(unavailable),
            "result": result,
        },
    }


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def report_exit_code(report: Mapping[str, Any]) -> int:
    """Honest exit status: 1 for a definite disagreement, 3 for unmeasured."""
    summary = report["summary"]
    if summary["result"] == "fail":
        return 1
    if summary["result"] == "unavailable":
        return 3
    return 0


def render_human(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(
        f"required-context audit: {report['summary']['repos_with_required_context']} repos, "
        f"{report['summary']['contexts_required']} contexts, "
        f"{report['summary']['contexts_matched']} matched"
    )
    for repo in sorted(report["repos"]):
        entry = report["repos"][repo]
        if "unavailable" in entry:
            lines.append(f"  {repo}: UNAVAILABLE ({entry['unavailable']})")
            continue
        for match in entry.get("matches", ()):
            lines.append(
                f"  {repo}: {match['context']} <- {match['workflow']}:{match['job_id']} "
                f"[{match['mechanism']}]"
            )
        for context in entry.get("missing_contexts", ()):
            lines.append(f"  {repo}: MISSING {context}")
    for entry in report.get("matrix_contract", ()):
        lines.append(
            f"  matrix {entry['context']}: matrix_produced={entry.get('matrix_produced')} "
            f"legs_present={entry.get('legs_present')}"
            + (f" ({entry['note']})" if entry.get("note") else "")
        )
    capture = report.get("capture", {})
    if capture.get("skipped"):
        lines.append("  capture: skipped")
    else:
        lines.append(
            f"  capture: parsed {capture['parsed_repos']} repos / "
            f"{capture['parsed_distinct']} contexts; matches_live={capture['matches_live']}; "
            f"differences={len(capture['differences'])}"
        )
        for claim in capture.get("historical", ()):
            lines.append(
                f"  capture history {claim['repo']}: claimed {claim['captured_emitted']!r}, "
                f"current {claim['current_emitted']!r}, resolved={claim['resolved']}"
            )
    for finding in report.get("findings", ()):
        location = finding.get("repo") or "(fleet)"
        context = f" {finding['context']!r}" if finding.get("context") else ""
        lines.append(
            f"  {finding['severity'].upper():<11} {location}: {finding['code']}{context} - "
            f"{finding['detail']}"
        )
    summary = report["summary"]
    lines.append(
        f"result: {summary['result']} ({summary['contexts_matched']}/"
        f"{summary['contexts_required']} matched, {summary['high_severity']} high, "
        f"{summary['unavailable']} unavailable)"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_required_contexts.py",
        description=(
            "Derive every public repository's required status-check contexts from "
            "origin/<default> workflow YAML and compare them character for character."
        ),
    )
    parser.add_argument("--scope", required=True, help="scope TSV (repo/visibility/rel columns)")
    parser.add_argument("--root", required=True, help="root that scope rel paths resolve against")
    parser.add_argument("--capture", help="captured required-contexts markdown to prove")
    parser.add_argument("--json", action="store_true", help="emit the deterministic JSON report")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="fetch a stale origin/<default> ref (run inside a repository lease)",
    )
    parser.add_argument("--org", default=ORG, help=f"GitHub organization (default: {ORG})")
    parser.add_argument(
        "--expect-login",
        default=DEFAULT_EXPECT_LOGIN,
        help=f"required gh login (default: {DEFAULT_EXPECT_LOGIN})",
    )
    parser.add_argument(
        "--expect-repos",
        type=int,
        default=DEFAULT_EXPECT_REPOS,
        help=f"expected public repos with a required context (default: {DEFAULT_EXPECT_REPOS})",
    )
    parser.add_argument(
        "--expect-distinct",
        type=int,
        default=DEFAULT_EXPECT_DISTINCT,
        help=f"expected distinct required contexts (default: {DEFAULT_EXPECT_DISTINCT})",
    )
    args = parser.parse_args(argv)

    try:
        check_identity(args.expect_login)
        report = build_report(
            Path(args.scope),
            Path(args.root),
            capture_path=Path(args.capture) if args.capture else None,
            expect_repos=args.expect_repos,
            expect_distinct=args.expect_distinct,
            fetch=args.fetch,
            org=args.org,
        )
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"error: command timed out: {exc}", file=sys.stderr)
        return 3

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_human(report) + "\n")
    return report_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
