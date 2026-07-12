# Changelog

All notable changes to MooCow Mini Color Mixing Tool are documented here.

## [4.5.0] - 2026-07-12

### Changed

- Recommendations now prioritize candidates with at least 96% simulated
  two-coat hiding and no more than 3.0 dE black/white substrate shift before
  comparing the existing model score. The candidate set and four-colorant
  production limit remain unchanged.
- Candidate cards now show simulated two-coat hiding and black/white substrate
  shift, so a deceptively low black-substrate dE is not presented without its
  stability context.
- The prepared runtime colorant catalog is deeply frozen after initialization
  to prevent accidental model mutation.

### Verification

- Added a browser-UMD bootstrap parity test that parses the real inline entry
  script and compares browser candidate output with the CommonJS runtime.
- Full 216-target regression: 197 stable recommendations, up from 109; grade
  distribution changed from 8/24/88/96 to 8/31/104/73
  (excellent/pass/warning/fail).
- The mean screen-model dE increases from 3.937 to 4.723 because unstable
  black-substrate matches are no longer preferred. This is a recommendation
  safety improvement, not evidence of physical drawdown accuracy.

[4.5.0]: https://github.com/raydenpromen96-maker/moocow-color-tool/releases/tag/v4.5.0

## 4.4.0 integration stage - 2026-07-12 (included in v4.5.0)

### Added

- Added a local, attributed snapshot of all 216 QTC RAL Classic computer-
  simulated colour references, including HEX, RGB, Lab, Chinese/English names,
  QTC colour IDs, source URLs, retrieval time, and a reproducible SHA-256.
- Added `npm run sync:qtc-ral` to rebuild the snapshot from QTC's public
  directory and colour-detail endpoints with schema, count, code, and HEX/RGB
  validation.
- Added tests for 216-code completeness, order, uniqueness, provenance, and
  HEX/RGB/Lab consistency.
- Added immutable `paint-catalog.js`, a shared DOM-free `production-runtime.js`,
  and `npm run test:ral216` for complete offline recipe regression.

### Changed

- Recipe generation now uses QTC Lab as the target when present. HEX remains a
  display value and a documented fallback only.
- Preserved the six existing hand-authored recipe presets while replacing the
  old 191-colour inline display map.
- Added a visible notice that QTC values are computer-simulated screen
  references and production work must be confirmed with a current physical
  colour card.
- Extracted the current browser model/search path without changing parameters,
  then fixed representative and complete-catalogue regression hashes to
  prevent accidental output drift.

### Authorization and boundary

- Integration proceeds on the user's reported telephone confirmation from the
  RAL Asia-Pacific business manager and the user's identification of QTC as an
  authorised presentation source.
- The snapshot is not reflectance data and is not described as a replacement
  for a physical RAL colour card.

## [4.3.0] - 2026-07-12

### Added

- Added attributed GOLDEN Heavy Body water-based acrylic reflectance and
  single-constant K/S references for nine exact C.I. families: `PY83`,
  `PB15:3`, `PG7`, `PR254`, `PR101`, `PY42`, `PR122`, `PBk7`, and `PV23`.
- Added source ZIP/workbook hashes, measurement conditions, product links,
  sharing-permission wording, data limitations, a complete extraction manifest,
  per-profile digests, and a source-workbook verifier.
- Restored weighted exact-C.I. family coverage in the UI and TXT export.

### Boundaries

- The white Leneta card affects transparent colors and the approximately
  6 mil dry films are not all truly opaque.
- The data are side references only: they do not affect candidate scoring or
  ranking and do not represent current Clariant/Heubach CN batches.
- `PY74`, `PB15:1`, `PW6`, and unverified Orange D2R remain explicit gaps;
  no similar-C.I. substitution is performed.
- The public host states that Golden allowed sharing, but publishes no named
  data licence; attribution and this limitation are retained in the project.

[4.3.0]: https://github.com/raydenpromen96-maker/moocow-color-tool/releases/tag/v4.3.0

## [4.2.1] - 2026-07-12

### Fixed

- Removed all `MultipigmentPhantoms` pigment-in-epoxy `mu_a` and `mu_s'`
  arrays from the runtime, UI wording, TXT export, tests, and active notices.
- Removed the runtime family-coverage percentage instead of replacing the
  epoxy arrays with data whose commercial redistribution rights are unclear.
- Added an explicit UI and TXT boundary: no licensed measured waterborne-
  acrylic spectra are bundled, and neither epoxy curves nor similar-C.I.
  substitutions are used.

### Boundaries

- GOLDEN and RIT waterborne-acrylic datasets remain research-only candidates
  until explicit commercial redistribution and derivative-use permission is
  obtained.
- The existing screen-model reference curves are not measurements of the
  current Clariant/Heubach CN batches.

[4.2.1]: https://github.com/raydenpromen96-maker/moocow-color-tool/releases/tag/v4.2.1

## [4.2.0] - 2026-07-12

### Added

- Added three deterministic, dose-constrained model candidates with a fixed
  `106 g/L` total, `0.5 g/L` grid, minimum active dose, and four-color limit.
- Added black one-coat, black two-coat, white two-coat, and black-white
  substrate-shift diagnostics.
- Added an attributed optical-evidence layer for exact `PY74`, `PR122`, `PV23`,
  `PG7`, `PB15:3`, and `PW6` C.I. families from the MIT-licensed
  MultipigmentPhantoms dataset.
- Added TXT export metadata for model limits, optical-family coverage, batch
  references, and physical-drawdown requirements.
- Added local Node regression tests for ColorCore, recipe search, source wiring,
  spectral evidence, deterministic output, and fail-closed C.I. coverage.

### Changed

- Moved color math into the local deterministic `ColorCore` module and removed
  the network-dependent `spectral.js` scoring branch.
- Corrected the 30 nm D65 and CIE 1931 2-degree samples, including the previous
  incorrect 580 nm x-bar value.
- Improved desktop and mobile typography, control sizing, contrast, candidate
  scanning, and fixed-panel behavior.
- Kept measured `mu_a` and `mu_s'` data as shadow shape evidence only; it does
  not alter candidate scoring or ranking.

### Fixed

- Prevented generated candidates from mutating static RAL presets.
- Re-evaluated final rounded recipes before ranking and made repeated searches
  deterministic.
- Removed the unverified `PO13` identity from Orange D2R and now reports it as a
  C.I. data gap.
- Made unsupported or unverified spectral families fail closed instead of
  inheriting a visually similar pigment curve.

### Validation

- `npm test`: 41/41 passing.
- Desktop and 390 px mobile browser checks: no application errors or horizontal
  overflow.
- Shadow-isolation check: mutating optical coverage changed diagnostics only;
  candidate recipes, scores, order, and selection remained identical.
- Independent code review: approved with zero findings.

[4.2.0]: https://github.com/raydenpromen96-maker/moocow-color-tool/releases/tag/v4.2.0
