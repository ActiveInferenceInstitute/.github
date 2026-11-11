# InstituteOS integration — .github

## Identity

| Field | Value |
| --- | --- |
| Repository | `ActiveInferenceInstitute/.github` |
| Affiliation | `institute` |
| Category | `organization` |
| Status | `active` |
| Maturity | `stable` |
| Steward | Active Inference Institute |
| Default branch | `main` |
| Current release | — |
| License | `MIT` |
| Citation | Active Inference Institute — .github. |

## Federation

| Setting | Value |
| --- | --- |
| Dashboard mode | `repos` |
| Analytics | `true` |
| Dashboard | `true` |
| Sync policy | `pull` |
| Registries | — |
| Tags | organization, community-health, github-profile, templates |

## Ecosystem relations

| Repository | Relation | Note |
| --- | --- | --- |
| `ActiveInferenceInstitute/InstituteOS` | `referenced-by` | Federated via the InstituteOS .aii manifest. |

## Key artifacts

| Path | Kind | Description |
| --- | --- | --- |
| `README.md` | `doc` | Repository overview. |
| `pyproject.toml` | `code` | Python package config. |
| `LICENSE` | `doc` | License. |
| `profile/README.md` | `doc` | Repository artifact. |
| `AGENTS.md` | `doc` | Repository artifact. |
| `CONTRIBUTING.md` | `doc` | Repository artifact. |
| `CODE_OF_CONDUCT.md` | `doc` | Repository artifact. |
| `SECURITY.md` | `doc` | Repository artifact. |
| `SUPPORT.md` | `doc` | Repository artifact. |
| `PULL_REQUEST_TEMPLATE.md` | `doc` | Repository artifact. |
| `tests` | `test` | Test suite. |

## Portable tasks

| Task | Command | Working directory | Description |
| --- | --- | --- | --- |
| `test` | `python -m pytest` | `.` | Run the test suite. |
| `inventory` | `git ls-files` | `.` | List tracked organization files. |

## Validation

```bash
python -m aii_platform.aii_sidecar.validate <repo> --completeness
python -m aii_platform.aii_sidecar docs <repo> --overwrite
python -m aii_platform.aii_sidecar.run <repo> --list
```

The sidecar is valid when `validate` prints `OK` and reports no missing artifact,
manifest, link, or declared-doc path. Completeness is a curation target, not a
runtime requirement.

## Data boundary

This sidecar carries metadata only. It does not grant execution permission, does not
replace the repository's own documentation, and must not be used to move private
content into a public repository. Task execution is limited to trusted checkouts.
