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

- Last updated: 2026-08-21.
- Source baseline: point-cloud geometry/classification extraction is the current review slice, with proposed subject `refactor: extract point-cloud geometry`; inspect the live checkout and Git history for the aligned source, test, quality-gate, and documentation changes.
- The live checkout and Git history remain the source of truth; do not expect this document to contain the hash of the commit that updates the document itself.
- Most recently completed code slice: point-cloud building-footprint GeoJSON reading, canopy PNG decoding, and paired ESRI ASCII-grid discovery/parsing live in `stages/point_cloud/inputs.py`. Commit: `15776b2 refactor: extract point-cloud input adapters`.
- Current review slice: `stages/point_cloud/geometry.py` owns projected-polygon predicates and indexing, DTM/DSM scanning, and building/tree/unclassified point classification. `stage.py` retains input-policy validation and handoff selection, CRS projection, diagnostics, artifact writes, manifest publication, and public `plan()`/`run()` behavior. The behavior-preserving verification passes 462 tests with 86.05% branch coverage on Python 3.11.12; Ruff passes, configured mypy passes for 17 source files, and full-package mypy passes for 44 source files. `.codegraph/` remains intentionally untracked.
- The recorded cross-source-policy verification baseline remains historical. Like-for-like coverage evidence clears the stage-package review: Python 3.11.12 feature coverage was 85.91% versus the recorded 85.88%; Python 3.13.12 feature coverage was 85.73% versus the `08d2cd4` baseline of 85.71%. The earlier 85.73%-versus-85.88% comparison mixed runtimes and is not a refactor regression.

## Baseline priorities and status

| Priority | Improvement | Status | Current state and remaining work |
| ---: | --- | --- | --- |
| 1 | Authoritative pipeline model | Complete in `1345dd2` | `StageId` and the dependency-neutral layout catalogue own identity/order/path derivation; `StageSpec` owns operational metadata, selection policy, planners, typed runners, and CLI dispatch; the dependency-aware `run` command resolves and executes safe plans. |
| 2 | Stable stage identity and unique output numbering | Complete | `StageId` is independent of order, and `number_name` derives unique `01` through `07` directories from the stored stage identity and sequence number without hard-coded numbered folder strings. |
| 3 | Break up god modules | In progress | The shapefiles decomposition is complete. Point-cloud input adapters, geometry/classification, reporting, and HTML/scene-data rendering now live in focused sibling modules, reducing `point_cloud/stage.py` to 741 lines; `city_models/stage.py` remains 2,067. Preserve public `plan()`/`run()` facades while continuing point-cloud diagnostics and publication slices. |
| 4 | Shared stage contracts | Complete | All six executable stages publish schema-version-2 manifests using shared status, manifest, output, provenance, artifact-reference, and consumer-validation rules. |
| 5 | Transactional output handling | Partial | Manifest-last is universal. Locks and atomic artifact writers are not yet universal in shapefiles, trees, and visual enrichment. |
| 6 | Declarative uniformly typed CLI | Partial | Registry dispatch and immutable CLI-independent `StageRunOptions` exist. Stage-focused argument registration and one application exception hierarchy remain. |
| 7 | Python quality gates | Partial/advanced | Ruff, mypy, branch coverage, and documented commands exist. CI, optional pre-commit, expanded configured scope, and stronger validated boundary types remain. |
| 8 | Central geospatial transformations | Not started | EPSG:25832 conversion remains duplicated across five stage modules. Add a shared CRS adapter and evaluate maintained readers separately. |
| 9 | Tests independent from mutable demonstration assets | Partial | The Mercato AP-007 mismatch is fixed, but behavioral tests still read a mutable documentation asset. Add immutable `tests/data/` fixtures and retain separate canonical-asset tests. |
| 10 | README operational truth | Complete and continuous | The stage-status table and limitations are current. Keep README, code, tests, and graphical QA instructions aligned after every change. |

## Dependency-adjusted execution order

Priority 1 was the highest-level objective, but its automatic runner required unambiguous stage layouts. Priority 2 was therefore completed first in `370f29d`, followed by Priority 1 in `1345dd2`.

### Checkpoint 1: Stable stage identity and layout

Status: complete in `370f29d`.

- Introduce a typed stable stage identifier independent of numeric order.
- Put static stage layout metadata in a dependency-neutral catalogue.
- Make planners, runners, and consumers derive output paths from that catalogue.
- Derive every stage path through the catalogue.
- Add registry consistency and uniqueness tests.
- Combined review-slice commit: `refactor: derive unique pipeline stage directories`.

### Checkpoint 2: Unique output directories

Status: complete in `370f29d` at the user's direction. Combining the checkpoints ensured no intermediate design retained hard-coded numbered folder names.

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

Status: complete in `1345dd2`.

- Add a novice-friendly `run` command driven by registry dependencies.
- Make bare `run` execute only shapefiles, point-cloud, and city-models; keep air-purifiers optional through `--include air-purifiers`.
- Support `--target <stage>` for one executable stage and its required dependency closure.
- Respect explicit inputs that replace default producers.
- Do not implicitly run review-only, incomplete, or planned stages.
- Print the resolved execution plan, stop on required-stage failure, aggregate typed results, and return consistent exit codes.
- Derive operational validation and documentation from registry metadata where practical.
- Commit: `1345dd2 feat: add dependency-aware pipeline execution`.
- Priority 1 and priority 2 are complete.

### Checkpoint 4: Decompose large stage modules

Status: the planned focused shapefiles slices and stage-package reorganization are complete. Point-cloud geometry/classification extraction is the current review slice with proposed subject `refactor: extract point-cloud geometry`; decomposition of `city_models/stage.py` has not started.

- Use several behavior-preserving commits: extract HTML/report rendering first, then diagnostics/publication, input adapters/parsers, and domain transformations/validation.
- Work through shapefiles, point-cloud, and City4CFD separately; never combine all three into one commit.
- Keep each stage's public `plan()` and `run()` facade stable.
- Completed presentation slice: `stages/shapefiles/rendering.py` owns self-contained HTML/SVG feedback and `stages/shapefiles/reporting.py` owns Markdown reporting.
- Completed diagnostics slice: `stages/shapefiles/diagnostics.py` owns pure geometry, supplemental-input, urban-planning, and aggregate-summary diagnostics; `run()` still writes every diagnostic artifact.
- Completed publication slice: `stages/shapefiles/publication.py` owns the typed publication input, stable artifact ordering and naming, imagery-evidence discovery, metrics/details assembly, and the final manifest publication call. `run()` invokes it only after every artifact write completes. Commit: `3cda1e9 refactor: extract shapefiles publication`.
- Completed input-boundary slice: `stages/shapefiles/inputs.py` owns Overpass cache/network/retry handling and batch merging, binary ESRI SHP/DBF validation and decoding, and WMS request/response handling plus evidence files. `stages/shapefiles/stage.py` supplies queries and the ROI bounding box, then converts parsed records into domain features. Commit: `5681e3d refactor: extract shapefiles input adapters`.
- Completed Overpass transformation slice: `stages/shapefiles/transformation.py` owns tag-inventory classification, node lookup, way/relation geometry assembly, ROI assignment, building-roof base-height interpretation, deterministic feature ordering, and skipped-reason accounting. `stages/shapefiles/stage.py` imports shared metric/ROI and polygon helpers, re-exports `overpass_to_features()`, and retains stable `plan()`/`run()` behavior. Commit: `ad4a983 refactor: extract shapefiles transformation`.
- Completed supplemental ESRI transformation slice: `stages/shapefiles/supplemental.py` owns supplemental path/type validation, stage-local EPSG:4326/EPSG:25832/EPSG:3003 conversion, polygon and point record conversion, and declarative `TREE_ATTRIBUTE_MAPPINGS`. Recognized mappings remain optional, unmapped source fields remain available in `source_attributes`, and another normalized mapping can be added without changing record conversion. Metric conversion uses explicit value or attribute-name units before declared mapping defaults and never infers units from numeric magnitude. Commit: `e702264 refactor: extract shapefiles supplemental transformation`.
- Completed cross-source policy slice: `stages/shapefiles/policy.py` owns Overpass-versus-supplemental tree deduplication, configured selector ranking, polygon clipping/removal, and overlap diagnostics. `stages/shapefiles/stage.py` retains compatibility aliases used by established tests and keeps stable orchestration and `plan()`/`run()` behavior. Commit: `refactor: extract shapefiles cross-source policy`.
- Completed final review slice: stage packages expose their public facades from `stages/<stage>/__init__.py`, keep implementation in `stages/<stage>/stage.py`, and place shapefiles helpers and their focused tests in matching nested paths. The topology contract now forbids cross-stage private `stage.py` test imports while retaining same-stage focused imports and the `tests/test_artifacts.py` City4CFD-owner exception. Verification recorded stale-path/import searches, Ruff, configured mypy (17 files), full-package mypy (40 files), and 457 passing pytest tests. Python 3.11.12 feature coverage was 85.91% versus the recorded 85.88%; Python 3.13.12 feature coverage was 85.73% versus the `08d2cd4` baseline of 85.71%.
- Completed point-cloud presentation slice: `stages/point_cloud/reporting.py` owns Markdown generation and `stages/point_cloud/rendering.py` owns self-contained HTML, scene payloads, voxel preview sampling, and footprint elevation lookup. `stage.py` retains orchestration and passes the already-derived projected bounding box and classification display thresholds into rendering. Commit: `259d40b refactor: extract point-cloud presentation`.
- Completed point-cloud input-adapter slice: `stages/point_cloud/inputs.py` owns building-footprint GeoJSON reading, canopy PNG decoding, and paired ESRI ASCII-grid discovery, metadata/value parsing, pairing validation, and cell iteration. `stage.py` keeps input-policy validation and handoff selection, projection, geometry/classification, diagnostics, publication, and stable `plan()`/`run()` behavior. Commit: `15776b2 refactor: extract point-cloud input adapters`.
- Current point-cloud geometry/classification slice: `stages/point_cloud/geometry.py` owns projected-polygon predicates and spatial indexing, DTM/DSM scanning, tree-evidence and roof-relative filtering, and complete building/tree/unclassified point assignment. `stage.py` calls `classify_raster_points()` after selecting and projecting inputs, then retains diagnostics, artifact writes, manifest publication, and stable `plan()`/`run()` behavior. Proposed commit: `refactor: extract point-cloud geometry`.
- Remaining Checkpoint 4 scope: extract point-cloud diagnostics next, then publication in a later focused slice before starting `city_models/stage.py`.

### Checkpoint 5: Complete transactional publication

- Apply `lock -> invalidate stale manifest -> validate -> atomic artifacts -> report/preview -> manifest last` to every executable stage.
- Add locking and atomic writes to shapefiles, trees, and visual enrichment.
- Ensure interrupted runs never leave a trusted manifest.
- Assess directory-level promotion only after universal file-level atomicity.
- Proposed commit: `refactor: make stage artifact publication transactional`.

### Checkpoint 6: Finish the declarative CLI

- Add focused per-stage argument registration and validation derived from the registry.
- Classify every stage input as persistent TOML configuration, a one-run CLI override, or both; when both are supported, preserve explicit CLI-over-TOML precedence and derive help and validation from registry metadata where practical.
- Remove unrelated options from a single shared parser surface.
- Introduce one application-level exception hierarchy and uniform human/JSON error behavior.
- Use separate commits for parser declaration and error unification if review scope becomes large.

### Checkpoint 7: Complete quality infrastructure

- Add CI running Ruff, mypy, and full pytest branch coverage.
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
3. Run Ruff, configured and full-package mypy, full pytest with branch coverage, and `git diff --check`.
4. Present the final diff and explain the commit meaning.
5. Wait for approval before committing.
6. Announce that it is time to push after the commit.
7. Push only after authorization.
8. Update this plan with the completed commit and next checkpoint before the development handoff.

## Immediate next checkpoint

After approval and commit of the current geometry/classification slice, continue Checkpoint 4 with a behavior-preserving extraction of point-cloud alignment diagnostics. Keep input adapters, geometry/classification, publication, external behavior, and public `plan()`/`run()` stable. Do not mix later point-cloud boundaries, Checkpoint 5 transactional publication, or Checkpoint 8 shared CRS centralization into that slice. Continue the remaining point-cloud decomposition before starting `city_models/stage.py`.
