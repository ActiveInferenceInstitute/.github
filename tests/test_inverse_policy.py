"""The shipped inverse criteria reject a violation in any admitted peer."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_check_readiness import SHIPPED_STANDARD, _load_checker, _make_repo


@pytest.mark.parametrize(
    ("criterion_id", "paths", "clean", "violation"),
    [
        (
            "HTML-NO-COVERAGE-GATE",
            (".github/workflows/a.yml", ".github/workflows/z.yaml"),
            "run: echo clean\n",
            "run: pytest --cov-fail-under=90\n",
        ),
        (
            "CD-NO-TESTFRAMEWORK-REQUIRED",
            ("requirements-a.txt", "requirements-z.txt"),
            "requests\n",
            "pytest\n",
        ),
    ],
)
@pytest.mark.parametrize("scenario", ["violation-first", "violation-last", "all-clean", "empty"])
def test_shipped_inverse_policy_checks_every_peer(
    tmp_path: Path,
    criterion_id: str,
    paths: tuple[str, str],
    clean: str,
    violation: str,
    scenario: str,
) -> None:
    files = {} if scenario == "empty" else dict.fromkeys(paths, clean)
    violating_path = None
    if scenario in ("violation-first", "violation-last"):
        violating_path = paths[0] if scenario == "violation-first" else paths[1]
        files[violating_path] = violation
    repo = _make_repo(tmp_path, files)
    module = _load_checker()
    criterion = module.load_standard(SHIPPED_STANDARD).criteria_by_id[criterion_id]
    context = module.EvalContext(
        repo_id="ActiveInferenceInstitute/example",
        repo_path=repo,
        org="ActiveInferenceInstitute",
        github=False,
        github_client=None,
        default_branch="main",
        index=module.build_repo_index(repo),
    )
    result = module.evaluate_criterion(criterion, context)
    if scenario == "empty":
        expected = "not-applicable"
    elif violating_path is not None:
        expected = "fail"
    else:
        expected = "pass"
    assert result.status == expected, result.evidence
    if violating_path is not None:
        assert result.evidence.startswith(violating_path + ":")
    assert criterion.blocking is False
