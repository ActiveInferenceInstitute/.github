#!/usr/bin/env python3
"""Build ``standards/public-ledger.json`` from a fleet readiness report.

The org ``.github`` repository is public, so the published ledger carries every
*public* repository's full readiness result and only aggregate counts for the
private repositories: no private repository name, URL or content may appear in
it.

Visibility comes from the scope table the checker already uses (named ``repo``
and ``rel`` columns), never from a disk walk or a language guess: a row is public
when its ``rel`` path carries the ``Public`` segment and private when it carries
``Private``.  A scope row without either segment is a configuration error.

Usage::

    uv run python scripts/build_public_ledger.py \
        --report /path/to/fleet-report.json \
        --scope /path/to/scope.tsv \
        --out standards/public-ledger.json

Exit codes:
    0  the ledger was written (or printed)
    2  usage, scope or report error, including a boundary violation
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from check_readiness import ConfigError, read_scope

LEDGER_SCHEMA = "aii-readiness-ledger/v1"
STATUSES = ("pass", "fail", "waived", "unavailable", "not-applicable")
PUBLIC = "public"
PRIVATE = "private"


class LedgerError(Exception):
    """A scope, report or boundary problem that prevents a publishable ledger."""


def load_scope(path: Path) -> dict[str, str]:
    """Return ``{repo: "public"|"private"}`` for every row of a scope table."""
    try:
        scope_rows = read_scope(path)
    except ConfigError as exc:
        raise LedgerError(str(exc)) from exc
    rows: dict[str, str] = {}
    seen: set[str] = set()
    for row in scope_rows:
        repo, rel = row["repo"].strip(), row["rel"].strip()
        identity = repo.casefold()
        if identity in seen:
            raise LedgerError(f"scope file {path} duplicates repository {repo}")
        seen.add(identity)
        segments = Path(rel).parts
        if "Public" in segments:
            rows[repo] = PUBLIC
        elif "Private" in segments:
            rows[repo] = PRIVATE
        else:
            raise LedgerError(f"{path}: {repo}: no Public/Private segment in {rel!r}")
    return rows


def _counts(summary: dict[str, int]) -> dict[str, int]:
    counts = {status: summary.get(status, 0) for status in STATUSES}
    counts["total"] = sum(counts.values())
    return counts


def _add(target: dict[str, int], source: Any) -> None:
    if source is None:
        return
    if not isinstance(source, dict):
        raise LedgerError("report summary is not an object")
    for status, count in source.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise LedgerError(f"report summary count {status!r} must be a nonnegative integer")
    for status in STATUSES:
        target[status] = target.get(status, 0) + source.get(status, 0)


def repository_name_pattern(name: str) -> re.Pattern[str]:
    """Match a repository name case-insensitively with whole-token boundaries."""
    return re.compile(
        rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])",
        re.IGNORECASE | re.ASCII,
    )


def private_name_patterns(scope: dict[str, str]) -> list[tuple[str, re.Pattern[str]]]:
    """Select and compile the scope's private names for both ledger consumers."""
    return [
        (name, repository_name_pattern(name))
        for name in sorted(name for name, kind in scope.items() if kind == PRIVATE)
    ]


def boundary_violations(text: str, scope: dict[str, str]) -> list[str]:
    """Private repository names that appear anywhere in a candidate ledger."""
    return [name for name, pattern in private_name_patterns(scope) if pattern.search(text)]


def build_ledger(report: dict[str, Any], scope: dict[str, str]) -> dict[str, Any]:
    """Return the publishable ledger for a fleet report and a scope table."""
    repos = report.get("repos")
    if not isinstance(repos, dict) or not repos:
        raise LedgerError("report has no 'repos' mapping")

    public: dict[str, Any] = {}
    public_counts: dict[str, int] = {}
    private_counts: dict[str, int] = {}
    seen: set[str] = set()
    for repo_id in sorted(repos):
        entry = repos[repo_id]
        if not isinstance(entry, dict):
            raise LedgerError(f"report entry for {repo_id!r} is not an object")
        name = repo_id.split("/", 1)[-1]
        seen.add(name)
        visibility = scope.get(name)
        if visibility is None:
            raise LedgerError(f"report row {repo_id!r} is not in the scope table")
        if visibility == PUBLIC:
            public[repo_id] = entry
            _add(public_counts, entry.get("summary"))
        else:
            _add(private_counts, entry.get("summary"))

    missing = sorted(set(scope) - seen)
    if missing:
        raise LedgerError(f"scope rows missing from the report: {missing}")
    if not public:
        raise LedgerError("report contains no public repository")

    ledger: dict[str, Any] = {
        "schema": LEDGER_SCHEMA,
        "standard": report.get("standard") or {},
        "scope": {
            "total": len(scope),
            "public": sum(1 for kind in scope.values() if kind == PUBLIC),
            "private": sum(1 for kind in scope.values() if kind == PRIVATE),
        },
        "public": {
            "count": len(public),
            "summary": _counts(public_counts),
            "repos": public,
        },
        "private": {
            "count": sum(1 for kind in scope.values() if kind == PRIVATE),
            "summary": _counts(private_counts),
        },
    }
    violations = boundary_violations(serialize(ledger), scope)
    if violations:
        raise LedgerError(f"private repository names reached the ledger: {violations}")
    return ledger


def serialize(ledger: dict[str, Any]) -> str:
    """Deterministic ledger bytes: sorted keys, two-space indent, LF, final newline."""
    return json.dumps(ledger, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_public_ledger.py",
        description="Build the public-only readiness ledger from a fleet report.",
    )
    parser.add_argument("--report", required=True, help="fleet report JSON (checker --json)")
    parser.add_argument("--scope", required=True, help="scope TSV used for the fleet report")
    parser.add_argument("--out", help="write the ledger here (default: stdout)")
    args = parser.parse_args(argv)

    try:
        scope_path = Path(args.scope)
        report_path = Path(args.report)
        for path in (scope_path, report_path):
            if not path.is_file():
                raise LedgerError(f"not a file: {path}")
        scope = load_scope(scope_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise LedgerError(f"{report_path}: report is not an object")
        text = serialize(build_ledger(report, scope))
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: {args.report}: {exc}", file=sys.stderr)
        return 2

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
