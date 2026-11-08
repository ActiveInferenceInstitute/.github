"""Tests for the AII readiness checker.

These tests are written against ``scripts/check_readiness.py`` and are the executable
form of the mission's readiness contract:

* every evaluation kind works (``file-exists``, ``file-contains``, ``yaml-path``,
  ``command``, ``github-api``);
* two ``--json`` runs on one commit produce byte-identical output;
* exit codes are honest: 0 all blocking pass, 1 blocking failure, 2 usage/config
  error, 3 a criterion could not be evaluated;
* ``unavailable`` is never reported as ``pass``;
* a waiver requires a non-empty reason;
* an unknown criterion kind is a hard error, never a silent skip.

Run with ``uv run pytest -q`` from the repository root.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_readiness.py"
SHIPPED_STANDARD = ROOT / "standards" / "tiers.yaml"

MARKER = "__SCRIPT_IMPORT_MARKER__"


def _load_checker() -> ModuleType:
    """Import the checker module from its script path."""
    spec = importlib.util.spec_from_file_location("check_readiness", SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_readiness"] = module
    spec.loader.exec_module(module)
    return module


def _write_standard(
    tmp_path: Path,
    criteria: list[dict[str, Any]],
    *,
    tiers: dict[str, Any] | None = None,
    name: str = "tiers.yaml",
) -> Path:
    document = {
        "schema": "aii-readiness-standard/v1",
        "version": "1.0.0",
        "tiers": tiers
        or {
            "core": {"title": "Core"},
            "python-library": {"title": "Python library", "includes": ["core"]},
            "static-html": {"title": "Static HTML", "includes": ["core"]},
        },
        "criteria": criteria,
    }
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _criterion(
    criterion_id: str,
    kind: str,
    *,
    blocking: bool = True,
    within: tuple[str, ...] = ("core",),
    **spec: Any,
) -> dict[str, Any]:
    return {
        "id": criterion_id,
        "title": criterion_id,
        "kind": kind,
        "blocking": blocking,
        "within": list(within),
        "spec": spec,
    }


def _make_repo(tmp_path: Path, files: dict[str, str], name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _write_manifest(repo: Path, document: dict[str, Any]) -> None:
    aii = repo / ".aii"
    aii.mkdir(parents=True, exist_ok=True)
    (aii / "readiness.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _write_scope(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """Write a scope TSV with (repo name, relative path) rows."""
    lines = ["repo\tvisibility\tlanguage\tdefault_branch\tdisk_kb\trel\tlocal\tdot_aii\tworkflows"]
    for repo_name, relative in rows:
        lines.append(f"{repo_name}\tPUBLIC\tPython\t{{'name': 'main'}}\t1\t{relative}\tY\t-\t-")
    path = tmp_path / "scope.tsv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run_cli(*args: object, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT), *(str(arg) for arg in args)]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=180,
        check=False,
    )


def _repo_args(repo: Path, standard: Path, *extra: object) -> tuple[object, ...]:
    return (
        "--repo",
        "ActiveInferenceInstitute/example",
        "--path",
        repo,
        "--standard",
        standard,
        *extra,
    )


# ---------------------------------------------------------------------------
# The five evaluation kinds
# ---------------------------------------------------------------------------


def test_file_exists_pass_and_fail(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion("CORE-README", "file-exists", paths=["README.md"]),
            _criterion("CORE-LICENSE", "file-exists", paths=["LICENSE", "LICENSE.md"]),
        ],
    )
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})

    result = _run_cli(*_repo_args(repo, standard, "--json", "--check"))
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    criteria = report["repos"]["ActiveInferenceInstitute/example"]["criteria"]
    assert criteria["CORE-README"]["status"] == "pass"
    assert criteria["CORE-LICENSE"]["status"] == "fail"


def test_file_exists_glob_and_non_empty(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [_criterion("CORE-DOC", "file-exists", paths=["docs/*.md"], non_empty=True)],
    )
    empty_repo = _make_repo(tmp_path, {"docs/empty.md": ""}, name="empty")
    filled_repo = _make_repo(tmp_path, {"docs/full.md": "content\n"}, name="filled")
    module = _load_checker()

    empty_report = module.evaluate_repo(
        module.load_standard(standard),
        "ActiveInferenceInstitute/example",
        empty_repo,
    )
    filled_report = module.evaluate_repo(
        module.load_standard(standard),
        "ActiveInferenceInstitute/example",
        filled_repo,
    )
    assert empty_report["criteria"]["CORE-DOC"]["status"] == "fail"
    assert filled_report["criteria"]["CORE-DOC"]["status"] == "pass"


def test_file_contains_reports_relative_path_and_line(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [_criterion("CORE-COMMANDS", "file-contains", paths=["README.md"], all_of=["pytest"])],
    )
    repo = _make_repo(tmp_path, {"README.md": "# Title\n\nRun with pytest.\n"})
    result = _run_cli(*_repo_args(repo, standard, "--json"))
    assert result.returncode == 0, result.stderr
    criterion = json.loads(result.stdout)["repos"]["ActiveInferenceInstitute/example"]["criteria"][
        "CORE-COMMANDS"
    ]
    assert criterion["status"] == "pass"
    assert criterion["evidence"] == "README.md:3"
    assert str(tmp_path) not in criterion["evidence"]


def test_file_contains_forbid_rejects_forbidden_pattern(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion(
                "PY-RUFF-EXTEND",
                "file-contains",
                paths=["pyproject.toml"],
                all_of=[r"extend-select\s*="],
                forbid=r"(?m)^\s*select\s*=",
            )
        ],
    )
    bad = _make_repo(
        tmp_path,
        {"pyproject.toml": '[tool.ruff]\nselect = ["E"]\nextend-select = ["B"]\n'},
        name="bad",
    )
    good = _make_repo(
        tmp_path,
        {"pyproject.toml": '[tool.ruff]\nextend-select = ["B"]\n'},
        name="good",
    )
    module = _load_checker()
    bad_report = module.evaluate_repo(
        module.load_standard(standard), "ActiveInferenceInstitute/example", bad
    )
    good_report = module.evaluate_repo(
        module.load_standard(standard), "ActiveInferenceInstitute/example", good
    )
    assert bad_report["criteria"]["PY-RUFF-EXTEND"]["status"] == "fail"
    assert good_report["criteria"]["PY-RUFF-EXTEND"]["status"] == "pass"


def test_file_contains_floor_in_ci(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion(
                "PY-PYTHON-FLOOR", "file-contains", paths=["pyproject.toml"], floor_in_ci=True
            )
        ],
    )
    files = {
        "pyproject.toml": '[project]\nrequires-python = ">=3.12"\n',
        ".github/workflows/ci.yml": "python-version: ['3.12']\n",
    }
    matching = _make_repo(tmp_path, files, name="matching")
    mismatched = _make_repo(
        tmp_path,
        {
            "pyproject.toml": '[project]\nrequires-python = ">=3.12"\n',
            ".github/workflows/ci.yml": "python-version: ['3.11']\n",
        },
        name="mismatched",
    )
    module = _load_checker()
    assert (
        module.evaluate_repo(module.load_standard(standard), "A/R", matching)["criteria"][
            "PY-PYTHON-FLOOR"
        ]["status"]
        == "pass"
    )
    assert (
        module.evaluate_repo(module.load_standard(standard), "A/R", mismatched)["criteria"][
            "PY-PYTHON-FLOOR"
        ]["status"]
        == "fail"
    )


def test_coverage_threshold_match(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion(
                "PY-COVERAGE-MATCH",
                "file-contains",
                paths=["pyproject.toml"],
                coverage_threshold_match=True,
            )
        ],
    )
    equal = _make_repo(
        tmp_path,
        {
            "pyproject.toml": "[tool.coverage.report]\nfail_under = 90\n",
            ".github/workflows/ci.yml": "run: pytest --cov-fail-under=90\n",
        },
        name="equal",
    )
    contradicting = _make_repo(
        tmp_path,
        {
            "pyproject.toml": "[tool.coverage.report]\nfail_under = 90\n",
            ".github/workflows/ci.yml": "run: pytest --cov-fail-under=89\n",
        },
        name="contradicting",
    )
    module = _load_checker()
    assert (
        module.evaluate_repo(module.load_standard(standard), "A/R", equal)["criteria"][
            "PY-COVERAGE-MATCH"
        ]["status"]
        == "pass"
    )
    assert (
        module.evaluate_repo(module.load_standard(standard), "A/R", contradicting)["criteria"][
            "PY-COVERAGE-MATCH"
        ]["status"]
        == "fail"
    )


def test_yaml_path_resolves_workflow_trigger(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion(
                "CORE-CI-ON",
                "yaml-path",
                files=[".github/workflows/*.yml"],
                require=["on.pull_request"],
            )
        ],
    )
    repo = _make_repo(
        tmp_path,
        {
            ".github/workflows/ci.yml": (
                "on: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
            )
        },
    )
    module = _load_checker()
    report = module.evaluate_repo(module.load_standard(standard), "A/R", repo)
    assert report["criteria"]["CORE-CI-ON"]["status"] == "pass"
    assert report["criteria"]["CORE-CI-ON"]["evidence"].startswith(".github/workflows/ci.yml")


def test_yaml_path_wildcard_requires_every_job_timeout(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion(
                "CORE-CI-TIMEOUT",
                "yaml-path",
                files=[".github/workflows/*.yml"],
                require=["jobs.*.timeout-minutes"],
            )
        ],
    )
    missing = _make_repo(
        tmp_path,
        {
            ".github/workflows/ci.yml": (
                "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n  b:\n"
                "    runs-on: ubuntu-latest\n    timeout-minutes: 10\n"
            )
        },
        name="missing",
    )
    complete = _make_repo(
        tmp_path,
        {
            ".github/workflows/ci.yml": (
                "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    timeout-minutes: 10\n"
                "  b:\n    runs-on: ubuntu-latest\n    timeout-minutes: 10\n"
            )
        },
        name="complete",
    )
    module = _load_checker()
    assert (
        module.evaluate_repo(module.load_standard(standard), "A/R", missing)["criteria"][
            "CORE-CI-TIMEOUT"
        ]["status"]
        == "fail"
    )
    assert (
        module.evaluate_repo(module.load_standard(standard), "A/R", complete)["criteria"][
            "CORE-CI-TIMEOUT"
        ]["status"]
        == "pass"
    )


def test_yaml_path_malformed_document_is_unavailable_not_pass(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [_criterion("CORE-CI-ON", "yaml-path", files=[".github/workflows/*.yml"], require=["on"])],
    )
    repo = _make_repo(tmp_path, {".github/workflows/ci.yml": "on: [unclosed\n"})
    module = _load_checker()
    report = module.evaluate_repo(module.load_standard(standard), "A/R", repo)
    assert report["criteria"]["CORE-CI-ON"]["status"] == "unavailable"


def test_yaml_path_dependabot_ecosystem(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion(
                "CORE-DEPENDABOT-ECOSYSTEM",
                "yaml-path",
                files=[".github/dependabot.yml"],
                ecosystems={"uv.lock": "uv", "package-lock.json": "npm"},
            )
        ],
    )
    wrong = _make_repo(
        tmp_path,
        {
            "uv.lock": "version = 1\n",
            ".github/dependabot.yml": (
                "version: 2\nupdates:\n  - package-ecosystem: pip\n    directory: /\n"
            ),
        },
        name="wrong",
    )
    right = _make_repo(
        tmp_path,
        {
            "uv.lock": "version = 1\n",
            ".github/dependabot.yml": (
                "version: 2\nupdates:\n  - package-ecosystem: uv\n    directory: /\n"
            ),
        },
        name="right",
    )
    module = _load_checker()
    assert (
        module.evaluate_repo(module.load_standard(standard), "A/R", wrong)["criteria"][
            "CORE-DEPENDABOT-ECOSYSTEM"
        ]["status"]
        == "fail"
    )
    assert (
        module.evaluate_repo(module.load_standard(standard), "A/R", right)["criteria"][
            "CORE-DEPENDABOT-ECOSYSTEM"
        ]["status"]
        == "pass"
    )


DEPENDABOT_HEADER = "version: 2\nupdates:\n"


def _dependabot(*ecosystems: str) -> str:
    return DEPENDABOT_HEADER + "".join(
        f"  - package-ecosystem: {ecosystem}\n    directory: /\n" for ecosystem in ecosystems
    )


def _ecosystem_standard(
    tmp_path: Path,
    ecosystems: dict[str, str],
    precedence: list[list[str]] | None = None,
    *,
    name: str = "tiers.yaml",
) -> Path:
    spec: dict[str, Any] = {
        "files": [".github/dependabot.yml"],
        "ecosystems": ecosystems,
    }
    if precedence is not None:
        spec["precedence"] = precedence
    return _write_standard(
        tmp_path,
        [_criterion("CORE-DEPENDABOT-ECOSYSTEM", "yaml-path", **spec)],
        name=name,
    )


def _ecosystem_status(standard: Path, repo: Path) -> dict[str, str]:
    module = _load_checker()
    report = module.evaluate_repo(module.load_standard(standard), "A/R", repo)
    return report["criteria"]["CORE-DEPENDABOT-ECOSYSTEM"]


PYTHON_LOCKS = {"uv.lock": "uv", "requirements.txt": "pip"}
PYTHON_PRECEDENCE = [["uv.lock"], ["requirements.txt"]]


def test_dependabot_ecosystem_prefers_package_manager_lockfile_over_requirements(
    tmp_path: Path,
) -> None:
    """requirements.txt is legacy: uv.lock decides when both are present."""
    standard = _ecosystem_standard(tmp_path, PYTHON_LOCKS, PYTHON_PRECEDENCE)
    files = {
        "uv.lock": "version = 1\n",
        "requirements.txt": "# generated from pyproject.toml; uv.lock is authoritative\n",
    }
    uv_watched = _make_repo(
        tmp_path, {**files, ".github/dependabot.yml": _dependabot("github-actions", "uv")}, "uv"
    )
    pip_only = _make_repo(
        tmp_path, {**files, ".github/dependabot.yml": _dependabot("github-actions", "pip")}, "pip"
    )

    watched = _ecosystem_status(standard, uv_watched)
    assert watched["status"] == "pass", watched["evidence"]
    assert "uv.lock is watched as uv" in watched["evidence"]
    assert "requirements.txt" in watched["evidence"]

    unwatched = _ecosystem_status(standard, pip_only)
    assert unwatched["status"] == "fail", unwatched["evidence"]
    assert "uv.lock needs ecosystem uv" in unwatched["evidence"]


def test_dependabot_ecosystem_requires_every_real_lockfile(tmp_path: Path) -> None:
    """package-lock.json and uv.lock are both real; npm-only must not pass."""
    ecosystems = {"uv.lock": "uv", "package-lock.json": "npm"}
    standard = _ecosystem_standard(
        tmp_path,
        ecosystems,
        [["uv.lock", "package-lock.json"]],
    )
    files = {"uv.lock": "version = 1\n", "package-lock.json": "{}\n"}
    npm_only = _make_repo(
        tmp_path, {**files, ".github/dependabot.yml": _dependabot("npm")}, "npm-only"
    )
    both = _make_repo(
        tmp_path, {**files, ".github/dependabot.yml": _dependabot("uv", "npm")}, "both"
    )

    unwatched = _ecosystem_status(standard, npm_only)
    assert unwatched["status"] == "fail", unwatched["evidence"]
    assert "uv.lock needs ecosystem uv" in unwatched["evidence"]

    watched = _ecosystem_status(standard, both)
    assert watched["status"] == "pass", watched["evidence"]
    assert "package-lock.json is watched as npm" in watched["evidence"]
    assert "uv.lock is watched as uv" in watched["evidence"]


def test_dependabot_ecosystem_single_lockfile_repository(tmp_path: Path) -> None:
    """A repository with one lockfile still requires exactly that ecosystem."""
    standard = _ecosystem_standard(tmp_path, PYTHON_LOCKS, PYTHON_PRECEDENCE)
    requirements_only = _make_repo(
        tmp_path,
        {
            "requirements.txt": "numpy\n",
            ".github/dependabot.yml": _dependabot("pip"),
        },
        "requirements",
    )
    dropped = _make_repo(
        tmp_path,
        {
            "requirements.txt": "numpy\n",
            ".github/dependabot.yml": _dependabot("uv"),
        },
        "requirements-uv",
    )
    uv_only = _make_repo(
        tmp_path,
        {"uv.lock": "version = 1\n", ".github/dependabot.yml": _dependabot("uv")},
        "uv",
    )

    assert _ecosystem_status(standard, requirements_only)["status"] == "pass"
    assert _ecosystem_status(standard, dropped)["status"] == "fail"
    assert _ecosystem_status(standard, uv_only)["status"] == "pass"


@pytest.mark.parametrize(
    "order",
    [
        ("uv.lock", "requirements.txt"),
        ("requirements.txt", "uv.lock"),
    ],
)
def test_dependabot_ecosystem_never_selects_by_sort_or_declaration_order(
    tmp_path: Path, order: tuple[str, str]
) -> None:
    """Selection follows precedence, so neither alphabetical nor file order decides."""
    ecosystems = {lockfile: PYTHON_LOCKS[lockfile] for lockfile in order}
    standard = _ecosystem_standard(tmp_path, ecosystems, PYTHON_PRECEDENCE)
    repo = _make_repo(
        tmp_path,
        {
            "uv.lock": "version = 1\n",
            "requirements.txt": "# legacy\n",
            ".github/dependabot.yml": _dependabot("pip"),
        },
        "repo-" + "-".join(order),
    )

    result = _ecosystem_status(standard, repo)
    assert result["status"] == "fail", (order, result["evidence"])
    assert "uv.lock needs ecosystem uv" in result["evidence"]


@pytest.mark.parametrize(
    "spec",
    [
        {"ecosystems": PYTHON_LOCKS, "precedence": [["uv.lock"], ["unknown.lock"]]},
        {"ecosystems": PYTHON_LOCKS, "precedence": [["uv.lock"]]},
        {"ecosystems": PYTHON_LOCKS, "precedence": [["uv.lock"], ["uv.lock", "requirements.txt"]]},
        {"ecosystems": PYTHON_LOCKS, "precedence": []},
        {"ecosystems": PYTHON_LOCKS, "precedence": [["uv.lock"], ["requirements.txt"], []]},
        {"ecosystems": {"uv.lock": ""}, "precedence": None},
    ],
)
def test_dependabot_ecosystem_precedence_is_validated(tmp_path: Path, spec: dict[str, Any]) -> None:
    """A precedence that does not cover the declared lockfiles exactly is exit 2."""
    criterion_spec = {key: value for key, value in spec.items() if value is not None}
    standard = _write_standard(
        tmp_path,
        [
            _criterion(
                "CORE-DEPENDABOT-ECOSYSTEM", "yaml-path", files=["dependabot.yml"], **criterion_spec
            )
        ],
    )
    module = _load_checker()
    with pytest.raises(module.ConfigError):
        module.load_standard(standard)


def test_command_pass_fail_and_missing_tool(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion("CMD-TRUE", "command", command=["true"]),
            _criterion("CMD-FALSE", "command", command=["false"]),
            _criterion(
                "CMD-MISSING",
                "command",
                command=["definitely-not-an-installed-tool-xyz", "--version"],
            ),
        ],
    )
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})
    result = _run_cli(*_repo_args(repo, standard, "--json", "--check"))
    assert result.returncode == 3, result.stderr
    criteria = json.loads(result.stdout)["repos"]["ActiveInferenceInstitute/example"]["criteria"]
    assert criteria["CMD-TRUE"]["status"] == "pass"
    assert criteria["CMD-FALSE"]["status"] == "fail"
    assert criteria["CMD-MISSING"]["status"] == "unavailable"
    assert criteria["CMD-MISSING"]["status"] != "pass"


def test_github_api_without_flag_is_unavailable_not_pass(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion(
                "CORE-INHERIT-SECURITY",
                "github-api",
                blocking=True,
                endpoints=["repos/{repo}/contents/SECURITY.md"],
            )
        ],
    )
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})
    module = _load_checker()

    report = module.evaluate_repo(module.load_standard(standard), "A/R", repo)
    assert report["criteria"]["CORE-INHERIT-SECURITY"]["status"] == "unavailable"
    assert report["criteria"]["CORE-INHERIT-SECURITY"]["status"] != "pass"

    result = _run_cli(*_repo_args(repo, standard, "--json", "--check"))
    assert result.returncode == 3, result.stderr
    criterion = json.loads(result.stdout)["repos"]["ActiveInferenceInstitute/example"]["criteria"][
        "CORE-INHERIT-SECURITY"
    ]
    assert criterion["status"] == "unavailable"


def test_github_api_with_fake_client(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self, statuses: dict[str, int]) -> None:
            self.statuses = statuses

        def get(self, endpoint: str) -> int:
            return self.statuses.get(endpoint, 404)

    standard = _write_standard(
        tmp_path,
        [
            _criterion(
                "CORE-INHERIT-SECURITY",
                "github-api",
                endpoints=["repos/{repo}/contents/SECURITY.md"],
            )
        ],
    )
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})
    module = _load_checker()
    loaded = module.load_standard(standard)

    passing = module.evaluate_repo(
        loaded,
        "A/R",
        repo,
        github=True,
        github_client=FakeClient({"repos/A/R/contents/SECURITY.md": 200}),
    )
    missing = module.evaluate_repo(loaded, "A/R", repo, github=True, github_client=FakeClient({}))
    forbidden = module.evaluate_repo(
        loaded,
        "A/R",
        repo,
        github=True,
        github_client=FakeClient({"repos/A/R/contents/SECURITY.md": 403}),
    )
    assert passing["criteria"]["CORE-INHERIT-SECURITY"]["status"] == "pass"
    assert missing["criteria"]["CORE-INHERIT-SECURITY"]["status"] == "fail"
    assert forbidden["criteria"]["CORE-INHERIT-SECURITY"]["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_two_json_runs_are_byte_identical(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion("CORE-README", "file-exists", paths=["README.md"]),
            _criterion("CORE-GITIGNORE", "file-exists", paths=[".gitignore"]),
        ],
    )
    repo_a = _make_repo(tmp_path, {"README.md": "a\n", ".gitignore": "*.pyc\n"}, name="repo-a")
    repo_b = _make_repo(tmp_path, {"README.md": "b\n"}, name="repo-b")
    scope = _write_scope(tmp_path, [("zeta", "repo-b"), ("alpha", "repo-a")])

    first = _run_cli(
        "--all", "--scope", scope, "--root", tmp_path, "--standard", standard, "--json"
    )
    second = _run_cli(
        "--all", "--scope", scope, "--root", tmp_path, "--standard", standard, "--json"
    )
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    assert str(tmp_path) not in first.stdout
    assert str(repo_a) not in first.stdout
    assert str(repo_b) not in first.stdout

    report = json.loads(first.stdout)
    assert list(report["repos"]) == [
        "ActiveInferenceInstitute/alpha",
        "ActiveInferenceInstitute/zeta",
    ]
    canonical = json.dumps(report, indent=2, sort_keys=True) + "\n"
    assert first.stdout == canonical


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def test_exit_code_zero_when_all_blocking_pass(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion("CORE-README", "file-exists", paths=["README.md"]),
            _criterion("CORE-DOC", "file-exists", paths=["missing.md"], blocking=False),
        ],
    )
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})
    result = _run_cli(*_repo_args(repo, standard, "--check"))
    assert result.returncode == 0, result.stderr


def test_exit_code_one_when_blocking_fails(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path, [_criterion("CORE-README", "file-exists", paths=["README.md"])]
    )
    repo = _make_repo(tmp_path, {".gitignore": "*.pyc\n"})
    result = _run_cli(*_repo_args(repo, standard, "--check"))
    assert result.returncode == 1, result.stderr


def test_exit_code_three_when_blocking_is_unavailable(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion("CORE-README", "file-exists", paths=["README.md"]),
            _criterion(
                "CMD-MISSING",
                "command",
                command=["definitely-not-an-installed-tool-xyz"],
            ),
        ],
    )
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})
    result = _run_cli(*_repo_args(repo, standard, "--check"))
    assert result.returncode == 3, result.stderr


def test_report_mode_exits_zero_and_still_records_failures(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path, [_criterion("CORE-README", "file-exists", paths=["README.md"])]
    )
    repo = _make_repo(tmp_path, {".gitignore": "*.pyc\n"})
    result = _run_cli(*_repo_args(repo, standard, "--json"))
    assert result.returncode == 0, result.stderr
    criterion = json.loads(result.stdout)["repos"]["ActiveInferenceInstitute/example"]["criteria"][
        "CORE-README"
    ]
    assert criterion["status"] == "fail"


def test_exit_code_two_on_usage_error(tmp_path: Path) -> None:
    standard = _write_standard(tmp_path, [])
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})

    missing_path = _run_cli(
        "--repo", "ActiveInferenceInstitute/example", "--standard", standard, "--check"
    )
    assert missing_path.returncode == 2, missing_path.stderr

    nonexistent = _run_cli(
        "--repo",
        "ActiveInferenceInstitute/example",
        "--path",
        tmp_path / "does-not-exist",
        "--standard",
        standard,
        "--check",
    )
    assert nonexistent.returncode == 2, nonexistent.stderr

    nothing_selected = _run_cli("--standard", standard)
    assert nothing_selected.returncode == 2

    del repo


def test_unknown_kind_is_a_hard_error_not_a_skip(tmp_path: Path) -> None:
    standard = _write_standard(tmp_path, [_criterion("BOGUS", "telepathy", paths=["README.md"])])
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})
    result = _run_cli(*_repo_args(repo, standard, "--json", "--check"))
    assert result.returncode == 2, result.stderr
    assert "telepathy" in result.stderr
    assert "unknown" in result.stderr.lower()


def test_unknown_kind_in_shipped_standard_is_never_skipped(tmp_path: Path) -> None:
    document = {
        "schema": "aii-readiness-standard/v1",
        "version": "1.0.0",
        "tiers": {"core": {"title": "Core"}},
        "criteria": [
            {
                "id": "BOGUS",
                "title": "Bogus",
                "kind": "not-a-kind",
                "blocking": True,
                "within": ["core"],
                "spec": {},
            }
        ],
    }
    path = tmp_path / "bogus.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    module = _load_checker()
    with pytest.raises(module.ConfigError):
        module.load_standard(path)


# ---------------------------------------------------------------------------
# Waivers
# ---------------------------------------------------------------------------


def test_waiver_with_reason_turns_fail_into_waived(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path, [_criterion("CORE-README", "file-exists", paths=["README.md"])]
    )
    repo = _make_repo(tmp_path, {".gitignore": "*.pyc\n"})
    _write_manifest(
        repo,
        {
            "schema": "aii-readiness/v1",
            "tier": "core",
            "waivers": [{"criterion": "CORE-README", "reason": "generated by the org"}],
        },
    )
    result = _run_cli(*_repo_args(repo, standard, "--json", "--check"))
    assert result.returncode == 0, result.stderr
    criterion = json.loads(result.stdout)["repos"]["ActiveInferenceInstitute/example"]["criteria"][
        "CORE-README"
    ]
    assert criterion["status"] == "waived"
    assert "generated by the org" in criterion["evidence"]


def test_waiver_without_reason_is_an_error(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path, [_criterion("CORE-README", "file-exists", paths=["README.md"])]
    )
    repo = _make_repo(tmp_path, {".gitignore": "*.pyc\n"})
    _write_manifest(
        repo,
        {
            "schema": "aii-readiness/v1",
            "tier": "core",
            "waivers": [{"criterion": "CORE-README"}],
        },
    )
    result = _run_cli(*_repo_args(repo, standard, "--json", "--check"))
    assert result.returncode == 2, result.stderr
    assert "reason" in result.stderr.lower()


def test_waiver_with_blank_reason_is_an_error(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path, [_criterion("CORE-README", "file-exists", paths=["README.md"])]
    )
    repo = _make_repo(tmp_path, {".gitignore": "*.pyc\n"})
    _write_manifest(
        repo,
        {
            "schema": "aii-readiness/v1",
            "tier": "core",
            "waivers": [{"criterion": "CORE-README", "reason": "   "}],
        },
    )
    result = _run_cli(*_repo_args(repo, standard, "--json", "--check"))
    assert result.returncode == 2, result.stderr
    assert "reason" in result.stderr.lower()


def test_expired_waiver_does_not_apply(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path, [_criterion("CORE-README", "file-exists", paths=["README.md"])]
    )
    repo = _make_repo(tmp_path, {".gitignore": "*.pyc\n"})
    _write_manifest(
        repo,
        {
            "schema": "aii-readiness/v1",
            "tier": "core",
            "waivers": [
                {"criterion": "CORE-README", "reason": "temporary", "expires": "2020-01-01"}
            ],
        },
    )
    result = _run_cli(*_repo_args(repo, standard, "--json", "--check", "--today", "2026-11-07"))
    assert result.returncode == 1, result.stderr
    criterion = json.loads(result.stdout)["repos"]["ActiveInferenceInstitute/example"]["criteria"][
        "CORE-README"
    ]
    assert criterion["status"] == "fail"


def test_waiver_for_unknown_criterion_is_an_error(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path, [_criterion("CORE-README", "file-exists", paths=["README.md"])]
    )
    repo = _make_repo(tmp_path, {".gitignore": "*.pyc\n"})
    _write_manifest(
        repo,
        {
            "schema": "aii-readiness/v1",
            "tier": "core",
            "waivers": [{"criterion": "NOPE", "reason": "typo"}],
        },
    )
    result = _run_cli(*_repo_args(repo, standard, "--json"))
    assert result.returncode == 2, result.stderr
    assert "NOPE" in result.stderr


# ---------------------------------------------------------------------------
# Tier applicability and classification
# ---------------------------------------------------------------------------


def test_criterion_outside_selected_tier_is_not_applicable(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion("CORE-README", "file-exists", paths=["README.md"]),
            _criterion(
                "PY-UVLOCK",
                "file-exists",
                paths=["uv.lock"],
                within=("python-library",),
            ),
        ],
    )
    repo = _make_repo(tmp_path, {"README.md": "hello\n", "index.html": "<html></html>\n"})
    _write_manifest(repo, {"schema": "aii-readiness/v1", "tier": "static-html"})
    result = _run_cli(*_repo_args(repo, standard, "--json"))
    assert result.returncode == 0, result.stderr
    criteria = json.loads(result.stdout)["repos"]["ActiveInferenceInstitute/example"]["criteria"]
    assert criteria["PY-UVLOCK"]["status"] == "not-applicable"
    assert criteria["CORE-README"]["status"] == "pass"


def test_manifest_tier_is_reported_and_used(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path,
        [
            _criterion("CORE-README", "file-exists", paths=["README.md"]),
            _criterion("PY-UVLOCK", "file-exists", paths=["uv.lock"], within=("python-library",)),
        ],
    )
    repo = _make_repo(tmp_path, {"README.md": "hello\n", "uv.lock": "version = 1\n"})
    _write_manifest(repo, {"schema": "aii-readiness/v1", "tier": "python-library"})
    result = _run_cli(*_repo_args(repo, standard, "--json"))
    report = json.loads(result.stdout)["repos"]["ActiveInferenceInstitute/example"]
    assert report["tier"] == "python-library"
    assert report["tier_source"] == "manifest"
    assert report["criteria"]["PY-UVLOCK"]["status"] == "pass"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_classification_uses_tracked_files(tmp_path: Path) -> None:
    module = _load_checker()
    python_repo = _make_repo(
        tmp_path,
        {"pyproject.toml": "[project]\nname = 'x'\n", "x.py": "print(1)\n"},
        name="python",
    )
    _git(python_repo, "init", "-q")
    _git(python_repo, "add", ".")
    assert module.classify_tier(python_repo) == "python-library"

    bun_repo = _make_repo(
        tmp_path,
        {
            "package.json": '{"name": "x", "scripts": {"test": "bun test"}}\n',
            "bun.lock": "",
            "index.ts": "export {};\n",
        },
        name="bun",
    )
    _git(bun_repo, "init", "-q")
    _git(bun_repo, "add", ".")
    assert module.classify_tier(bun_repo) == "typescript-bun"


def test_missing_working_copy_is_unavailable_in_all_mode(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path, [_criterion("CORE-README", "file-exists", paths=["README.md"])]
    )
    scope = _write_scope(tmp_path, [("ghost", "does-not-exist")])
    result = _run_cli(
        "--all", "--scope", scope, "--root", tmp_path, "--standard", standard, "--json"
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    row = report["repos"]["ActiveInferenceInstitute/ghost"]
    assert row["criteria"]["CORE-README"]["status"] == "unavailable"

    checked = _run_cli(
        "--all", "--scope", scope, "--root", tmp_path, "--standard", standard, "--check"
    )
    assert checked.returncode == 3


# ---------------------------------------------------------------------------
# Tier exclusions
# ---------------------------------------------------------------------------


def _excluded_standard(tmp_path: Path, *, reason: str = "workflow_call-only standard") -> Path:
    tiers = {
        "core": {"title": "Core"},
        "org": {
            "title": "Org",
            "includes": ["core"],
            "excludes": [{"criterion": "CORE-CI-ON", "reason": reason}],
        },
    }
    return _write_standard(
        tmp_path,
        [
            _criterion(
                "CORE-CI-ON",
                "yaml-path",
                files=[".github/workflows/*.yml"],
                match="any",
                require=["on.pull_request", "on.push"],
                on_empty="fail",
            )
        ],
        tiers=tiers,
    )


def test_tier_exclusion_reports_not_applicable_with_its_reason(tmp_path: Path) -> None:
    standard = _excluded_standard(tmp_path)
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})
    _write_manifest(repo, {"schema": "aii-readiness/v1", "tier": "org"})

    result = _run_cli(*_repo_args(repo, standard, "--json"))
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)["repos"]["ActiveInferenceInstitute/example"]["criteria"][
        "CORE-CI-ON"
    ]
    assert entry["status"] == "not-applicable"
    assert "workflow_call-only standard" in entry["evidence"]

    checked = _run_cli(*_repo_args(repo, standard, "--check"))
    assert checked.returncode == 0, checked.stderr


def test_tier_exclusion_does_not_apply_to_a_sibling_tier(tmp_path: Path) -> None:
    standard = _excluded_standard(tmp_path)
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})
    _write_manifest(repo, {"schema": "aii-readiness/v1", "tier": "core"})

    result = _run_cli(*_repo_args(repo, standard, "--json"))
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)["repos"]["ActiveInferenceInstitute/example"]["criteria"][
        "CORE-CI-ON"
    ]
    assert entry["status"] == "fail"


def test_tier_exclusion_requires_a_non_empty_reason(tmp_path: Path) -> None:
    module = _load_checker()
    standard = _excluded_standard(tmp_path, reason="   ")
    with pytest.raises(module.ConfigError):
        module.load_standard(standard)


def test_tier_exclusion_of_an_unknown_criterion_is_an_error(tmp_path: Path) -> None:
    module = _load_checker()
    tiers = {
        "core": {"title": "Core"},
        "org": {
            "title": "Org",
            "includes": ["core"],
            "excludes": [{"criterion": "CORE-NOT-A-CRITERION", "reason": "typo"}],
        },
    }
    standard = _write_standard(
        tmp_path,
        [_criterion("CORE-README", "file-exists", paths=["README.md"])],
        tiers=tiers,
    )
    with pytest.raises(module.ConfigError):
        module.load_standard(standard)


def test_shipped_org_tier_excludes_core_ci_on_and_only_there() -> None:
    module = _load_checker()
    standard = module.load_standard(SHIPPED_STANDARD)
    org_exclusions = standard.exclusions("org")
    assert "CORE-CI-ON" in org_exclusions
    assert org_exclusions["CORE-CI-ON"].strip()
    for other_tier in ("core", "python-library", "static-html"):
        assert "CORE-CI-ON" not in standard.exclusions(other_tier), other_tier


# ---------------------------------------------------------------------------
# The shipped catalogue
# ---------------------------------------------------------------------------


def test_shipped_standard_declares_every_tier_and_known_kinds() -> None:
    module = _load_checker()
    standard = module.load_standard(SHIPPED_STANDARD)
    expected_tiers = {
        "core",
        "python-library",
        "python-service",
        "typescript-node",
        "typescript-bun",
        "static-html",
        "julia",
        "notebook",
        "content-data",
        "org",
    }
    assert expected_tiers <= set(standard.tiers)
    assert len(standard.criteria) >= 70
    for criterion in standard.criteria:
        assert criterion.kind in module.KNOWN_KINDS, criterion.id
        assert criterion.within, criterion.id
        for tier in criterion.within:
            assert tier in standard.tiers, (criterion.id, tier)


def test_shipped_standard_contains_the_catalogue_ids() -> None:
    module = _load_checker()
    standard = module.load_standard(SHIPPED_STANDARD)
    identifiers = {criterion.id for criterion in standard.criteria}
    required = {
        "CORE-README",
        "CORE-READINESS",
        "CORE-ACTIONLINT",
        "CORE-INHERIT-CONTRIBUTING",
        "CORE-INHERIT-SECURITY",
        "CORE-INHERIT-CODEOWNERS",
        "PY-PYPROJECT",
        "PY-UVLOCK",
        "PY-COVERAGE-RUN",
        "PY-COVERAGE-MATCH",
        "PYSVC-HEALTH",
        "TS-TSCONFIG",
        "BUN-LOCK",
        "HTML-VALIDATE",
        "HTML-NO-COVERAGE-GATE",
        "JL-TEST-CI",
        "NB-EXCLUDED-FROM-TYPECHECK",
        "CD-LINKS",
        "CD-NO-TESTFRAMEWORK-REQUIRED",
        "ORG-COMMUNITY-HEALTH",
        "ORG-CODEOWNERS",
        "ORG-FUNDING",
        "ORG-TIERS-YAML",
        "ORG-CHECKER-DETERMINISTIC",
        "ORG-CHECKER-TESTS",
        "ORG-CHECKER-EXIT-CODES",
        "ORG-REUSABLE-WORKFLOWS",
        "ORG-LEDGER-PUBLIC-ONLY",
        "ORG-STANDARD-README",
    }
    assert required <= identifiers


def test_shipped_catalogue_documents_lockfile_precedence() -> None:
    """The catalogue, not Python, states that legacy requirements.txt ranks last."""
    module = _load_checker()
    standard = module.load_standard(SHIPPED_STANDARD)
    spec = standard.criteria_by_id["CORE-DEPENDABOT-ECOSYSTEM"].spec
    ecosystems = spec["ecosystems"]
    precedence = spec["precedence"]

    flattened = [lockfile for group in precedence for lockfile in group]
    assert sorted(flattened) == sorted(ecosystems), "precedence must cover every lockfile once"
    assert precedence[-1] == ["requirements.txt"], precedence[-1]
    package_manager_group = set(precedence[0])
    assert {
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "package-lock.json",
        "bun.lock",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Manifest.toml",
    } <= package_manager_group


def test_shipped_standard_waiverless_unavailable_never_passes(tmp_path: Path) -> None:
    """No github-api criterion in the shipped catalogue may resolve to pass offline."""
    module = _load_checker()
    standard = module.load_standard(SHIPPED_STANDARD)
    repo = _make_repo(
        tmp_path,
        {
            "README.md": "hello\n",
            "AGENTS.md": "commands\n",
            "LICENSE": "MIT\n",
            ".gitignore": "*.pyc\n",
            "uv.lock": "version = 1\n",
        },
    )
    _write_manifest(repo, {"schema": "aii-readiness/v1", "tier": "python-library"})
    report = module.evaluate_repo(standard, "A/R", repo)
    for criterion_id, result in report["criteria"].items():
        criterion = standard.criteria_by_id[criterion_id]
        if criterion.kind == "github-api":
            assert result["status"] == "unavailable", criterion_id


def test_unknown_tier_in_manifest_is_an_error(tmp_path: Path) -> None:
    standard = _write_standard(
        tmp_path, [_criterion("CORE-README", "file-exists", paths=["README.md"])]
    )
    repo = _make_repo(tmp_path, {"README.md": "hello\n"})
    _write_manifest(repo, {"schema": "aii-readiness/v1", "tier": "made-up-tier"})
    result = _run_cli(*_repo_args(repo, standard, "--json"))
    assert result.returncode == 2, result.stderr
    assert "made-up-tier" in result.stderr
