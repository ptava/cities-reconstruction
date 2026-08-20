# Ongoing Development Plan

This document is the durable development roadmap for the baseline Python and pipeline improvements. Read it together with the live checkout and `README.md`; when they disagree, verify the implementation and update this document rather than assuming this snapshot is current.

## How to use and maintain this plan

At the start of a development session:

1. Read `AGENTS.md`, `README.md`, and this document before choosing work.
2. Verify the current branch, commit, working tree, tests, configuration, and relevant source paths.
3. Resume from **Immediate next checkpoint** unless the user explicitly changes priorities.

Before ending a development session that changes code or this roadmap:

1. Update the source-of-truth commit and verification baseline below.
2. Update every affected priority status and checkpoint.
3. Record the next concrete starting point and any unresolved decision.
4. Keep code, tests, `README.md`, and this plan aligned in the same reviewable commit where practical.
5. Present the final diff and commit meaning for approval, then prompt separately before pushing.

Do not silently reorder the baseline priorities. If implementation dependencies require a different local sequence, document the reason explicitly.

## Current checkpoint

- Last updated: 2026-08-20.
- Baseline captured after: `b88813a refactor: make stage registry own runtime dispatch` on `main` and `origin/main`.
- The live checkout and Git history remain the source of truth; do not expect this document to contain the hash of the commit that updates the document itself.
- Current change: stable stage identity and unique derived output numbering are implemented and approved for commit. `.codegraph/` remains intentionally untracked.
- Verification baseline for the current review slice: Ruff clean; configured mypy clean across 8 source files; full-package mypy clean across 24 source files; supplemental-planning migration audit clean; 401 tests passed; branch coverage 85.42%; `git diff --check` clean.

## Baseline priorities and status

| Priority | Improvement | Status | Current state and remaining work |
| ---: | --- | --- | --- |
| 1 | Authoritative pipeline model | Partial | `StageId` and the dependency-neutral layout catalogue own identity/order/path derivation; `StageSpec` owns operational metadata, planners, typed runners, and CLI dispatch. Add a novice-friendly dependency-aware end-to-end `run` command. |
| 2 | Stable stage identity and unique output numbering | Complete | `StageId` is independent of order, and `number_name` derives unique `01` through `07` directories from the stored stage identity and sequence number without hard-coded numbered folder strings. |
| 3 | Break up god modules | Not started | `shapefiles.py` is 3,974 lines, `point_cloud.py` 2,093, and `city_models.py` 2,060. Preserve public `plan()`/`run()` facades while extracting focused modules. |
| 4 | Shared stage contracts | Complete | All six executable stages publish schema-version-2 manifests using shared status, manifest, output, provenance, artifact-reference, and consumer-validation rules. |
| 5 | Transactional output handling | Partial | Manifest-last is universal. Locks and atomic artifact writers are not yet universal in shapefiles, trees, and visual enrichment. |
| 6 | Declarative uniformly typed CLI | Partial | Registry dispatch and immutable CLI-independent `StageRunOptions` exist. Stage-focused argument registration and one application exception hierarchy remain. |
| 7 | Python quality gates | Partial/advanced | Ruff, mypy, branch coverage, migration audit, and documented commands exist. CI, optional pre-commit, expanded configured scope, and stronger validated boundary types remain. |
| 8 | Central geospatial transformations | Not started | EPSG:25832 conversion remains duplicated across five stage modules. Add a shared CRS adapter and evaluate maintained readers separately. |
| 9 | Tests independent from mutable demonstration assets | Partial | The Mercato AP-007 mismatch is fixed, but behavioral tests still read a mutable documentation asset. Add immutable `tests/data/` fixtures and retain separate canonical-asset tests. |
| 10 | README operational truth | Complete and continuous | The stage-status table and limitations are current. Keep README, code, tests, and graphical QA instructions aligned after every change. |

## Dependency-adjusted execution order

Priority 1 remains the highest-level objective, but its automatic runner requires unambiguous stage layouts. Complete priority 2 first, then finish priority 1.

### Checkpoint 1: Stable stage identity and layout

Status: complete in the current change.

- Introduce a typed stable stage identifier independent of numeric order.
- Put static stage layout metadata in a dependency-neutral catalogue.
- Make planners, runners, and consumers derive output paths from that catalogue.
- Derive every stage path through the catalogue.
- Add registry consistency and uniqueness tests.
- Combined review-slice commit: `refactor: derive unique pipeline stage directories`.

### Checkpoint 2: Unique output directories

Status: complete in the same change at the user's direction. Combining the checkpoints ensures no intermediate design retains hard-coded numbered folder names.

- Assign unique presentation directories:

  ```text
  01_shapefiles
  02_visual_enrichment
  03_point_cloud
  04_city_models
  05_trees
  06_air_purifiers
  07_openfoam
  ```

- Update all producers, consumers, tests, examples, maintained snapshots, and README references.
- Use a documented clean break: old directories are left untouched and users rerun stages under the new names. Backward compatibility is not assumed.
- Never delete existing user outputs automatically.
- Verify no duplicate or obsolete hard-coded paths remain.
- Combined review-slice commit: `refactor: derive unique pipeline stage directories`.

### Checkpoint 3: Finish the authoritative pipeline model

- Add a novice-friendly `run` command driven by registry dependencies.
- Respect explicit inputs that replace default producers.
- Do not implicitly run review-only, incomplete, or planned stages.
- Print the resolved execution plan, stop on required-stage failure, aggregate typed results, and return consistent exit codes.
- Derive operational validation and documentation from registry metadata where practical.
- Proposed commit: `feat: add dependency-aware pipeline execution`.
- Mark priority 1 complete after this checkpoint; priority 2 remains complete once the current review slice is committed.

### Checkpoint 4: Decompose large stage modules

- Use several behavior-preserving commits: extract HTML/report rendering first, then diagnostics/publication, input adapters/parsers, and domain transformations/validation.
- Work through shapefiles, point-cloud, and City4CFD separately; never combine all three into one commit.
- Keep each stage's public `plan()` and `run()` facade stable.

### Checkpoint 5: Complete transactional publication

- Apply `lock -> invalidate stale manifest -> validate -> atomic artifacts -> report/preview -> manifest last` to every executable stage.
- Add locking and atomic writes to shapefiles, trees, and visual enrichment.
- Ensure interrupted runs never leave a trusted manifest.
- Assess directory-level promotion only after universal file-level atomicity.
- Proposed commit: `refactor: make stage artifact publication transactional`.

### Checkpoint 6: Finish the declarative CLI

- Add focused per-stage argument registration and validation derived from the registry.
- Remove unrelated options from a single shared parser surface.
- Introduce one application-level exception hierarchy and uniform human/JSON error behavior.
- Use separate commits for parser declaration and error unification if review scope becomes large.

### Checkpoint 7: Complete quality infrastructure

- Add CI running Ruff, mypy, full pytest branch coverage, and the migration audit.
- Expand configured Ruff/mypy scope toward the full package.
- Add optional pre-commit only after CI commands are stable.
- Type validated external-data boundaries incrementally.
- Reassess the 70% coverage floor against the maintained 85% result rather than raising it blindly.

### Checkpoint 8: Centralize geospatial transformations

- Add a shared CRS adapter and remove duplicated EPSG:25832 transformations.
- Centralize supported-CRS validation and keep stages independent of transformation internals.
- Evaluate `rasterio`/`pyogrio` in a separate change; do not combine wholesale reader replacement with the CRS refactor.

### Checkpoint 9: Isolate behavioral test fixtures

- Add immutable minimal fixtures under `tests/data/`.
- Use them for behavioral tests and retain separate explicit tests for canonical Florence/Mercato assets.
- Proposed commit: `test: isolate behavioral fixtures from demo assets`.

### Checkpoint 10: Maintain operational documentation

- Update README with each behavior, directory, command, or maturity change.
- Keep unresolved external-tool and domain decisions visible as top-level TODOs.
- Never describe automatic end-to-end execution or OpenFOAM generation as available before implementation.

## Required checkpoint discipline

For every checkpoint:

1. Implement one focused feature.
2. Run focused tests during development.
3. Run Ruff, configured and full-package mypy, full pytest with branch coverage, the migration audit, and `git diff --check`.
4. Present the final diff and explain the commit meaning.
5. Wait for approval before committing.
6. Announce that it is time to push after the commit.
7. Push only after authorization.
8. Update this plan with the completed commit and next checkpoint before the development handoff.

## Immediate next checkpoint

Begin Checkpoint 3: add the novice-friendly dependency-aware `run` command without implicitly executing review-only, incomplete, or planned stages.
