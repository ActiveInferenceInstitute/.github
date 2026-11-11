#!/usr/bin/env python3
"""The boundary command: prove a published ledger names no private repository.

``ORG-LEDGER-PUBLIC-ONLY`` asks whether the shipped ledger matches the
catalogue's ``forbid`` pattern.  This command is the wider gate the standard's
README names: it scans a ledger against the scope table and fails on *any*
occurrence of a private repository name or repository URL, quoted or not, and it
also applies the catalogue's own ``forbid`` pattern, so a ledger that would fail
the criterion is rejected before it is published.

The catalogue pattern is case-insensitive and matches ``aii-[a-z]+``, so it also
matches the *public* ``aii-org``.  A faithful ledger identifies repositories as
``OWNER/REPO`` and never as a bare quoted name, so the collision stays harmless —
and this command proves it.

Usage::

    uv run python scripts/check_ledger_boundary.py \
        --ledger standards/public-ledger.json \
        --scope /path/to/scope.tsv

Exit codes:
    0  the ledger names no private repository
    1  a private repository name or URL appears (every finding is printed)
    2  usage, scope or catalogue error
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# Sibling script in this directory: sys.path[0] is scripts/ when either runs.
from build_public_ledger import (
    PRIVATE,
    LedgerError,
    load_scope,
    repository_name_pattern,
)

LEDGER_CRITERION = "ORG-LEDGER-PUBLIC-ONLY"


class BoundaryError(Exception):
    """A ledger, scope or catalogue that cannot be scanned."""


@dataclass(frozen=True)
class Finding:
    """One private-name occurrence, located in the ledger by line."""

    line: int
    rule: str
    name: str
    excerpt: str

    def render(self) -> str:
        return f"line {self.line}: {self.rule} {self.name!r}: {self.excerpt}"


def load_forbid_pattern(standard: Path, criterion_id: str = LEDGER_CRITERION) -> str:
    """Return the catalogue's ``forbid`` regex for the ledger criterion."""
    if not standard.is_file():
        raise BoundaryError(f"not a file: {standard}")
    try:
        document = yaml.safe_load(standard.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BoundaryError(f"{standard}: {exc}") from exc
    if not isinstance(document, dict):
        raise BoundaryError(f"{standard}: not a catalogue document")
    for criterion in document.get("criteria") or []:
        if not isinstance(criterion, dict) or criterion.get("id") != criterion_id:
            continue
        forbid = (criterion.get("spec") or {}).get("forbid")
        if not isinstance(forbid, str) or not forbid:
            raise BoundaryError(f"{standard}: {criterion_id} declares no forbid pattern")
        return forbid
    raise BoundaryError(f"{standard}: {criterion_id} is not in the catalogue")


def scan_ledger(text: str, scope: dict[str, str], forbid: str) -> list[Finding]:
    """Every private-name occurrence in a ledger, under both rules."""
    private = sorted(name for name, kind in scope.items() if kind == PRIVATE)
    name_patterns = [(name, repository_name_pattern(name)) for name in private]
    catalogue_pattern = re.compile(forbid)
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        excerpt = line.strip()[:160]
        for name, pattern in name_patterns:
            if pattern.search(line):
                findings.append(Finding(number, "private repository name", name, excerpt))
        match = catalogue_pattern.search(line)
        if match:
            findings.append(Finding(number, "catalogue forbid pattern", match.group(0), excerpt))
    return findings


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        prog="check_ledger_boundary.py",
        description="Fail when a published readiness ledger names a private repository.",
    )
    parser.add_argument(
        "--ledger",
        default=str(here / "standards" / "public-ledger.json"),
        help="ledger to scan (default: standards/public-ledger.json)",
    )
    parser.add_argument("--scope", required=True, help="scope TSV naming the private rows")
    parser.add_argument(
        "--standard",
        default=str(here / "standards" / "tiers.yaml"),
        help="catalogue that owns ORG-LEDGER-PUBLIC-ONLY (default: standards/tiers.yaml)",
    )
    args = parser.parse_args(argv)

    try:
        ledger_path = Path(args.ledger)
        if not ledger_path.is_file():
            raise BoundaryError(f"not a file: {ledger_path}")
        scope = load_scope(Path(args.scope))
        forbid = load_forbid_pattern(Path(args.standard))
        findings = scan_ledger(ledger_path.read_text(encoding="utf-8"), scope, forbid)
    except (BoundaryError, LedgerError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    private_count = sum(1 for kind in scope.values() if kind == PRIVATE)
    if findings:
        for finding in findings:
            print(finding.render())
        print(
            f"boundary: {len(findings)} violation(s) across "
            f"{private_count} private scope rows in {ledger_path}"
        )
        return 1
    print(f"boundary: clean; {ledger_path} names none of the {private_count} private scope rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
