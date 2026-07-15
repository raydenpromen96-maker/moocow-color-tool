# Open-selection laboratory recipe solver v1

This protocol converts a reverified open-selection finite-film K-M fit into a
deterministic laboratory trial recipe. It is offline only. It does not read a
sealed holdout, change browser ranking, create a runtime model, prove physical
accuracy, release a production formula, or grant any permission.

The solver accepts measured reflectance spectra only. Lab, LCh, RGB, HEX, and
screen swatches are intentionally rejected because they do not uniquely define
a spectrum and cannot establish metamerism or hiding behavior.

## Inputs

Keep these roots local, private, non-link, and free of concurrent writers:

- the verified acquisition receipt plus its `shared/` and `open/` roots;
- the verified open-measurement admission and admitted dataset root;
- the reverified `open-selection-fit-export-v1` candidate root;
- one private request root containing the request, its `.sha256` sidecar, and
  two unique structured JSON evidence records: target spectrum and dispenser
  profile.

Start from `target-spectrum-evidence.template.json` and
`dispense-profile-evidence.template.json`. Complete those records first, hash
their exact bytes, then put the two paths and hashes into
`recipe-request.template.json`. All three templates are deliberately invalid
and have no sidecars. Replace every `REQUIRED_*`, `null`, and empty spectrum or
grid. The component and lot order must exactly match
`fit-model.json.component_order`.

## Target cells

The solver derives `target_id`, `wavelength_nm`, cells, DFT, weights, backings,
and reflectance only from the hash-bound target-spectrum JSON record. The
request cannot repeat or override them. `wavelength_nm` must exactly equal the
fit model grid. Reflectance values are fractions in `[0,1]`, not percentages.

- For a real coating standard measured over black and white, enter both measured
  cell spectra with their actual dry-film thicknesses.
- For one opaque target-chip spectrum plus a hiding requirement, use the same
  measured target spectrum in one black and one white cell at the intended dry
  film thickness. This explicitly asks the model to approach the same target on
  both substrates.
- A one-cell request is accepted for diagnostic work, but it does not test the
  black/white hiding difference.

Weights are relative positive numbers. Do not duplicate a backing/DFT condition.
Preserve the unchanged instrument export separately, and normalize it into the
structured target-spectrum record under a controlled procedure. An arbitrary
text attachment, a Lab/HEX value, or a request field is not accepted as target
evidence.

Every requested DFT must remain inside the minimum/maximum DFT interval of the
measured train cells. The solver fails closed on DFT extrapolation.

## Search and dispenser policy

Use only current model colorants. V1 enumerates every support from zero through
three colorants, then runs deterministic constrained SLSQP. If every start for
any declared support fails or returns an invalid constrained result, the whole
solve fails with no output instead of silently skipping that support. A fixed
two-million objective-evaluation ceiling also fails closed. Per-colorant and
total limits are nonvolatile-volume fractions, not wet-mass fractions.

The solver derives the profile ID, mass-error limit, exact current physical
lots, balance increments, minimum nonzero doses, and maximum wet masses only
from the hash-bound dispenser-profile JSON record. The request cannot repeat or
override them. Minimum and maximum masses must be exact integer multiples of
the increment. Preserve the scale certificate or controlled balance
configuration separately and create the structured profile record from it.

The requested total-colorant and per-colorant maxima must not exceed the largest
actual nonvolatile-volume fractions observed in the hash-bound open train and
validation formulas. This is a componentwise upper envelope, not a measured
composition convex hull: previously unseen two- or three-color combinations may
still be evaluated for laboratory trial. The candidate reports
`convex_hull_membership_verified: false` and cannot be treated as production
interpolation. A deeper or more concentrated target requires additional
measured calibration formulas; v1 does not silently exceed the observed
componentwise concentration or train-DFT bounds.

The continuous result is converted with receipt-bound current-lot properties.
The local wet-mass lattice is then exhausted, converted back to actual
nonvolatile-volume fractions, and every target cell is re-predicted. The report
states that the declared neighborhood is exhaustive and that a global lattice
optimum is not proven.

## Create the request sidecar

After the completed request is final, create its lowercase SHA-256 sidecar:

```powershell
$request = Resolve-Path .\request\recipe-request.json
$digest = (Get-FileHash -Algorithm SHA256 $request).Hash.ToLowerInvariant()
Set-Content -Encoding ascii "$request.sha256" "$digest  $([IO.Path]::GetFileName($request))"
```

Evidence SHA-256 values inside the request must be calculated from the completed
structured JSON records. The parser reads the same byte snapshot that produced
each digest and derives the physical fields from that parsed record. Evidence
paths are portable POSIX-relative paths below the request root, and
target/dispenser paths and hashes must be different.

## Solve and verify

Run from `calibration/`:

```powershell
python -m km_calibration solve-open-selection-recipe-candidate `
  --acquisition-receipt <path> `
  --admission-receipt <path> `
  --dataset-root <path> `
  --shared-root <path> `
  --open-root <path> `
  --measurement-root <path> `
  --fit-export-root <path> `
  --request-root <private-request-root> `
  --request-relative-path recipe-request.json `
  --output-dir <new-or-empty-candidate-directory>
```

```powershell
python -m km_calibration verify-open-selection-recipe-candidate `
  --acquisition-receipt <path> `
  --admission-receipt <path> `
  --dataset-root <path> `
  --shared-root <path> `
  --open-root <path> `
  --measurement-root <path> `
  --fit-export-root <path> `
  --request-root <private-request-root> `
  --request-relative-path recipe-request.json `
  --candidate-root <candidate-directory>
```

The package contains only `recipe-candidate.json`, its sidecar,
`recipe-candidate-receipt.json`, and its sidecar. All production, fitting,
holdout, ranking, release, promotion, and runtime permissions remain `false`.

Use `selected.quantized.wet_masses` only to make a controlled laboratory trial.
Measure the resulting drawdown over the declared substrates and DFT before any
formula adjustment. V1 reports `uncertainty.status: insufficient_data`; its
quantization diagnostic is not a confidence interval or physical acceptance.
