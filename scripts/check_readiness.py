#!/usr/bin/env python3
"""Deterministic readiness checker for ActiveInferenceInstitute repositories.

The criterion catalogue lives in ``standards/tiers.yaml``; this program only knows
the five evaluation kinds:

``file-exists``   a path (or glob) is present in the tracked tree
``file-contains`` a path is present and a regular expression matches
``yaml-path``     a dotted path resolves in a YAML document
``command``       a bounded local command exits zero
``github-api``    an opt-in REST read (only with ``--github``)

Statuses are exactly ``pass``, ``fail``, ``waived``, ``unavailable`` and
``not-applicable``.  ``unavailable`` is never rounded up to ``pass``.

Exit codes (``--check`` only; without it the run is a report):
    0  every blocking criterion passed (or was waived / not applicable)
    1  a blocking criterion failed
    2  usage or configuration error (bad arguments, bad catalogue, bad manifest)
    3  a criterion could not be evaluated, so the gate cannot be certified

Determinism contract: two ``--json`` runs on one commit are byte-identical.
Output uses sorted keys, repository-relative paths, no wall-clock timestamps, no
run identifiers and no absolute vault paths.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import yaml

KNOWN_KINDS = frozenset({"file-exists", "file-contains", "yaml-path", "command", "github-api"})
STATUSES = ("pass", "fail", "waived", "unavailable", "not-applicable")
STANDARD_SCHEMA = "aii-readiness-standard/v1"
MANIFEST_SCHEMA = "aii-readiness/v1"
REPORT_SCHEMA = "aii-readiness-report/v1"
DEFAULT_ORG = "ActiveInferenceInstitute"
DEFAULT_TIMEOUT = 60
MAX_TEXT_BYTES = 2_000_000
WORKFLOW_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml")

IGNORED_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".lake",
        ".elan",
        "lake-packages",
        ".next",
        ".parcel-cache",
    }
)


class ConfigError(Exception):
    """A usage, catalogue or manifest error (exit code 2)."""


@dataclass(frozen=True)
class Exclusion:
    """A criterion a tier cannot satisfy by construction, with the reason why."""

    criterion: str
    reason: str


@dataclass(frozen=True)
class Tier:
    name: str
    title: str
    includes: tuple[str, ...] = ()
    excludes: tuple[Exclusion, ...] = ()


@dataclass(frozen=True)
class Criterion:
    id: str
    title: str
    kind: str
    blocking: bool
    within: tuple[str, ...]
    spec: dict[str, Any]


@dataclass(frozen=True)
class Standard:
    schema: str
    version: str
    tiers: dict[str, Tier]
    criteria: tuple[Criterion, ...]
    criteria_by_id: dict[str, Criterion]

    def tier_closure(self, tier: str) -> frozenset[str]:
        if tier not in self.tiers:
            raise ConfigError(f"unknown tier: {tier!r}")
        closure: set[str] = set()
        stack = [tier]
        while stack:
            current = stack.pop()
            if current in closure:
                continue
            closure.add(current)
            stack.extend(self.tiers[current].includes)
        return frozenset(closure)

    def applicable(self, tier: str) -> tuple[Criterion, ...]:
        closure = self.tier_closure(tier)
        excluded = self.exclusions(tier)
        return tuple(
            criterion
            for criterion in self.criteria
            if criterion.id not in excluded
            and any(member in closure for member in criterion.within)
        )

    def exclusions(self, tier: str) -> dict[str, str]:
        """Criterion id to reason for every tier exclusion in ``tier``'s closure."""
        if tier not in self.tiers:
            raise ConfigError(f"unknown tier: {tier!r}")
        result: dict[str, str] = {}
        for member in sorted(self.tier_closure(tier)):
            for exclusion in self.tiers[member].excludes:
                result.setdefault(exclusion.criterion, exclusion.reason)
        return result


class GithubClient(Protocol):
    """Minimal status-only GitHub client used by the ``github-api`` kind."""

    def get(self, endpoint: str) -> int:
        """Return an HTTP status code, or 0 when the read was not possible."""


@dataclass
class GhCliClient:
    """GitHub REST reads through the pinned ``gh`` CLI.

    Only the HTTP status is retained, which keeps the JSON report deterministic and
    free of timestamps, run identifiers and payload drift.
    """

    timeout: int = 30

    def get(self, endpoint: str) -> int:
        if shutil.which("gh") is None:
            return 0
        try:
            completed = subprocess.run(
                ["gh", "api", "--include", endpoint],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            return 0
        for line in completed.stdout.splitlines():
            match = re.match(r"HTTP/\S+\s+(\d{3})", line)
            if match:
                return int(match.group(1))
        return 0


@dataclass
class EvalContext:
    repo_id: str
    repo_path: Path
    org: str
    github: bool
    github_client: GithubClient | None
    default_branch: str | None
    index: RepoIndex


@dataclass
class RunResult:
    status: str
    evidence: str


@dataclass
class RepoReport:
    repo_id: str
    tier: str
    tier_source: str
    commit: str | None
    default_branch: str | None
    criteria: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "tier_source": self.tier_source,
            "commit": self.commit,
            "default_branch": self.default_branch,
            "criteria": self.criteria,
            "summary": summarize(self.criteria),
        }


# ---------------------------------------------------------------------------
# Catalogue loading
# ---------------------------------------------------------------------------


def _as_mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{what} must be a mapping")
    return value


def _as_string_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{what} must be a string or a list of strings")
    return list(value)


def load_standard(path: Path) -> Standard:
    """Parse and validate ``standards/tiers.yaml``.

    An unknown evaluation kind is a hard error: skipping it would make the ledger
    look complete when it is not.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read standard {path}: {exc}") from exc
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"standard {path} is not valid YAML: {exc}") from exc
    document = _as_mapping(document, "standard")
    if document.get("schema") != STANDARD_SCHEMA:
        raise ConfigError(f"standard schema must be {STANDARD_SCHEMA!r}")

    tiers_raw = _as_mapping(document.get("tiers"), "standard.tiers")
    if not tiers_raw:
        raise ConfigError("standard.tiers must not be empty")
    tiers: dict[str, Tier] = {}
    for name, tier_raw in tiers_raw.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("tier names must be non-empty strings")
        tier_doc = _as_mapping(tier_raw, f"tier {name}")
        includes = tuple(_as_string_list(tier_doc.get("includes"), f"tier {name}.includes"))
        excludes_raw = tier_doc.get("excludes", []) or []
        if not isinstance(excludes_raw, list):
            raise ConfigError(f"tier {name}.excludes must be a list")
        excludes: list[Exclusion] = []
        for exclusion_index, exclusion_raw in enumerate(excludes_raw):
            where = f"tier {name}.excludes[{exclusion_index}]"
            exclusion_doc = _as_mapping(exclusion_raw, where)
            criterion_id = exclusion_doc.get("criterion")
            if not isinstance(criterion_id, str) or not criterion_id:
                raise ConfigError(f"{where} must name a criterion")
            reason = exclusion_doc.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ConfigError(
                    f"tier {name} exclusion for {criterion_id} needs a non-empty reason"
                )
            excludes.append(Exclusion(criterion=criterion_id, reason=" ".join(reason.split())))
        tiers[name] = Tier(
            name=name,
            title=str(tier_doc.get("title", name)),
            includes=includes,
            excludes=tuple(excludes),
        )
    for tier in tiers.values():
        for included in tier.includes:
            if included not in tiers:
                raise ConfigError(f"tier {tier.name!r} includes unknown tier {included!r}")

    criteria_raw = document.get("criteria")
    if not isinstance(criteria_raw, list) or not criteria_raw:
        raise ConfigError("standard.criteria must be a non-empty list")
    criteria: list[Criterion] = []
    seen: set[str] = set()
    for index, item_raw in enumerate(criteria_raw):
        item = _as_mapping(item_raw, f"criteria[{index}]")
        criterion_id = item.get("id")
        if not isinstance(criterion_id, str) or not criterion_id:
            raise ConfigError(f"criteria[{index}] has no id")
        if criterion_id in seen:
            raise ConfigError(f"duplicate criterion id: {criterion_id}")
        seen.add(criterion_id)
        kind = item.get("kind")
        if kind not in KNOWN_KINDS:
            raise ConfigError(
                f"unknown criterion kind {kind!r} for {criterion_id}; "
                f"known kinds: {', '.join(sorted(KNOWN_KINDS))}"
            )
        blocking = item.get("blocking")
        if not isinstance(blocking, bool):
            raise ConfigError(f"criterion {criterion_id} must declare a boolean blocking flag")
        within = _as_string_list(item.get("within"), f"criterion {criterion_id}.within")
        if not within:
            raise ConfigError(f"criterion {criterion_id} must name at least one tier in within")
        for tier_name in within:
            if tier_name not in tiers:
                raise ConfigError(f"criterion {criterion_id} names unknown tier {tier_name!r}")
        spec = _as_mapping(item.get("spec", {}), f"criterion {criterion_id}.spec")
        _validate_spec(criterion_id, kind, spec)
        criteria.append(
            Criterion(
                id=criterion_id,
                title=str(item.get("title", criterion_id)),
                kind=kind,
                blocking=blocking,
                within=tuple(within),
                spec=spec,
            )
        )
    criteria.sort(key=lambda criterion: criterion.id)
    standard = Standard(
        schema=str(document["schema"]),
        version=str(document.get("version", "unversioned")),
        tiers=tiers,
        criteria=tuple(criteria),
        criteria_by_id={criterion.id: criterion for criterion in criteria},
    )
    for tier in standard.tiers.values():
        closure = standard.tier_closure(tier.name)
        for exclusion in tier.excludes:
            criterion = standard.criteria_by_id.get(exclusion.criterion)
            if criterion is None:
                raise ConfigError(
                    f"tier {tier.name!r} excludes unknown criterion {exclusion.criterion!r}"
                )
            if not any(member in closure for member in criterion.within):
                raise ConfigError(
                    f"tier {tier.name!r} excludes {exclusion.criterion!r}, "
                    "which does not apply to it"
                )
    return standard


def _validate_spec(criterion_id: str, kind: str, spec: dict[str, Any]) -> None:
    if kind == "file-exists":
        if not any(key in spec for key in ("path", "paths", "declared_in")):
            raise ConfigError(f"{criterion_id}: file-exists needs path, paths or declared_in")
    elif kind == "file-contains":
        if not any(key in spec for key in ("path", "paths")):
            raise ConfigError(f"{criterion_id}: file-contains needs path or paths")
        if not any(
            key in spec
            for key in (
                "regex",
                "all_of",
                "any_of",
                "forbid",
                "floor_in_ci",
                "coverage_threshold_match",
                "cname_match",
                "node_pin",
            )
        ):
            raise ConfigError(f"{criterion_id}: file-contains needs a match rule")
    elif kind == "yaml-path":
        if not any(key in spec for key in ("file", "files")):
            raise ConfigError(f"{criterion_id}: yaml-path needs file or files")
        if not any(key in spec for key in ("require", "expect", "ecosystems")):
            raise ConfigError(f"{criterion_id}: yaml-path needs require, expect or ecosystems")
        if "ecosystems" in spec:
            _lockfile_groups(criterion_id, spec)
    elif kind == "command":
        if "command" not in spec:
            raise ConfigError(f"{criterion_id}: command needs a command")
        if not isinstance(spec["command"], (str, list)):
            raise ConfigError(f"{criterion_id}: command must be a string or a list")
    elif kind == "github-api" and "endpoint" not in spec and "endpoints" not in spec:
        raise ConfigError(f"{criterion_id}: github-api needs endpoint or endpoints")


# ---------------------------------------------------------------------------
# Manifest and waivers
# ---------------------------------------------------------------------------


def load_manifest(repo_path: Path) -> dict[str, Any]:
    manifest_path = repo_path / ".aii" / "readiness.json"
    if not manifest_path.is_file():
        return {}
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid .aii/readiness.json: {exc}") from exc
    document = _as_mapping(document, ".aii/readiness.json")
    if "schema" in document and document["schema"] != MANIFEST_SCHEMA:
        raise ConfigError(f".aii/readiness.json schema must be {MANIFEST_SCHEMA!r}")
    return document


def parse_waivers(standard: Standard, manifest: dict[str, Any], today: date) -> dict[str, str]:
    """Return active waivers as ``{criterion_id: reason}``.

    A waiver without a non-empty reason is an error, never a pass.  A waiver whose
    ``expires`` date has passed is ignored, so the criterion is evaluated normally.
    """
    raw_waivers = manifest.get("waivers", [])
    if raw_waivers is None:
        return {}
    if not isinstance(raw_waivers, list):
        raise ConfigError(".aii/readiness.json waivers must be a list")
    waivers: dict[str, str] = {}
    for item_raw in raw_waivers:
        item = _as_mapping(item_raw, "waiver")
        criterion_id = item.get("criterion")
        if not isinstance(criterion_id, str) or not criterion_id:
            raise ConfigError("every waiver must name a criterion")
        if criterion_id not in standard.criteria_by_id:
            raise ConfigError(f"waiver names unknown criterion {criterion_id!r}")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ConfigError(f"waiver for {criterion_id} requires a non-empty reason")
        expires = item.get("expires")
        if expires is not None:
            try:
                expiry = date.fromisoformat(str(expires))
            except ValueError as exc:
                raise ConfigError(
                    f"waiver for {criterion_id} has an invalid expires date {expires!r}"
                ) from exc
            if today > expiry:
                continue
        if criterion_id in waivers:
            raise ConfigError(f"duplicate waiver for {criterion_id}")
        waivers[criterion_id] = reason.strip()
    return waivers


# ---------------------------------------------------------------------------
# Repository helpers
# ---------------------------------------------------------------------------


def _checked_relative(relative: str) -> PurePosixPath:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"standard path must stay inside the repository: {relative!r}")
    return path


def _patterns(spec: dict[str, Any]) -> list[str]:
    raw = spec.get("paths")
    if raw is None:
        raw = spec.get("path")
    patterns = _as_string_list(raw, "paths")
    for pattern in patterns:
        _checked_relative(pattern)
    return patterns


@dataclass
class RepoIndex:
    """Bounded, deterministic file index for one working copy.

    For a Git working copy the index is ``git ls-files``: tracked files only.  That
    matches the tier rules, excludes generated dependency trees, and keeps a scan of
    a multi-gigabyte checkout bounded.  A plain directory (used by tests) falls back
    to a pruned, symlink-free walk with a hard cap.
    """

    repo_path: Path
    files: tuple[str, ...]
    _cache: dict[tuple[str, ...], list[Path]] = field(default_factory=dict, repr=False)

    def expand(self, patterns: list[str]) -> list[Path]:
        key = tuple(patterns)
        cached = self._cache.get(key)
        if cached is not None:
            return list(cached)
        matches: set[Path] = set()
        for pattern in patterns:
            _checked_relative(pattern)
            for relative in self.files:
                if not PurePosixPath(relative).full_match(pattern):
                    continue
                candidate = self.repo_path / relative
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                matches.add(candidate)
        ordered = sorted(matches, key=lambda candidate: candidate.as_posix())
        self._cache[key] = ordered
        return list(ordered)


def build_repo_index(repo_path: Path) -> RepoIndex:
    toplevel = _git_output(repo_path, ["rev-parse", "--show-toplevel"])
    if toplevel is not None and Path(toplevel.strip()).resolve() == repo_path.resolve():
        listing = _git_output(repo_path, ["ls-files", "-z"])
        if listing is not None:
            files = tuple(sorted(entry for entry in listing.split("\0") if entry))
            return RepoIndex(repo_path=repo_path, files=files)
    collected: list[str] = []
    for root, directories, filenames in os.walk(repo_path):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in IGNORED_PARTS and not (Path(root) / directory).is_symlink()
        )
        for filename in sorted(filenames):
            candidate = Path(root) / filename
            if candidate.is_symlink():
                continue
            collected.append(candidate.relative_to(repo_path).as_posix())
            if len(collected) >= 200_000:
                return RepoIndex(repo_path=repo_path, files=tuple(sorted(collected)))
    return RepoIndex(repo_path=repo_path, files=tuple(sorted(collected)))


def _expand(context: EvalContext, patterns: list[str]) -> list[Path]:
    return context.index.expand(patterns)


def _relative(path: Path, repo_path: Path) -> str:
    try:
        return path.relative_to(repo_path).as_posix()
    except ValueError:
        return path.name


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _line_of(text: str, match: re.Match[str]) -> int:
    return text.count("\n", 0, match.start()) + 1


def _yaml_document(path: Path) -> Any:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"unparseable YAML in {path.name}: {exc}") from exc


def _normalize_yaml_keys(value: Any) -> Any:
    """PyYAML parses an unquoted ``on:`` key as the boolean ``True``."""
    if isinstance(value, dict):
        return {
            ("on" if key is True else key): _normalize_yaml_keys(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_normalize_yaml_keys(child) for child in value]
    return value


def _resolve_dotted(document: Any, dotted: str) -> list[Any]:
    values: list[Any] = [document]
    for segment in dotted.split("."):
        next_values: list[Any] = []
        for value in values:
            if isinstance(value, dict) and segment in value:
                next_values.append(value[segment])
            elif isinstance(value, list) and segment in value:
                next_values.append(segment)
        values = next_values
        if not values:
            return []
    return values


def path_satisfied(document: Any, dotted: str) -> bool:
    if not dotted:
        return False
    if "*" in dotted:
        prefix, _, rest = dotted.partition("*")
        rest = rest.lstrip(".")
        prefix = prefix.rstrip(".")
        parents = _resolve_dotted(document, prefix) if prefix else [document]
        children: list[Any] = []
        for parent in parents:
            if isinstance(parent, dict):
                children.extend(parent.values())
            elif isinstance(parent, list):
                children.extend(parent)
        if not children:
            return False
        return all(path_satisfied(child, rest) for child in children)
    return bool(_resolve_dotted(document, dotted))


def _empty_result(on_empty: str, detail: str) -> RunResult:
    if on_empty == "not-applicable":
        return RunResult("not-applicable", detail)
    if on_empty == "pass":
        return RunResult("pass", detail)
    if on_empty == "unavailable":
        return RunResult("unavailable", detail)
    return RunResult("fail", detail)


# ---------------------------------------------------------------------------
# The five evaluation kinds
# ---------------------------------------------------------------------------


def evaluate_criterion(criterion: Criterion, context: EvalContext) -> RunResult:
    if criterion.kind == "file-exists":
        return evaluate_file_exists(criterion, context)
    if criterion.kind == "file-contains":
        return evaluate_file_contains(criterion, context)
    if criterion.kind == "yaml-path":
        return evaluate_yaml_path(criterion, context)
    if criterion.kind == "command":
        return evaluate_command(criterion, context)
    if criterion.kind == "github-api":
        return evaluate_github_api(criterion, context)
    raise ConfigError(f"unknown criterion kind {criterion.kind!r}")


def evaluate_file_exists(criterion: Criterion, context: EvalContext) -> RunResult:
    spec = criterion.spec
    patterns = _patterns(spec)
    match_all = spec.get("match") == "all"
    non_empty = bool(spec.get("non_empty", False))
    parses = spec.get("parses")
    matched = _expand(context, patterns)

    if match_all:
        missing = [pattern for pattern in patterns if not _expand(context, [pattern])]
        if missing:
            return RunResult("fail", "missing: " + ", ".join(missing))
    if not matched:
        declared = _license_from_manifest(spec, context.repo_path)
        if declared is not None:
            return RunResult("pass", declared)
        if not patterns:
            return _empty_result(str(spec.get("on_empty", "fail")), "no paths declared")
        return RunResult("fail", "none of: " + ", ".join(patterns))

    if non_empty and match_all:
        # ``match: all`` promises every declared path; an empty one is absent
        # for its purpose, so it must fail rather than be filtered out of the
        # count that produces the pass.
        empty = [path for path in matched if path.stat().st_size == 0]
        if empty:
            return RunResult(
                "fail",
                "empty files: " + ", ".join(_relative(path, context.repo_path) for path in empty),
            )
    readable = [path for path in matched if not non_empty or path.stat().st_size > 0]
    if not readable:
        first = _relative(matched[0], context.repo_path)
        return RunResult("fail", f"matched files are empty: {first}")
    if parses == "yaml":
        for path in readable:
            try:
                _yaml_document(path)
            except ConfigError:
                return RunResult("fail", f"{_relative(path, context.repo_path)}: invalid YAML")
    elif parses == "json":
        for path in readable:
            text = _read_text(path)
            try:
                json.loads(text or "")
            except json.JSONDecodeError:
                return RunResult("fail", f"{_relative(path, context.repo_path)}: invalid JSON")
    if match_all:
        return RunResult("pass", f"{len(patterns)} paths present")
    return RunResult("pass", _relative(readable[0], context.repo_path))


def _license_from_manifest(spec: dict[str, Any], repo_path: Path) -> str | None:
    declared_in = _as_string_list(spec.get("declared_in"), "declared_in")
    for name in declared_in:
        path = repo_path / name
        if not path.is_file():
            continue
        if name.endswith(".json"):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except OSError, json.JSONDecodeError:
                continue
            if isinstance(document, dict) and document.get("license"):
                return f"{name}:license"
            continue
        text = _read_text(path) or ""
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            document = None
        if isinstance(document, dict):
            project = document.get("project")
            if isinstance(project, dict) and project.get("license"):
                return f"{name}:project.license"
            tool = document.get("tool")
            if isinstance(tool, dict):
                poetry = tool.get("poetry")
                if isinstance(poetry, dict) and poetry.get("license"):
                    return f"{name}:tool.poetry.license"
        if re.search(r"(?m)^\s*license\s*=", text):
            return f"{name}:license"
    return None


def _match_rule(spec: dict[str, Any], text: str) -> tuple[bool, str | None]:
    """Return ``(matched, detail)`` for one text document."""
    forbid = spec.get("forbid")
    if isinstance(forbid, str):
        forbidden = re.search(forbid, text, re.MULTILINE)
        if forbidden is not None:
            return False, f"forbidden pattern at line {_line_of(text, forbidden)}"
    required: list[str] = []
    if isinstance(spec.get("regex"), str):
        required.append(spec["regex"])
    required.extend(_as_string_list(spec.get("all_of"), "all_of"))
    first_line = 1
    for pattern in required:
        match = re.search(pattern, text, re.MULTILINE)
        if match is None:
            return False, f"missing {pattern!r}"
        first_line = _line_of(text, match)
    any_of = _as_string_list(spec.get("any_of"), "any_of")
    if any_of:
        for pattern in any_of:
            match = re.search(pattern, text, re.MULTILINE)
            if match is not None:
                return True, str(_line_of(text, match))
        return False, "none of the accepted patterns matched"
    if required:
        return True, str(first_line)
    return True, ""


def _file_contains_result(path: Path, spec: dict[str, Any]) -> RunResult:
    text = _read_text(path)
    if text is None:
        return RunResult("unavailable", "file could not be read or is too large")
    matched, detail = _match_rule(spec, text)
    if matched:
        return RunResult("pass", detail or "matched")
    return RunResult("fail", detail or "did not match")


def _file_contains_evidence(path: Path, result: RunResult, context: EvalContext) -> str:
    """Render the documented ``path:line`` evidence for a passing match."""
    if result.evidence.isdigit():
        detail = f":{result.evidence}"
    elif result.evidence:
        detail = f": {result.evidence}"
    else:
        detail = ": no forbidden match"
    return f"{_relative(path, context.repo_path)}{detail}"


def evaluate_file_contains(criterion: Criterion, context: EvalContext) -> RunResult:
    spec = criterion.spec
    if spec.get("floor_in_ci"):
        return _floor_in_ci(context)
    if spec.get("coverage_threshold_match"):
        return _coverage_threshold_match(context)
    if spec.get("cname_match"):
        return _cname_match(context)
    if spec.get("node_pin"):
        return _node_pin(context)

    patterns = _patterns(spec)
    files = _expand(context, patterns)
    if not files:
        return _empty_result(
            str(spec.get("on_empty", "fail")), "no files matched: " + ", ".join(patterns)
        )
    match_all = spec.get("match") == "all"
    results: list[tuple[Path, RunResult]] = [
        (path, _file_contains_result(path, spec)) for path in files
    ]
    if match_all:
        for path, result in results:
            if result.status != "pass":
                where = _relative(path, context.repo_path)
                return RunResult(result.status, f"{where}: {result.evidence}")
        first_path, first_result = results[0]
        return RunResult("pass", _file_contains_evidence(first_path, first_result, context))
    unavailable: list[str] = []
    for path, result in results:
        if result.status == "pass":
            return RunResult("pass", _file_contains_evidence(path, result, context))
        if result.status == "unavailable":
            unavailable.append(_relative(path, context.repo_path))
    if unavailable:
        return RunResult("unavailable", "unreadable: " + ", ".join(unavailable))
    path, result = results[0]
    return RunResult("fail", f"{_relative(path, context.repo_path)}: {result.evidence}")


def _read_ci_text(context: EvalContext) -> str:
    chunks: list[str] = []
    for path in _expand(context, list(WORKFLOW_GLOBS)):
        text = _read_text(path)
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _declared_python_floor(context: EvalContext) -> str | None:
    text = _read_text(context.repo_path / "pyproject.toml")
    if text is None:
        return None
    match = re.search(r"""requires-python\s*=\s*["']([^"']+)["']""", text)
    if match is None:
        return None
    floor = re.search(r"\d+\.\d+(?:\.\d+)?", match.group(1))
    return floor.group(0) if floor else None


def _floor_in_ci(context: EvalContext) -> RunResult:
    floor = _declared_python_floor(context)
    if floor is None:
        return RunResult("fail", "requires-python is not declared in pyproject.toml")
    ci_text = _read_ci_text(context)
    if not ci_text:
        return RunResult("fail", "no CI workflow declares the Python floor")
    if re.search(rf"(?<![\d.]){re.escape(floor)}(?![\d.])", ci_text):
        return RunResult("pass", f"pyproject.toml requires {floor}; CI exercises it")
    return RunResult("fail", f"pyproject.toml requires {floor}; no CI matrix leg uses it")


def _coverage_threshold_match(context: EvalContext) -> RunResult:
    pyproject = _read_text(context.repo_path / "pyproject.toml") or ""
    declared = re.search(r"(?m)^\s*fail_under\s*=\s*(\d+)", pyproject)
    ci_text = _read_ci_text(context)
    enforced = re.search(r"--cov-fail-under[=\s]+(\d+)", ci_text) or re.search(
        r"(?m)^\s*fail_under\s*=\s*(\d+)", ci_text
    )
    if declared is None and enforced is None:
        return RunResult("not-applicable", "no coverage threshold declared")
    if declared is None or enforced is None:
        return RunResult("not-applicable", "threshold declared in only one place")
    if declared.group(1) == enforced.group(1):
        return RunResult("pass", f"threshold {declared.group(1)} in both places")
    return RunResult(
        "fail",
        f"pyproject.toml says {declared.group(1)}; CI enforces {enforced.group(1)}",
    )


def _cname_match(context: EvalContext) -> RunResult:
    cname = _read_text(context.repo_path / "CNAME")
    if cname is None or not cname.strip():
        return RunResult("not-applicable", "no CNAME file")
    host = cname.strip().splitlines()[0].strip()
    ci_text = _read_ci_text(context)
    if host and host in ci_text:
        return RunResult("pass", f"CNAME {host} appears in CI")
    return RunResult("fail", f"CNAME {host} does not appear in any workflow")


def _node_pin(context: EvalContext) -> RunResult:
    nvmrc = _read_text(context.repo_path / ".nvmrc")
    pin = nvmrc.strip().splitlines()[0].strip() if nvmrc and nvmrc.strip() else None
    if pin is None:
        package_text = _read_text(context.repo_path / "package.json") or ""
        match = re.search(r'"node"\s*:\s*"([^"]+)"', package_text)
        pin = match.group(1) if match else None
    if pin is None:
        return RunResult("fail", "no Node version pin (.nvmrc or engines.node)")
    ci_text = _read_ci_text(context)
    if pin in ci_text:
        return RunResult("pass", f"Node pin {pin} appears in CI")
    return RunResult("fail", f"Node pin {pin} does not appear in any workflow")


def evaluate_yaml_path(criterion: Criterion, context: EvalContext) -> RunResult:
    spec = criterion.spec
    raw_files = spec.get("files")
    if raw_files is None:
        raw_files = spec.get("file")
    patterns = _as_string_list(raw_files, "files")
    files = _expand(context, patterns)
    if not files:
        return _empty_result(
            str(spec.get("on_empty", "not-applicable")),
            "no YAML documents matched: " + ", ".join(patterns),
        )
    match_all = spec.get("match", "all") == "all"
    results: list[tuple[Path, RunResult]] = []
    for path in files:
        text = _read_text(path)
        if text is None:
            results.append((path, RunResult("unavailable", "file could not be read")))
            continue
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            results.append((path, RunResult("unavailable", "document does not parse")))
            continue
        document = _normalize_yaml_keys(document)
        results.append((path, _requires(document, spec, context, criterion.id)))
    if match_all:
        for path, result in results:
            if result.status != "pass":
                return RunResult(
                    result.status, f"{_relative(path, context.repo_path)}: {result.evidence}"
                )
        first_path, first_result = results[0]
        return RunResult(
            "pass",
            f"{_relative(first_path, context.repo_path)}: {first_result.evidence}",
        )
    unavailable: list[str] = []
    for path, result in results:
        if result.status == "pass":
            return RunResult("pass", f"{_relative(path, context.repo_path)}: {result.evidence}")
        if result.status == "unavailable":
            unavailable.append(_relative(path, context.repo_path))
    if unavailable:
        return RunResult("unavailable", "unparseable: " + ", ".join(unavailable))
    path, result = results[0]
    return RunResult("fail", f"{_relative(path, context.repo_path)}: {result.evidence}")


def _lockfile_groups(criterion_id: str, spec: dict[str, Any]) -> list[list[str]]:
    """Resolve the lockfile precedence groups a criterion declares.

    Groups are ordered from most authoritative to most legacy. The first group
    holding any present lockfile is the repository's real ecosystem: every
    lockfile in it must be watched, and lower groups are ignored as legacy. When
    a spec declares no ``precedence``, all declared lockfiles form one group, so
    selection never depends on declaration or alphabetical order.
    """
    ecosystems = spec.get("ecosystems")
    if not isinstance(ecosystems, dict) or not ecosystems:
        raise ConfigError(f"{criterion_id}: ecosystems must be a non-empty mapping")
    for lockfile, ecosystem in ecosystems.items():
        if not isinstance(lockfile, str) or not lockfile:
            raise ConfigError(f"{criterion_id}: lockfile names must be non-empty strings")
        if not isinstance(ecosystem, str) or not ecosystem:
            raise ConfigError(
                f"{criterion_id}: ecosystem for {lockfile} must be a non-empty string"
            )
    raw_groups = spec.get("precedence")
    if raw_groups is None:
        return [sorted(ecosystems)]
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ConfigError(f"{criterion_id}: precedence must be a non-empty list of lockfile groups")
    groups: list[list[str]] = []
    seen: set[str] = set()
    for index, raw_group in enumerate(raw_groups):
        names = _as_string_list(raw_group, f"{criterion_id}: precedence[{index}]")
        if not names:
            raise ConfigError(
                f"{criterion_id}: precedence[{index}] must name at least one lockfile"
            )
        for name in names:
            if name not in ecosystems:
                raise ConfigError(f"{criterion_id}: precedence names unknown lockfile {name!r}")
            if name in seen:
                raise ConfigError(
                    f"{criterion_id}: lockfile {name!r} appears in more than one precedence group"
                )
            seen.add(name)
        groups.append(names)
    omitted = sorted(set(ecosystems) - seen)
    if omitted:
        raise ConfigError(
            f"{criterion_id}: precedence omits declared lockfile(s): " + ", ".join(omitted)
        )
    return groups


def _present_lockfiles(context: EvalContext, names: list[str]) -> list[str]:
    """Lockfiles that are both tracked and present, like every other kind.

    Presence is resolved from the repository index (``git ls-files`` for a Git
    working copy), not from a disk glance, so an ignored build artifact such as
    a gitignored ``uv.lock`` cannot be cited as a lockfile GitHub can see.
    """
    tracked = set(context.index.files)
    return [name for name in names if name in tracked and (context.repo_path / name).is_file()]


def _untracked_lockfiles(context: EvalContext, names: list[str]) -> list[str]:
    """Named lockfiles that exist on disk but are not tracked, for evidence only."""
    tracked = set(context.index.files)
    return [name for name in names if name not in tracked and (context.repo_path / name).is_file()]


def _requires(
    document: Any, spec: dict[str, Any], context: EvalContext, criterion_id: str
) -> RunResult:
    ecosystems = spec.get("ecosystems")
    if isinstance(ecosystems, dict):
        groups = _lockfile_groups(criterion_id, spec)
        selected: list[str] = []
        lower: list[str] = []
        for index, group in enumerate(groups):
            present = _present_lockfiles(context, group)
            if present:
                selected = present
                for legacy_group in groups[index + 1 :]:
                    lower.extend(_present_lockfiles(context, legacy_group))
                break
        if not selected:
            detail = "no tracked lock file matches a known ecosystem"
            untracked = _untracked_lockfiles(context, [name for group in groups for name in group])
            if untracked:
                detail += "; untracked on disk: " + ", ".join(sorted(untracked))
            return RunResult("fail", detail)
        updates: list[Any] = []
        if isinstance(document, dict) and isinstance(document.get("updates"), list):
            updates = document["updates"]
        found = {
            str(update.get("package-ecosystem"))
            for update in updates
            if isinstance(update, dict) and update.get("package-ecosystem") is not None
        }
        missing = sorted(name for name in selected if str(ecosystems[name]) not in found)
        if missing:
            return RunResult(
                "fail",
                "; ".join(f"{name} needs ecosystem {ecosystems[name]}" for name in missing)
                + "; dependabot declares "
                + (", ".join(sorted(found)) or "nothing"),
            )
        evidence = "; ".join(
            f"{name} is watched as {ecosystems[name]}" for name in sorted(selected)
        )
        if lower:
            # Lower-precedence lockfiles are legacy once a real lockfile is present.
            evidence += "; ignored lower-precedence: " + ", ".join(sorted(lower))
        return RunResult("pass", evidence)

    requires = _as_string_list(spec.get("require"), "require")
    for dotted in requires:
        resolved = dotted.replace("{default_branch}", context.default_branch or "")
        if "{default_branch}" in dotted and not context.default_branch:
            return RunResult("unavailable", "default branch could not be resolved")
        if not path_satisfied(document, resolved):
            return RunResult("fail", f"missing {resolved}")
    expects = spec.get("expect")
    if isinstance(expects, dict):
        for dotted, expected in expects.items():
            values = _resolve_dotted(document, str(dotted))
            if not any(value == expected for value in values):
                return RunResult("fail", f"{dotted} does not equal {expected!r}")
    return RunResult("pass", "required paths resolve")


def evaluate_command(criterion: Criterion, context: EvalContext) -> RunResult:
    spec = criterion.spec
    skip_globs = _as_string_list(spec.get("skip_if_no_paths"), "skip_if_no_paths")
    if skip_globs and not _expand(context, skip_globs):
        return RunResult("not-applicable", "no matching files for " + ", ".join(skip_globs))
    raw_command = spec["command"]
    argv = shlex.split(raw_command) if isinstance(raw_command, str) else list(raw_command)
    if not argv:
        raise ConfigError(f"{criterion.id}: empty command")
    if shutil.which(argv[0]) is None:
        return RunResult("unavailable", f"tool not found: {argv[0]}")
    timeout = int(spec.get("timeout", DEFAULT_TIMEOUT))
    environment = dict(os.environ)
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        completed = subprocess.run(
            argv,
            cwd=context.repo_path,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return RunResult("unavailable", f"timed out after {timeout}s")
    except OSError as exc:
        return RunResult("unavailable", f"could not run {argv[0]}: {exc.strerror}")
    if completed.returncode == 0:
        return RunResult("pass", "exit 0")
    return RunResult("fail", f"exit {completed.returncode}")


def evaluate_github_api(criterion: Criterion, context: EvalContext) -> RunResult:
    spec = criterion.spec
    endpoints = _as_string_list(spec.get("endpoints"), "endpoints")
    if not endpoints:
        endpoints = _as_string_list(spec.get("endpoint"), "endpoint")
    formatted = [endpoint.format(repo=context.repo_id, org=context.org) for endpoint in endpoints]
    if not context.github:
        return RunResult("unavailable", "github-api disabled; pass --github to evaluate")
    client = context.github_client or GhCliClient()
    observations: list[tuple[str, int]] = []
    for endpoint in formatted:
        code = client.get(endpoint)
        if code == 200:
            return RunResult("pass", f"{endpoint} -> 200")
        observations.append((endpoint, code))
    if all(code == 404 for _, code in observations):
        return RunResult("fail", "; ".join(f"{endpoint} -> 404" for endpoint, _ in observations))
    return RunResult(
        "unavailable",
        "; ".join(f"{endpoint} -> {code or 'no response'}" for endpoint, code in observations),
    )


# ---------------------------------------------------------------------------
# Tier resolution and classification
# ---------------------------------------------------------------------------


def _git_output(repo_path: Path, arguments: list[str], timeout: int = 30) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), *arguments],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", "replace")


def classify_tier(
    repo_path: Path, repo_id: str | None = None, files: tuple[str, ...] | None = None
) -> str:
    """Classify a repository from its tracked files.

    The rules are the verified ones in ``library/readiness-standard.md``: tracked
    files only, dependency trees pruned, a depth-0 manifest decides, and GitHub's
    ``primaryLanguage`` is never consulted.

    The single repository-specific branch is deliberate: the org ``.github``
    repository maps to the ``org`` tier, because that tier exists for exactly one
    repository. It is the only adopter special case; every other tier is decided
    from tracked files alone. ``architecture.md`` records it as the one permitted
    exception to "no repository-specific branching".
    """
    if repo_id is not None and repo_id.endswith("/.github"):
        return "org"
    tracked = files if files is not None else build_repo_index(repo_path).files
    names = set(tracked)
    extensions = Counter(PurePosixPath(name).suffix for name in tracked)

    pyproject = _read_text(repo_path / "pyproject.toml") or ""
    setup_py = _read_text(repo_path / "setup.py") or ""
    setup_cfg = _read_text(repo_path / "setup.cfg") or ""
    package_json = _read_text(repo_path / "package.json") or ""
    python_manifest = (
        "pyproject.toml" in names
        or "setup.py" in names
        or "setup.cfg" in names
        or bool(re.search(r"(?m)^\s*\[project\]", setup_cfg))
    )

    julia = "Project.toml" in names and extensions[".jl"] > 0
    if julia:
        return "julia"

    notebooks = extensions[".ipynb"]
    python_files = extensions[".py"]
    if (
        notebooks >= 5
        and notebooks > python_files
        and not python_manifest
        and "package.json" not in names
    ):
        return "notebook"

    bun_signals = any(name in names for name in ("bun.lock", "bun.lockb", "bunfig.toml")) or bool(
        re.search(r'"scripts"[\s\S]*\bbun\b', package_json)
    )
    if "package.json" in names and not python_manifest:
        return "typescript-bun" if bun_signals else "typescript-node"

    if python_manifest or python_files > 0:
        service_signal = re.search(
            r"\[project\.scripts\]|\b(fastapi|flask|uvicorn)\b",
            f"{pyproject}\n{setup_py}\n{setup_cfg}",
            re.IGNORECASE,
        )
        return "python-service" if service_signal else "python-library"

    html_files = extensions[".html"]
    if html_files > 0 and html_files >= python_files:
        return "static-html"

    return "content-data"


def resolve_default_branch(
    repo_path: Path, manifest: dict[str, Any], fallback: str | None
) -> str | None:
    declared = manifest.get("default_branch")
    if isinstance(declared, str) and declared:
        return declared
    if fallback:
        return fallback
    symbolic = _git_output(repo_path, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if symbolic:
        name = symbolic.strip()
        return name.split("/", 1)[1] if "/" in name else name
    return None


def _commit_sha(repo_path: Path) -> str | None:
    value = _git_output(repo_path, ["rev-parse", "HEAD"])
    return value.strip() if value else None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_repo(
    standard: Standard,
    repo_id: str,
    repo_path: Path,
    *,
    tier: str | None = None,
    github: bool = False,
    today: date | None = None,
    github_client: GithubClient | None = None,
    org: str = DEFAULT_ORG,
    default_branch: str | None = None,
) -> dict[str, Any]:
    repo_path = Path(repo_path)
    manifest = load_manifest(repo_path)
    index = build_repo_index(repo_path)
    if tier is not None:
        if tier not in standard.tiers:
            raise ConfigError(f"unknown tier: {tier!r}")
        resolved_tier = tier
        tier_source = "cli"
    else:
        declared_tier = manifest.get("tier")
        if declared_tier is not None:
            if not isinstance(declared_tier, str) or declared_tier not in standard.tiers:
                raise ConfigError(f".aii/readiness.json declares unknown tier {declared_tier!r}")
            resolved_tier = declared_tier
            tier_source = "manifest"
        else:
            classified = classify_tier(repo_path, repo_id=repo_id, files=index.files)
            if classified in standard.tiers:
                resolved_tier = classified
            elif "core" in standard.tiers:
                resolved_tier = "core"
            else:
                resolved_tier = next(iter(standard.tiers))
            tier_source = "classified"

    branch = resolve_default_branch(repo_path, manifest, default_branch)
    waivers = parse_waivers(standard, manifest, today or date.today())
    context = EvalContext(
        repo_id=repo_id,
        repo_path=repo_path,
        org=org,
        github=github,
        github_client=github_client,
        default_branch=branch,
        index=index,
    )
    closure = standard.tier_closure(resolved_tier)
    exclusions = standard.exclusions(resolved_tier)
    report = RepoReport(
        repo_id=repo_id,
        tier=resolved_tier,
        tier_source=tier_source,
        commit=_commit_sha(repo_path),
        default_branch=branch,
    )
    for criterion in standard.criteria:
        if criterion.id in exclusions:
            excluded_by = exclusions[criterion.id]
            report.criteria[criterion.id] = {
                "kind": criterion.kind,
                "blocking": criterion.blocking,
                "status": "not-applicable",
                "evidence": f"tier {resolved_tier} excludes {criterion.id}: {excluded_by}",
            }
            continue
        if not any(member in closure for member in criterion.within):
            report.criteria[criterion.id] = {
                "kind": criterion.kind,
                "blocking": criterion.blocking,
                "status": "not-applicable",
                "evidence": f"tier {resolved_tier} excludes {', '.join(criterion.within)}",
            }
            continue
        result = evaluate_criterion(criterion, context)
        status = result.status
        evidence = result.evidence
        if status == "fail" and criterion.id in waivers:
            status = "waived"
            evidence = f"waived: {waivers[criterion.id]} ({evidence})"
        report.criteria[criterion.id] = {
            "kind": criterion.kind,
            "blocking": criterion.blocking,
            "status": status,
            "evidence": evidence,
        }
    return report.as_dict()


def _unavailable_repo_report(standard: Standard, repo_id: str, evidence: str) -> dict[str, Any]:
    criteria = {
        criterion.id: {
            "kind": criterion.kind,
            "blocking": criterion.blocking,
            "status": "unavailable",
            "evidence": evidence,
        }
        for criterion in standard.criteria
    }
    return {
        "tier": "unknown",
        "tier_source": "working-copy-unavailable",
        "commit": None,
        "default_branch": None,
        "criteria": criteria,
        "summary": summarize(criteria),
    }


def summarize(criteria: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts = Counter(entry["status"] for entry in criteria.values())
    summary = {status: counts.get(status, 0) for status in STATUSES}
    summary["total"] = len(criteria)
    summary["blocking_failures"] = sum(
        1 for entry in criteria.values() if entry["blocking"] and entry["status"] == "fail"
    )
    summary["blocking_unavailable"] = sum(
        1 for entry in criteria.values() if entry["blocking"] and entry["status"] == "unavailable"
    )
    return summary


# ---------------------------------------------------------------------------
# Scope handling
# ---------------------------------------------------------------------------


def read_scope(path: Path) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError(f"cannot read scope file {path}: {exc}") from exc
    if not lines:
        raise ConfigError(f"scope file {path} is empty")
    header = lines[0].split("\t")
    required = {"repo", "rel"}
    if not required <= set(header):
        raise ConfigError(f"scope file {path} must declare columns: repo, rel")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split("\t")
        row = dict(zip(header, values, strict=False))
        repo = row.get("repo", "").strip()
        relative = row.get("rel", "").strip()
        if not repo or not relative:
            raise ConfigError(f"scope file {path} has a row without repo or rel")
        if repo in seen:
            raise ConfigError(f"scope file {path} duplicates repository {repo}")
        seen.add(repo)
        rows.append(row)
    if not rows:
        raise ConfigError(f"scope file {path} has no repository rows")
    return sorted(rows, key=lambda row: row["repo"])


def _scope_branch(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"'name'\s*:\s*'([^']+)'", value)
    if match:
        return match.group(1)
    cleaned = value.strip().strip("{}").strip()
    return cleaned or None


def build_all_report(
    standard: Standard,
    scope_path: Path,
    root: Path,
    *,
    github: bool,
    today: date,
    github_client: GithubClient | None,
    org: str = DEFAULT_ORG,
) -> dict[str, Any]:
    rows = read_scope(scope_path)
    repos: dict[str, dict[str, Any]] = {}
    for row in rows:
        repo_id = f"{org}/{row['repo']}"
        repo_path = root / row["rel"]
        if not repo_path.is_dir():
            repos[repo_id] = _unavailable_repo_report(
                standard, repo_id, "working copy not available"
            )
            continue
        repos[repo_id] = evaluate_repo(
            standard,
            repo_id,
            repo_path,
            github=github,
            today=today,
            github_client=github_client,
            org=org,
            default_branch=_scope_branch(row.get("default_branch")),
        )
    return _report_envelope(standard, repos)


def build_single_report(
    standard: Standard,
    repo_id: str,
    repo_path: Path,
    *,
    tier: str | None,
    github: bool,
    today: date,
    github_client: GithubClient | None,
    org: str = DEFAULT_ORG,
) -> dict[str, Any]:
    repos = {
        repo_id: evaluate_repo(
            standard,
            repo_id,
            repo_path,
            tier=tier,
            github=github,
            today=today,
            github_client=github_client,
            org=org,
        )
    }
    return _report_envelope(standard, repos)


def _report_envelope(standard: Standard, repos: dict[str, dict[str, Any]]) -> dict[str, Any]:
    totals = Counter[str]()
    for repo in repos.values():
        for status in STATUSES:
            totals[status] += repo["summary"][status]
    summary = {status: totals.get(status, 0) for status in STATUSES}
    summary["total"] = sum(totals.values())
    return {
        "schema": REPORT_SCHEMA,
        "standard": {"schema": standard.schema, "version": standard.version},
        "repos": repos,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Exit codes and presentation
# ---------------------------------------------------------------------------


def gate_exit_code(report: dict[str, Any]) -> int:
    """Honest gate semantics for ``--check``.

    An unevaluable criterion outranks a definite failure: the gate could not be
    certified, so it reports 3 rather than claiming the standard was measured.
    """
    criteria = [entry for repo in report["repos"].values() for entry in repo["criteria"].values()]
    if any(entry["status"] == "unavailable" for entry in criteria):
        return 3
    if any(entry["blocking"] and entry["status"] == "fail" for entry in criteria):
        return 1
    return 0


def render_human(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for repo_id in sorted(report["repos"]):
        repo = report["repos"][repo_id]
        lines.append(f"{repo_id}  tier={repo['tier']} ({repo['tier_source']})")
        for criterion_id in sorted(repo["criteria"]):
            entry = repo["criteria"][criterion_id]
            lines.append(f"  {entry['status']:<14} {criterion_id:<30} {entry['evidence']}")
        summary = repo["summary"]
        lines.append(
            f"  summary: {summary['pass']} pass, {summary['fail']} fail, "
            f"{summary['waived']} waived, {summary['unavailable']} unavailable, "
            f"{summary['not-applicable']} n/a"
        )
    summary = report["summary"]
    lines.append(
        f"total: {summary['pass']} pass, {summary['fail']} fail, "
        f"{summary['waived']} waived, {summary['unavailable']} unavailable, "
        f"{summary['not-applicable']} n/a"
    )
    return "\n".join(lines)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"--today must be an ISO date: {value!r}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_readiness.py",
        description="Evaluate the AII readiness criterion catalogue against repositories.",
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--repo", help="OWNER/REPO for a single-repository run")
    selection.add_argument("--all", action="store_true", help="evaluate every scope row")
    parser.add_argument("--path", help="working copy for --repo")
    parser.add_argument("--scope", help="scope TSV for --all")
    parser.add_argument(
        "--root",
        default="/Volumes/GitVault",
        help="root that scope rel paths resolve against (default: /Volumes/GitVault)",
    )
    parser.add_argument(
        "--standard",
        default=str(Path(__file__).resolve().parents[1] / "standards" / "tiers.yaml"),
        help="path to tiers.yaml",
    )
    parser.add_argument("--tier", help="override the tier for a --repo run")
    parser.add_argument("--json", action="store_true", help="emit deterministic JSON")
    parser.add_argument("--check", action="store_true", help="apply the blocking gate")
    parser.add_argument(
        "--github",
        action="store_true",
        help="enable github-api criteria (requires an authenticated gh)",
    )
    parser.add_argument("--today", help="ISO date used for waiver expiry (default: today)")
    args = parser.parse_args(argv)

    try:
        standard = load_standard(Path(args.standard))
        today = _parse_date(args.today) if args.today else date.today()
        if args.all:
            if not args.scope:
                parser.error("--all requires --scope")
            if args.path or args.tier:
                parser.error("--path and --tier are only valid with --repo")
            report = build_all_report(
                standard,
                Path(args.scope),
                Path(args.root),
                github=args.github,
                today=today,
                github_client=None,
            )
        else:
            if not args.path:
                parser.error("--repo requires --path")
            repo_path = Path(args.path)
            if not repo_path.is_dir():
                raise ConfigError(f"repository path not found: {repo_path}")
            report = build_single_report(
                standard,
                args.repo,
                repo_path,
                tier=args.tier,
                github=args.github,
                today=today,
                github_client=None,
            )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_human(report) + "\n")
    if not args.check:
        return 0
    return gate_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
