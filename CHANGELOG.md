# Changelog

All notable changes to MooCow Mini Color Mixing Tool are documented here.

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
