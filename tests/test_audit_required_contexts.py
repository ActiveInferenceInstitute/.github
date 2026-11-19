"""Tests for the required-context audit.

The audit protects the one artifact this organization cannot repair: a required
status-check context.  These tests pin the four naming cases, the "never read a
working tree" rule, the matrix-leg rule, the capture comparison, and honest exit
codes (``unavailable`` is never a pass).

Run with ``uv run pytest -q`` from the repository root.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_required_contexts.py"
WORKFLOW = ".github/workflows/ci.yml"


def _load_audit() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_required_contexts", SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_required_contexts"] = module
    spec.loader.exec_module(module)
    return module


audit = _load_audit()


def _ruleset(*contexts: str, ruleset_id: int = 1) -> dict[str, object]:
    return {
        "id": ruleset_id,
        "name": "require-pr-check",
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": context} for context in contexts]
                },
            },
            {"type": "deletion"},
        ],
    }


def test_fetch_rulesets_reads_effective_branch_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = []
    required = _ruleset("tests", ruleset_id=9)["rules"][0]
    required["ruleset_id"] = 9

    def fake_gh(path: str) -> list[dict[str, object]]:
        urls.append(path)
        return [{"type": "deletion", "ruleset_id": 3}, required]

    monkeypatch.setattr(audit, "gh_json", fake_gh)

    rulesets = audit.fetch_rulesets("ActiveInferenceInstitute/Example", "release/main")
    assert urls == [
        "repos/ActiveInferenceInstitute/Example/rules/branches/release%2Fmain?per_page=100"
    ]
    assert [ruleset["id"] for ruleset in rulesets] == [9]
    assert audit.required_contexts_from_ruleset(rulesets[0]) == ["tests"]


def _workflow(text: str) -> object:
    return audit.derive_workflow(WORKFLOW, text)


def _displays(derived: object) -> list[tuple[str, str]]:
    return [
        (display.display, display.mechanism) for job in derived.jobs for display in job.displays
    ]


def _audit(text: str, *contexts: str, repo: str = "Example") -> tuple[object, list[object]]:
    return audit.audit_repo(
        repo,
        "main",
        [_ruleset(*contexts)],
        [_workflow(text)],
        "0" * 40,
    )


# ---------------------------------------------------------------------------
# Naming cases
# ---------------------------------------------------------------------------


def test_named_job_reports_its_name() -> None:
    derived = _workflow(
        """
jobs:
  lint:
    name: Lint & Type Check
    runs-on: ubuntu-latest
    steps: []
"""
    )
    assert _displays(derived) == [("Lint & Type Check", "name")]


def test_unnamed_job_reports_its_job_id() -> None:
    derived = _workflow(
        """
jobs:
  tests:
    runs-on: ubuntu-latest
    steps: []
  gnn-conformance:
    name: gnn conformance (pinned)
    runs-on: ubuntu-latest
    steps: []
"""
    )
    assert ("tests", "job_id") in _displays(derived)
    assert ("gnn conformance (pinned)", "name") in _displays(derived)
    tests_job = next(job for job in derived.jobs if job.job_id == "tests")
    assert tests_job.has_name is False
    assert tests_job.raw_name is None


def test_unnamed_matrix_job_reports_leg_suffix() -> None:
    derived = _workflow(
        """
jobs:
  validate:
    strategy:
      matrix:
        python-version: ["3.11", "3.14"]
    runs-on: ubuntu-latest
    steps: []
"""
    )
    assert _displays(derived) == [
        ("validate (3.11)", "matrix-job-id"),
        ("validate (3.14)", "matrix-job-id"),
    ]


def test_two_leg_matrix_preserves_key_order() -> None:
    derived = _workflow(
        """
jobs:
  probes:
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        python: ["3.15", "3.11"]
    runs-on: ubuntu-latest
    steps: []
"""
    )
    displays = [display for display, _mechanism in _displays(derived)]
    assert "probes (ubuntu-latest, 3.15)" in displays
    assert "probes (windows-latest, 3.11)" in displays


def test_named_matrix_job_substitutes_direct_matrix_references() -> None:
    derived = _workflow(
        """
jobs:
  test:
    name: test (py${{ matrix.python-version }})
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    runs-on: ubuntu-latest
    steps: []
"""
    )
    assert _displays(derived) == [
        ("test (py3.11)", "matrix-name"),
        ("test (py3.12)", "matrix-name"),
    ]


def test_matrix_include_adds_a_combination() -> None:
    derived = _workflow(
        """
jobs:
  build-and-check:
    strategy:
      matrix:
        node: ["22", "24"]
        python: ["3.12"]
        include:
          - node: "22"
            python: "3.15"
    runs-on: ubuntu-latest
    steps: []
"""
    )
    displays = [display for display, _mechanism in _displays(derived)]
    assert "build-and-check (24, 3.12)" in displays
    assert "build-and-check (22, 3.12)" in displays
    assert "build-and-check (22, 3.15)" in displays


def test_matrix_include_augment_does_not_create_a_job() -> None:
    derived = _workflow(
        """
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.10", "3.11"]
        include:
          - python-version: "3.11"
            extras: "dev,llm"
    runs-on: ubuntu-latest
    steps: []
"""
    )
    displays = [display for display, _mechanism in _displays(derived)]
    assert len(displays) == 2
    assert displays == ["test (3.10)", "test (3.11, dev,llm)"]


def test_matrix_include_only_uses_entry_values() -> None:
    derived = _workflow(
        """
jobs:
  installed:
    strategy:
      matrix:
        include:
          - os: ubuntu-latest
            python: "3.11"
          - os: macos-latest
            python: "3.14"
    runs-on: ubuntu-latest
    steps: []
"""
    )
    displays = [display for display, _mechanism in _displays(derived)]
    assert "installed (ubuntu-latest, 3.11)" in displays
    assert "installed (macos-latest, 3.14)" in displays


def test_matrix_exclude_removes_a_leg() -> None:
    derived = _workflow(
        """
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
        exclude:
          - python-version: "3.11"
    runs-on: ubuntu-latest
    steps: []
"""
    )
    assert _displays(derived) == [("test (3.12)", "matrix-job-id")]


def test_unexpanded_expression_is_not_guessed() -> None:
    derived = _workflow(
        """
jobs:
  interchange:
    name: ${{ matrix.python == '3.12' && 'gnn conformance (pinned)' || 'other' }}
    strategy:
      matrix:
        python: ["3.11", "3.12"]
    runs-on: ubuntu-latest
    steps: []
"""
    )
    assert _displays(derived) == []
    assert derived.jobs[0].unexpanded is not None


def test_float_matrix_value_is_unmeasurable() -> None:
    derived = _workflow(
        """
jobs:
  test:
    strategy:
      matrix:
        python-version: [3.10, 3.11]
    runs-on: ubuntu-latest
    steps: []
"""
    )
    assert _displays(derived) == []
    assert "ambiguous" in (derived.jobs[0].unexpanded or "")


# ---------------------------------------------------------------------------
# Reusable workflow callers
# ---------------------------------------------------------------------------


def test_local_reusable_caller_reports_composite_name() -> None:
    contents = {
        ".github/workflows/callee.yml": """
jobs:
  conformance:
    name: gnn conformance (pinned)
    runs-on: ubuntu-latest
    steps: []
""",
        WORKFLOW: """
jobs:
  paired:
    name: paired-interchange
    uses: ./.github/workflows/callee.yml
""",
    }
    derived = audit.derive_repo_workflows(contents)
    caller = next(wf for wf in derived if wf.path == WORKFLOW)
    assert _displays(caller) == [
        ("paired-interchange / gnn conformance (pinned)", "reusable-caller")
    ]


def test_remote_reusable_caller_cannot_reproduce_a_bare_context() -> None:
    derived = _workflow(
        """
jobs:
  paired:
    name: gnn conformance (pinned)
    uses: ActiveInferenceInstitute/.github/.github/workflows/gnn-conformance.yml@abc
"""
    )
    displays = _displays(derived)
    assert len(displays) == 1
    assert displays[0][0] != "gnn conformance (pinned)"
    assert displays[0][0].endswith("/ <unresolved callee>")


# ---------------------------------------------------------------------------
# Requirement matching
# ---------------------------------------------------------------------------


def test_missing_context_is_high_severity() -> None:
    report, findings = _audit(
        """
jobs:
  lint:
    name: Lint & Type Check
    runs-on: ubuntu-latest
    steps: []
""",
        "Lint & Type Check",
        "test (3.12)",
    )
    assert report["missing_contexts"] == ["test (3.12)"]
    assert any(finding["code"] == "context_missing" for finding in findings)
    assert any(finding["severity"] == "high" for finding in findings)


def test_unexpanded_job_makes_missing_context_unavailable_not_a_mismatch() -> None:
    report, findings = _audit(
        """
jobs:
  interchange:
    name: ${{ matrix.python == '3.12' && 'gnn conformance (pinned)' || 'other' }}
    strategy:
      matrix:
        python: ["3.11", "3.12"]
    runs-on: ubuntu-latest
    steps: []
""",
        "gnn conformance (pinned)",
    )
    assert report["missing_contexts"] == ["gnn conformance (pinned)"]
    codes = {finding["code"] for finding in findings}
    assert "context_unmeasured" in codes
    assert "context_missing" not in codes
    assert all(finding["severity"] != "high" for finding in findings)


def test_gen24_unnamed_job_satisfies_the_context() -> None:
    report, findings = _audit(
        """
jobs:
  tests:
    runs-on: ubuntu-latest
    steps: []
  gnn-conformance:
    name: gnn conformance (pinned)
    runs-on: ubuntu-latest
    steps: []
""",
        "tests",
        repo="GEN24",
    )
    match = next(entry for entry in report["matches"] if entry["context"] == "tests")
    assert match["mechanism"] == "job_id"
    assert match["derived_from_job_id"] is True
    assert not any(finding["code"] == "gen24_job_gained_name" for finding in findings)


def test_gen24_job_that_gained_a_name_is_high_severity() -> None:
    _report, findings = _audit(
        """
jobs:
  tests:
    name: tests
    runs-on: ubuntu-latest
    steps: []
""",
        "tests",
        repo="GEN24",
    )
    codes = {finding["code"] for finding in findings}
    assert "gen24_job_gained_name" in codes
    assert "name_equals_job_id" in codes


def test_matrix_leg_missing_is_high_severity() -> None:
    _report, findings = _audit(
        """
jobs:
  validate:
    strategy:
      matrix:
        python-version: ["3.14"]
    runs-on: ubuntu-latest
    steps: []
""",
        "validate (3.11)",
    )
    codes = {finding["code"] for finding in findings}
    assert "matrix_leg_missing" in codes
    assert "context_missing" in codes


def test_literal_name_context_has_no_matrix_leg_finding() -> None:
    _report, findings = _audit(
        """
jobs:
  notebooks:
    name: tests and notebooks (julia 1.13)
    runs-on: ubuntu-latest
    steps: []
""",
        "tests and notebooks (julia 1.13)",
    )
    assert not any(finding["code"] == "matrix_leg_missing" for finding in findings)
    assert (
        audit.matrix_leg_check(
            "tests and notebooks (julia 1.13)",
            [
                _workflow(
                    """
jobs:
  notebooks:
    name: tests and notebooks (julia 1.13)
    runs-on: ubuntu-latest
    steps: []
"""
                )
            ],
        )
        is None
    )


# ---------------------------------------------------------------------------
# Capture parsing and comparison
# ---------------------------------------------------------------------------

CAPTURE = """
# Required status-check contexts — CONTRACT

## Summary

- Repos with a required context: **2**
- Distinct context strings: **2**

## Contexts and their repositories

| Required context string | Count | Repositories |
|---|---|---|
| `Fast source checks` | 1 | `Active_Fedference` |
| `gnn conformance (pinned)` | 1 | `fundamentals` |

## Known unsatisfiable contexts (pre-existing, high severity)

| Repo | Ruleset requires | Workflow emits | Fix |
|---|---|---|---|
| `fundamentals` | `gnn conformance (pinned)` | `gnn conformance (pinned v5.0.3)` | fix |

## How a context string is produced — four cases

Do not parse the prose.
"""


def test_capture_parser_reads_the_table_and_history() -> None:
    capture = audit.parse_capture(CAPTURE)
    assert capture.declared_repos == 2
    assert capture.declared_distinct == 2
    assert capture.repo_contexts == {
        "Active_Fedference": ("Fast source checks",),
        "fundamentals": ("gnn conformance (pinned)",),
    }
    assert capture.historical == (
        {
            "repo": "fundamentals",
            "required": "gnn conformance (pinned)",
            "claimed_emitted": "gnn conformance (pinned v5.0.3)",
        },
    )


def test_capture_that_matches_live_has_no_differences() -> None:
    capture = audit.parse_capture(CAPTURE)
    live = {
        "Active_Fedference": ["Fast source checks"],
        "fundamentals": ["gnn conformance (pinned)"],
    }
    reports = {
        "fundamentals": {
            "required_contexts": ["gnn conformance (pinned)"],
            "produced_contexts": ["gnn conformance (pinned)"],
        }
    }
    comparison, findings = audit.compare_capture(capture, live, reports)
    assert comparison["matches_live"] is True
    assert comparison["differences"] == []
    assert comparison["historical"][0]["resolved"] is True
    assert comparison["historical"][0]["capture_was_stale"] is True
    assert findings == []


def test_capture_historical_claim_that_is_still_true_is_high_severity() -> None:
    capture = audit.parse_capture(CAPTURE)
    live = {"fundamentals": ["gnn conformance (pinned)"]}
    reports = {
        "fundamentals": {
            "required_contexts": ["gnn conformance (pinned)"],
            "produced_contexts": ["gnn conformance (pinned v5.0.3)"],
        }
    }
    _comparison, findings = audit.compare_capture(capture, live, reports)
    assert any(
        finding["code"] == "capture_historical_claim_still_true" and finding["severity"] == "high"
        for finding in findings
    )


def test_capture_table_disagreement_is_high_severity() -> None:
    capture = audit.parse_capture(CAPTURE)
    live = {"Active_Fedference": ["Fast source checks"]}
    comparison, findings = audit.compare_capture(capture, live, {})
    assert comparison["matches_live"] is False
    assert comparison["differences"][0]["repo"] == "fundamentals"
    assert any(
        finding["code"] == "capture_table_mismatch" and finding["severity"] == "high"
        for finding in findings
    )


# ---------------------------------------------------------------------------
# Default-branch reads and the working-tree trap
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )
    return proc.stdout


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    workflows = work / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  tests:\n    runs-on: ubuntu-latest\n    steps: []\n",
        encoding="utf-8",
    )
    _git(work, "add", ".github/workflows/ci.yml")
    _git(work, "commit", "-m", "initial")
    _git(work, "push", "-u", "origin", "main")
    return origin, work


def test_default_branch_read_ignores_a_stale_working_tree(tmp_path: Path) -> None:
    _origin, work = _init_repo(tmp_path)
    # The working tree drifts to a preserved branch's content; origin/main does not.
    (work / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  renamed:\n    runs-on: ubuntu-latest\n    steps: []\n",
        encoding="utf-8",
    )
    contents, source_commit, error = audit.read_default_workflows(work, "main", fetch=False)
    assert error is None
    assert len(source_commit) == 40
    assert "tests:" in contents[".github/workflows/ci.yml"]
    assert "renamed:" not in contents[".github/workflows/ci.yml"]


def test_stale_origin_ref_is_unavailable_until_fetched(tmp_path: Path) -> None:
    origin, work = _init_repo(tmp_path)
    second = tmp_path / "second"
    _git(tmp_path, "clone", str(origin), str(second))
    (second / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  validate:\n    runs-on: ubuntu-latest\n    steps: []\n",
        encoding="utf-8",
    )
    _git(second, "add", ".github/workflows/ci.yml")
    _git(second, "commit", "-m", "advance origin")
    _git(second, "push", "origin", "main")

    _contents, _sha, error = audit.read_default_workflows(work, "main", fetch=False)
    assert error is not None
    assert "stale" in error and "--fetch" in error

    contents, _sha, error = audit.read_default_workflows(work, "main", fetch=True)
    assert error is None
    assert "validate:" in contents[".github/workflows/ci.yml"]


# ---------------------------------------------------------------------------
# Report determinism and exit codes
# ---------------------------------------------------------------------------


def _fake_scope(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    scope = tmp_path / "scope.tsv"
    lines = ["repo\tvisibility\trel"]
    for repo, rel in rows:
        lines.append(f"{repo}\tPUBLIC\t{rel}")
    scope.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return scope


def test_report_is_deterministic_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "Example").mkdir(parents=True)
    scope = _fake_scope(tmp_path, [("Example", "Example")])
    monkeypatch.setattr(audit, "gh_json", lambda _path, timeout=0: {"default_branch": "main"})
    monkeypatch.setattr(audit, "fetch_rulesets", lambda _slug, _branch: [_ruleset("tests")])

    def git_reader(
        _path: Path, _branch: str, *, fetch: bool, timeout: int
    ) -> tuple[dict[str, str], str, str | None]:
        return (
            {
                WORKFLOW: """
jobs:
  tests:
    runs-on: ubuntu-latest
    steps: []
"""
            },
            "a" * 40,
            None,
        )

    first = audit.build_report(
        scope,
        root,
        capture_path=None,
        expect_repos=1,
        expect_distinct=1,
        fetch=False,
        git_reader=git_reader,
    )
    second = audit.build_report(
        scope,
        root,
        capture_path=None,
        expect_repos=1,
        expect_distinct=1,
        fetch=False,
        git_reader=git_reader,
    )
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["summary"]["result"] == "pass"
    assert audit.report_exit_code(first) == 0


def test_report_fails_on_a_missing_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    (root / "Example").mkdir(parents=True)
    scope = _fake_scope(tmp_path, [("Example", "Example")])
    monkeypatch.setattr(audit, "gh_json", lambda _path, timeout=0: {"default_branch": "main"})
    monkeypatch.setattr(audit, "fetch_rulesets", lambda _slug, _branch: [_ruleset("renamed")])

    def git_reader(
        _path: Path, _branch: str, *, fetch: bool, timeout: int
    ) -> tuple[dict[str, str], str, str | None]:
        return (
            {WORKFLOW: "jobs:\n  tests:\n    runs-on: ubuntu-latest\n    steps: []\n"},
            "a" * 40,
            None,
        )

    report = audit.build_report(
        scope,
        root,
        capture_path=None,
        expect_repos=1,
        expect_distinct=1,
        fetch=False,
        git_reader=git_reader,
    )
    assert report["summary"]["result"] == "fail"
    assert audit.report_exit_code(report) == 1


def test_report_is_unavailable_when_a_working_copy_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = _fake_scope(tmp_path, [("Example", "Missing")])
    monkeypatch.setattr(audit, "gh_json", lambda _path, timeout=0: {"default_branch": "main"})
    monkeypatch.setattr(audit, "fetch_rulesets", lambda _slug, _branch: [_ruleset("tests")])

    report = audit.build_report(
        scope,
        tmp_path / "root",
        capture_path=None,
        expect_repos=1,
        expect_distinct=1,
        fetch=False,
    )
    assert report["summary"]["result"] == "unavailable"
    assert audit.report_exit_code(report) == 3
