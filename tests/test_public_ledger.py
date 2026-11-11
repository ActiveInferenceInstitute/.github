"""Tests for the public ledger builder and the boundary command.

The org ``.github`` repository is public, so ``standards/public-ledger.json`` may
carry every public repository in full and only aggregate counts for the private
ones.  These tests are the executable form of that boundary:

* the builder keeps a public repository's full result and reduces a private one
  to counts;
* it refuses a report row that is not in the scope table, a scope row that is
  not in the report, and a public entry that names a private repository;
* its output is byte-identical across runs;
* the boundary command fails on a private name even when it is unquoted or part
  of a URL, applies the catalogue's own ``forbid`` pattern, and reports bad
  input as exit 2 rather than a silent pass.

Run with ``uv run pytest -q`` from the repository root.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BUILDER = SCRIPTS / "build_public_ledger.py"
BOUNDARY = SCRIPTS / "check_ledger_boundary.py"
SHIPPED_STANDARD = ROOT / "standards" / "tiers.yaml"

sys.path.insert(0, str(SCRIPTS))
import build_public_ledger as builder  # noqa: E402
import check_ledger_boundary as boundary  # noqa: E402

SCOPE_TSV = (
    "repo\trel\n"
    "public-repo\tUsername/owner/Public/public-repo\n"
    "secret-repo\tUsername/owner/Private/secret-repo\n"
)


def _summary(**statuses: int) -> dict[str, int]:
    counts = {status: int(statuses.get(status, 0)) for status in builder.STATUSES}
    counts["total"] = sum(counts.values())
    return counts


def _entry(**statuses: int) -> dict[str, Any]:
    return {
        "commit": "a" * 40,
        "default_branch": "main",
        "tier": "python-library",
        "tier_source": "manifest",
        "summary": _summary(**statuses),
        "criteria": {"CORE-README": {"status": "pass", "evidence": "README.md:1"}},
    }


def _report() -> dict[str, Any]:
    return {
        "schema": "aii-readiness-report/v1",
        "standard": {"schema": "aii-readiness-standard/v1", "version": "1.0.0"},
        "repos": {
            "owner/public-repo": _entry(pass_=2, fail=1),
            "owner/secret-repo": _entry(pass_=3),
        },
    }


def _scope_file(tmp_path: Path, text: str = SCOPE_TSV) -> Path:
    path = tmp_path / "scope.tsv"
    path.write_text(text, encoding="utf-8")
    return path


def _report_file(tmp_path: Path, report: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report or _report()), encoding="utf-8")
    return path


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_builder_keeps_public_repos_in_full(tmp_path: Path) -> None:
    report = _report()
    ledger = builder.build_ledger(report, builder.load_scope(_scope_file(tmp_path)))

    assert ledger["schema"] == builder.LEDGER_SCHEMA
    assert ledger["standard"] == report["standard"]
    assert ledger["scope"] == {"total": 2, "public": 1, "private": 1}
    assert ledger["public"]["count"] == 1
    assert ledger["public"]["repos"] == {"owner/public-repo": report["repos"]["owner/public-repo"]}
    assert ledger["public"]["summary"] == _summary(pass_=2, fail=1)


def test_builder_reduces_private_repos_to_aggregate_counts(tmp_path: Path) -> None:
    ledger = builder.build_ledger(_report(), builder.load_scope(_scope_file(tmp_path)))
    text = builder.serialize(ledger)

    assert ledger["private"] == {"count": 1, "summary": _summary(pass_=3)}
    assert "secret-repo" not in text
    assert "Private/" not in text


def test_builder_is_deterministic(tmp_path: Path) -> None:
    scope = builder.load_scope(_scope_file(tmp_path))
    first = builder.serialize(builder.build_ledger(_report(), scope))
    second = builder.serialize(builder.build_ledger(_report(), scope))
    assert first == second

    report_file = _report_file(tmp_path)
    scope_file = _scope_file(tmp_path)
    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"
    for out in (out_a, out_b):
        built = _run(
            BUILDER,
            "--report",
            str(report_file),
            "--scope",
            str(scope_file),
            "--out",
            str(out),
        )
        assert built.returncode == 0, built.stderr
    assert out_a.read_bytes() == out_b.read_bytes()


def test_builder_rejects_a_report_row_outside_the_scope_table(tmp_path: Path) -> None:
    report = _report()
    report["repos"]["owner/unknown-repo"] = _entry(pass_=1)
    with pytest.raises(builder.LedgerError, match="not in the scope table"):
        builder.build_ledger(report, builder.load_scope(_scope_file(tmp_path)))


def test_builder_rejects_a_scope_row_missing_from_the_report(tmp_path: Path) -> None:
    report = _report()
    del report["repos"]["owner/secret-repo"]
    with pytest.raises(builder.LedgerError, match="missing from the report"):
        builder.build_ledger(report, builder.load_scope(_scope_file(tmp_path)))


def test_builder_refuses_a_public_entry_that_names_a_private_repo(tmp_path: Path) -> None:
    report = _report()
    report["repos"]["owner/public-repo"]["criteria"]["CORE-README"]["evidence"] = (
        "Private/secret-repo/README.md:1"
    )
    with pytest.raises(builder.LedgerError, match="private repository names reached the ledger"):
        builder.build_ledger(report, builder.load_scope(_scope_file(tmp_path)))


def test_scope_visibility_reads_the_public_or_private_path_segment(tmp_path: Path) -> None:
    assert builder.load_scope(_scope_file(tmp_path)) == {
        "public-repo": "public",
        "secret-repo": "private",
    }

    with pytest.raises(builder.LedgerError, match="no Public/Private segment"):
        builder.load_scope(_scope_file(tmp_path, "repo\trel\nmirror\tUsername/owner/mirror\n"))
    with pytest.raises(builder.LedgerError, match="malformed scope row"):
        builder.load_scope(_scope_file(tmp_path, "repo\trel\nbroken\n"))


def test_boundary_command_passes_a_clean_ledger(tmp_path: Path) -> None:
    out = tmp_path / "public-ledger.json"
    built = _run(
        BUILDER,
        "--report",
        str(_report_file(tmp_path)),
        "--scope",
        str(_scope_file(tmp_path)),
        "--out",
        str(out),
    )
    assert built.returncode == 0, built.stderr

    result = _run(BOUNDARY, "--ledger", str(out), "--scope", str(_scope_file(tmp_path)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "boundary: clean" in result.stdout


def test_boundary_command_fails_on_an_unquoted_private_name(tmp_path: Path) -> None:
    ledger = tmp_path / "public-ledger.json"
    ledger.write_text('{"note": "see Private/secret-repo/README.md"}\n', encoding="utf-8")

    result = _run(BOUNDARY, "--ledger", str(ledger), "--scope", str(_scope_file(tmp_path)))
    assert result.returncode == 1
    assert "private repository name 'secret-repo'" in result.stdout


def test_boundary_command_fails_on_a_private_repo_url(tmp_path: Path) -> None:
    ledger = tmp_path / "public-ledger.json"
    ledger.write_text('{"url": "https://github.com/owner/secret-repo"}\n', encoding="utf-8")

    result = _run(BOUNDARY, "--ledger", str(ledger), "--scope", str(_scope_file(tmp_path)))
    assert result.returncode == 1
    assert "secret-repo" in result.stdout


def test_boundary_command_applies_the_catalogue_forbid_pattern(tmp_path: Path) -> None:
    """``InstituteOS`` is in the shipped catalogue's forbid list and not in the
    synthetic scope, so only the catalogue rule can produce this finding."""
    ledger = tmp_path / "public-ledger.json"
    ledger.write_text('{"name": "InstituteOS"}\n', encoding="utf-8")

    result = _run(BOUNDARY, "--ledger", str(ledger), "--scope", str(_scope_file(tmp_path)))
    assert result.returncode == 1
    assert "catalogue forbid pattern" in result.stdout
    assert "InstituteOS" in result.stdout


def test_boundary_command_reports_bad_input_as_exit_2(tmp_path: Path) -> None:
    missing = _run(
        BOUNDARY,
        "--ledger",
        str(tmp_path / "absent.json"),
        "--scope",
        str(_scope_file(tmp_path)),
    )
    assert missing.returncode == 2
    assert "not a file" in missing.stderr

    ledger = tmp_path / "public-ledger.json"
    ledger.write_text("{}\n", encoding="utf-8")
    broken_scope = _run(BOUNDARY, "--ledger", str(ledger), "--scope", str(tmp_path / "absent.tsv"))
    assert broken_scope.returncode == 2

    catalogue = tmp_path / "tiers.yaml"
    catalogue.write_text("schema: aii-readiness-standard/v1\ncriteria: []\n", encoding="utf-8")
    no_criterion = _run(
        BOUNDARY,
        "--ledger",
        str(ledger),
        "--scope",
        str(_scope_file(tmp_path)),
        "--standard",
        str(catalogue),
    )
    assert no_criterion.returncode == 2
    assert "is not in the catalogue" in no_criterion.stderr


def test_shipped_forbid_pattern_collides_with_the_public_aii_org_name() -> None:
    """The catalogue's pattern is written for JSON quoted tokens and matches
    ``aii-[a-z]+`` case-insensitively, so it also matches the *public*
    ``aii-org``.  A ledger therefore identifies repositories as ``OWNER/REPO``
    and never as a bare quoted name."""
    pattern = boundary.load_forbid_pattern(SHIPPED_STANDARD)

    assert re.search(pattern, '"InstituteOS"')
    assert re.search(pattern, '"aii-org"')
    assert re.search(pattern, '"ActiveInferenceInstitute/aii-org"') is None
