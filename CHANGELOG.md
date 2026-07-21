# Changelog

All notable changes to MooCow Mini Color Mixing Tool are documented here.

## [Unreleased] - spectral engine v5

### Added

- Added measured proxy spectra to `src/family-spectra.js`: 4 CHSOS Pigments
  Checker acrylic-binder measurements (PY74, PB15:1, PO73, PW6 rutile,
  resampled to the 11-point 400-700 nm grid and hardcoded with source
  attribution) and PG36 as a GOLDEN-measured PG7 curve shifted +23 nm and
  anchored to the GOLDEN official PG36 masstone Lab (27.82, -11.83, -0.17,
  fit error 0.32). All proxy data is marked pending 45-card measured
  calibration.
- Added a 15th colorant G36 (PG36, `pending_purchase`) with estimated
  pigment content (30%) and estimated density (1.35 g/mL), both flagged as
  estimates in catalog fields, the UI, and the TXT export.
- Added official Clariant pigment-content values for R254D (50%) and 073
  (30%, estimated) and corrected W064 to 55% (estimated; the previous 70%
  was the White R 130 analog, inconsistent with the 1.83 wet density).
- Recipe candidates now carry `recipeMlPerL` (per-colorant mL/L from
  supplier wet densities), and the UI dose readouts show grams and mL
  together.

### Changed

- The production engine (`src/production-runtime.js`) now evaluates colour
  with an 11-wavelength single-constant Kubelka-Munk model over the bundled
  measured/proxy spectra, weighted by effective pigment mass (colorant grams
  x official pigment content %), replacing the fabricated REFERENCE_SPECTRA
  table and the 3-channel trust blend. The legacy 3-channel K/S model is
  kept only as the modelSpread divergence indicator.
- Runtime provenance updated to `proxy_measured_spectra_km_model` /
  `uncalibrated_proxy_spectra_pending_drawdown_measurement`; still not
  verified for physical accuracy.
- Validation: `experiments/validate-engine.mjs` scores the production
  engine's first-choice recipes for all 216 RAL targets against the measured
  -spectra truth function from `experiments/whatif-real-spectra.mjs`:
  mean dE2000 4.19, median 3.60, 119/216 above 3 dE (acceptance: mean <= 5;
  oracle bound 3.51).
- Re-pinned deterministic fixtures after the engine change:
  `tests/production-runtime.test.js` representative hashes (13 RAL codes)
  and `scripts/ral-216-regression.mjs` `EXPECTED_OUTPUT_SHA256`
  (new summary: stableRecommended 216/216, grades 29/39/94/54).
- Test updates with documented causes: catalog and runtime catalog sizes
  14 -> 15 (G36 added); fail-closed continuity tests now null out a CI in a
  cloned catalog because 073 gained a real CHSOS proxy spectrum; the
  supplier-density test scopes the dated supplier record to the 14 purchased
  colorants while G36 keeps explicit estimated-density provenance; the
  family-spectra manifest test compares only GOLDEN-sourced profiles.

### Safety boundary

- Spectra remain proxy data (GOLDEN white-card drawdowns, CHSOS acrylic
  samples, one anchored extrapolation); they do not represent current
  Clariant CN batches and must be replaced by measured 45-card calibration
  before any production use.

## [Unreleased] - 2026-07-15

### Added

- Added a normalized, hash-attributed supplier record for all 14 nominal wet
  densities and deterministic wet-mass/wet-volume conversion helpers.
- Added supplier-confirmed identity metadata for DPP Red GD (`PR254`) and
  Orange D2R (`PO73`, supplier-reported C.I. number `561170`).
- Added an offline finite-film, two-constant Kubelka-Munk calibration package
  with hash-bound receipts for pilot acquisition, open-measurement admission,
  open-selection fitting, independent-holdout activation, and laboratory-trial
  inverse recipe solving.
- Added current-lot operator protocols, immutable JSON/CSV templates, synthetic
  examples, and verification commands for the full calibration evidence chain.
- Added a JavaScript physical-optical model and deterministic synthetic recovery
  harness while preserving the existing browser recommendation boundary.

### Changed

- Replaced public-analog density values in the screen catalog with supplier
  nominal wet densities and exposed the resulting mL equivalents in the UI and
  TXT export without changing the 106 g/L wet-mass recipe policy.
- Recipe and runtime continuity checks now fail closed when calibration evidence
  is absent, malformed, mutated, or not authorized for the requested stage.
- Laboratory recipe candidates are re-evaluated on black and white substrates
  after real dispenser wet-mass quantization instead of treating continuous
  optimizer output as a directly dispensable formula.

### Safety boundary

- The new calibration path is research and laboratory tooling only. It does not
  activate browser ranking, publish production formulas, accept Lab/HEX as a
  measured target, or claim physical drawdown accuracy.
- Production promotion still requires real current-lot spectra, verified batch
  identity, physical drawdowns, and a separately sealed independent holdout.
- The supplier workbook contains density values only and omits their unit,
  method, temperature, batch identity, solids, and spectra. Values are treated
  as nominal wet density in g/mL, not as evidence for K/S, nonvolatile volume,
  physical accuracy, or production acceptance.

### Verification

- Python calibration suite: 263 tests passed.
- Node application suite: 60 tests passed.
- Complete 216-target regression passed with numeric hash
  `6e10fc9f7826a492a2d3bbbac03578ee2dd70e8d172e0f25e97e8c032b1e52d9`.

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
