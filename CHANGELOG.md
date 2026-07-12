# Changelog

All notable changes to MooCow Mini Color Mixing Tool are documented here.

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
