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
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BUILDER = SCRIPTS / "build_public_ledger.py"
BOUNDARY = SCRIPTS / "check_ledger_boundary.py"
SHIPPED_STANDARD = ROOT / "standards" / "tiers.yaml"
SHIPPED_LEDGER = ROOT / "standards" / "public-ledger.json"

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


def _assert_builder_refuses(
    tmp_path: Path, report_file: Path, scope_file: Path, diagnostic: str
) -> None:
    out = tmp_path / "refused-ledger.json"
    for previous in (None, b'{"previous": "ledger"}\r\n'):
        if previous is not None:
            out.write_bytes(previous)
        result = _run(
            BUILDER,
            "--report",
            str(report_file),
            "--scope",
            str(scope_file),
            "--out",
            str(out),
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert result.stdout == ""
        assert "error:" in result.stderr
        assert diagnostic in result.stderr
        assert "Traceback" not in result.stderr
        if previous is None:
            assert not out.exists()
        else:
            assert out.read_bytes() == previous


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


@pytest.mark.parametrize("repo_id", ["owner/public-repo", "owner/secret-repo"])
@pytest.mark.parametrize("present", [False, True])
def test_builder_preserves_absent_summary_counts(
    tmp_path: Path, repo_id: str, present: bool
) -> None:
    report = _report()
    entry = report["repos"][repo_id]
    del entry["summary"]
    if present:
        entry["summary"] = None

    scope = builder.load_scope(_scope_file(tmp_path))
    ledger = builder.build_ledger(report, scope)
    visibility = scope[repo_id.split("/", 1)[1]]
    assert ledger[visibility]["summary"] == _summary()
    if visibility == builder.PUBLIC:
        assert ledger["public"]["repos"][repo_id] == entry


def test_builder_aggregates_integer_summary_counts(tmp_path: Path) -> None:
    report = _report()
    public_counts = dict(zip(builder.STATUSES, (2, 0, 1, 3, 4), strict=True))
    private_counts = dict(zip(builder.STATUSES, (1, 2, 3, 4, 0), strict=True))
    for repo_id, counts in (
        ("owner/public-repo", public_counts),
        ("owner/secret-repo", private_counts),
    ):
        report["repos"][repo_id]["summary"] = {
            **counts,
            "total": sum(counts.values()),
            "blocking_failures": 0,
            "blocking_unavailable": 1,
        }

    ledger = builder.build_ledger(report, builder.load_scope(_scope_file(tmp_path)))
    assert ledger["public"]["summary"] == _summary(**public_counts)
    assert ledger["private"]["summary"] == _summary(**private_counts)
    assert ledger["public"]["repos"]["owner/public-repo"] == report["repos"]["owner/public-repo"]


@pytest.mark.parametrize("repo_id", ["owner/public-repo", "owner/secret-repo"])
@pytest.mark.parametrize(
    "summary",
    [
        "oops",
        "",
        1,
        0,
        False,
        [],
        ["pass"],
        {"pass": "oops"},
        {"pass": "1"},
        {"pass": {}},
        {"pass": []},
        {"pass": None},
        {"pass": True},
        {"pass": False},
        {"pass": -1},
        {"pass": 1.5},
        {"pass": 1.0},
        {"pass": float("inf")},
        {"pass": float("nan")},
        {"fail": -1},
        {"waived": -1},
        {"unavailable": -1},
        {"not-applicable": -1},
        {"total": -1},
        {"blocking_failures": -1},
        {"blocking_unavailable": -1},
    ],
)
def test_builder_refuses_malformed_summaries(tmp_path: Path, repo_id: str, summary: Any) -> None:
    report = _report()
    report["repos"][repo_id]["summary"] = summary
    scope_file = _scope_file(tmp_path)
    with pytest.raises(builder.LedgerError, match="report summary"):
        builder.build_ledger(report, builder.load_scope(scope_file))
    _assert_builder_refuses(tmp_path, _report_file(tmp_path, report), scope_file, "report summary")


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


@pytest.mark.parametrize(
    "evidence",
    ["Private/secret-repo/README.md:1", "https://github.com/OWNER/SeCrEt-RePo"],
)
def test_builder_refuses_a_public_entry_that_names_a_private_repo(
    tmp_path: Path, evidence: str
) -> None:
    report = _report()
    report["repos"]["owner/public-repo"]["criteria"]["CORE-README"]["evidence"] = evidence
    scope_file = _scope_file(tmp_path)
    diagnostic = "private repository names reached the ledger"
    with pytest.raises(builder.LedgerError, match=diagnostic):
        builder.build_ledger(report, builder.load_scope(scope_file))
    _assert_builder_refuses(tmp_path, _report_file(tmp_path, report), scope_file, diagnostic)


def test_scope_visibility_reads_the_public_or_private_path_segment(tmp_path: Path) -> None:
    assert builder.load_scope(_scope_file(tmp_path)) == {
        "public-repo": "public",
        "secret-repo": "private",
    }

    with pytest.raises(builder.LedgerError, match="no Public/Private segment"):
        builder.load_scope(_scope_file(tmp_path, "repo\trel\nmirror\tUsername/owner/mirror\n"))
    with pytest.raises(builder.LedgerError, match="row without repo or rel"):
        builder.load_scope(_scope_file(tmp_path, "repo\trel\nbroken\n"))


@pytest.mark.parametrize(
    "columns",
    [
        (
            "repo",
            "visibility",
            "language",
            "default_branch",
            "disk_kb",
            "rel",
            "local",
            "dot_aii",
            "workflows",
        ),
        (
            "visibility",
            "language",
            "repo",
            "default_branch",
            "rel",
            "disk_kb",
            "local",
            "dot_aii",
            "workflows",
        ),
    ],
)
def test_scope_named_columns_work_for_both_commands(
    tmp_path: Path, columns: tuple[str, ...]
) -> None:
    rows = [
        {
            "repo": " Public-Repo ",
            "rel": " Username/owner/Public/Public-Repo ",
            "visibility": "PRIVATE",
        },
        {
            "repo": "Secret-Repo",
            "rel": "Username/owner/Private/Secret-Repo",
            "visibility": "PUBLIC",
        },
    ]
    text = "\t".join(columns) + "\n"
    text += "\n".join("\t".join(row.get(column, "-") for column in columns) for row in rows)
    scope_file = _scope_file(tmp_path, text + "\n")
    assert builder.load_scope(scope_file) == {"Public-Repo": "public", "Secret-Repo": "private"}
    report = _report()
    report["repos"] = {
        "owner/Public-Repo": report["repos"]["owner/public-repo"],
        "owner/Secret-Repo": report["repos"]["owner/secret-repo"],
    }
    out = tmp_path / "public-ledger.json"
    built = _run(
        BUILDER,
        "--report",
        str(_report_file(tmp_path, report)),
        "--scope",
        str(scope_file),
        "--out",
        str(out),
    )
    assert built.returncode == 0, built.stderr
    ledger = json.loads(out.read_text(encoding="utf-8"))
    assert ledger["scope"] == {"total": 2, "public": 1, "private": 1}
    assert ledger["public"]["repos"] == {"owner/Public-Repo": report["repos"]["owner/Public-Repo"]}
    assert "Secret-Repo" not in out.read_text(encoding="utf-8")

    result = _run(BOUNDARY, "--ledger", str(out), "--scope", str(scope_file))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "boundary: clean" in result.stdout


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("secret-repo", "Private"), ("secret-repo", "Public")),
        (("secret-repo", "Public"), ("secret-repo", "Private")),
        (("secret-repo", "Private"), ("secret-repo", "Private")),
        (("secret-repo", "Public"), ("secret-repo", "Public")),
        (("secret-repo", "Private"), ("SeCrEt-RePo", "Public")),
        (("secret-repo", "Public"), ("SeCrEt-RePo", "Private")),
        ((" secret-repo ", "Private"), ("secret-repo", "Public")),
        (("secret-repo", "Public"), (" SeCrEt-RePo ", "Private")),
    ],
)
def test_both_commands_refuse_duplicate_scope_identities(
    tmp_path: Path, first: tuple[str, str], second: tuple[str, str]
) -> None:
    text = "repo\trel\npublic-repo\tUsername/owner/Public/public-repo\n"
    text += "".join(
        f"{name}\tUsername/owner/{visibility}/{name.strip()}\n"
        for name, visibility in (first, second)
    )
    scope_file = _scope_file(tmp_path, text)
    diagnostic = "duplicates repository"
    with pytest.raises(builder.LedgerError, match=diagnostic):
        builder.load_scope(scope_file)
    _assert_builder_refuses(tmp_path, _report_file(tmp_path), scope_file, diagnostic)

    ledger = tmp_path / "candidate.json"
    ledger.write_text("{}\n", encoding="utf-8")
    result = _run(BOUNDARY, "--ledger", str(ledger), "--scope", str(scope_file))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "error:" in result.stderr
    assert diagnostic in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("text", "diagnostic"),
    [
        ("", "is empty"),
        ("repo\trel\n", "has no repository rows"),
        ("repo\tvisibility\npublic-repo\tPUBLIC\n", "must declare columns: repo, rel"),
        ("repo\trel\nbroken\n", "row without repo or rel"),
        ("repo\trel\npublic-repo\t \n", "row without repo or rel"),
    ],
)
def test_both_commands_refuse_malformed_scope(tmp_path: Path, text: str, diagnostic: str) -> None:
    scope_file = _scope_file(tmp_path, text)
    with pytest.raises(builder.LedgerError, match=diagnostic):
        builder.load_scope(scope_file)
    _assert_builder_refuses(tmp_path, _report_file(tmp_path), scope_file, diagnostic)

    ledger = tmp_path / "candidate.json"
    ledger.write_text("{}\n", encoding="utf-8")
    result = _run(BOUNDARY, "--ledger", str(ledger), "--scope", str(scope_file))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "error:" in result.stderr
    assert diagnostic in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("affix", ["x", "0", "_", ".", "-"])
@pytest.mark.parametrize("prefix", [False, True])
def test_private_name_matching_preserves_whole_token_boundaries(
    tmp_path: Path, affix: str, prefix: bool
) -> None:
    name = f"{affix}SeCrEt-RePo" if prefix else f"SeCrEt-RePo{affix}"
    report = _report()
    evidence = f"https://github.com/OWNER/{name}"
    report["repos"]["owner/public-repo"]["criteria"]["CORE-README"]["evidence"] = evidence
    scope = builder.load_scope(_scope_file(tmp_path))
    ledger = builder.build_ledger(report, scope)
    assert ledger["public"]["repos"]["owner/public-repo"] == report["repos"]["owner/public-repo"]
    findings = boundary.scan_ledger(
        builder.serialize(ledger), scope, boundary.load_forbid_pattern(SHIPPED_STANDARD)
    )
    assert findings == []


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


@pytest.mark.parametrize(
    "url", ["https://github.com/owner/secret-repo", "https://github.com/OWNER/SeCrEt-RePo"]
)
def test_boundary_command_fails_on_a_private_repo_url(tmp_path: Path, url: str) -> None:
    ledger = tmp_path / "public-ledger.json"
    ledger.write_text(json.dumps({"url": url}) + "\n", encoding="utf-8")

    result = _run(BOUNDARY, "--ledger", str(ledger), "--scope", str(_scope_file(tmp_path)))
    assert result.returncode == 1
    assert "private repository name 'secret-repo'" in result.stdout
    assert result.stderr == ""


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


def test_shipped_ledger_matches_the_documented_shape() -> None:
    """``standards/README.md`` documents the ledger's member set; the shipped file
    must match it, and must record the catalogue version it was built from."""
    ledger = json.loads(SHIPPED_LEDGER.read_text(encoding="utf-8"))
    catalogue = yaml.safe_load(SHIPPED_STANDARD.read_text(encoding="utf-8"))

    assert set(ledger) == {"schema", "standard", "scope", "public", "private"}
    assert ledger["schema"] == builder.LEDGER_SCHEMA
    assert ledger["standard"] == {
        "schema": catalogue["schema"],
        "version": catalogue["version"],
    }
    assert set(ledger["scope"]) == {"total", "public", "private"}
    assert set(ledger["public"]) == {"count", "repos", "summary"}
    assert set(ledger["private"]) == {"count", "summary"}
    assert ledger["public"]["count"] == len(ledger["public"]["repos"])
    assert ledger["scope"]["public"] + ledger["scope"]["private"] == ledger["scope"]["total"]
    for repo_id, entry in ledger["public"]["repos"].items():
        assert set(entry) == {
            "tier",
            "tier_source",
            "commit",
            "default_branch",
            "criteria",
            "summary",
        }, repo_id
        assert entry["summary"]["total"] == len(entry["criteria"]), repo_id


def test_shipped_forbid_pattern_collides_with_the_public_aii_org_name() -> None:
    """The catalogue's pattern is written for JSON quoted tokens and matches
    ``aii-[a-z]+`` case-insensitively, so it also matches the *public*
    ``aii-org``.  A ledger therefore identifies repositories as ``OWNER/REPO``
    and never as a bare quoted name."""
    pattern = boundary.load_forbid_pattern(SHIPPED_STANDARD)

    assert re.search(pattern, '"InstituteOS"')
    assert re.search(pattern, '"aii-org"')
    assert re.search(pattern, '"ActiveInferenceInstitute/aii-org"') is None
