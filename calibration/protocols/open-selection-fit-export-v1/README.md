# Receipt-gated open-selection K-M fit/export v1

This protocol adds one offline-only path from a reverified open-measurement
admission to a finite-film, two-constant Kubelka-Munk candidate package. It is
an open train/validation selection boundary only. It does not alter legacy
generic fitting, open an independent evaluation set, rank browser/runtime
results, authorize production, or grant permissions.

The commands accept no tuning, split, independent-evaluation, ranking,
production, runtime, or permission options. Existing commands remain
unchanged.

## Commands

Fit into a new or empty, non-link output directory:

```powershell
python -m km_calibration fit-open-selection-candidate `
  --acquisition-receipt <path> `
  --admission-receipt <path> `
  --dataset-root <path> `
  --shared-root <path> `
  --open-root <path> `
  --measurement-root <path> `
  --output-dir <new-or-empty-directory>
```

Semantically reverify an existing candidate export:

```powershell
python -m km_calibration verify-open-selection-candidate-export `
  --acquisition-receipt <path> `
  --admission-receipt <path> `
  --dataset-root <path> `
  --shared-root <path> `
  --open-root <path> `
  --measurement-root <path> `
  --export-root <candidate-export-directory>
```

Both commands reverify the acquisition and admission receipts against the
current shared, open, and measurement roots. They consume no sealed or
independent-evaluation authority.

## Fit-readiness grid

| Gate | Required condition |
| --- | --- |
| Admission status | `open_selection_only`; all six permissions and `production_pass` are `false`. |
| Component basis | Exactly 15 fixed-order `(component_id, physical_lot_id)` pairs. |
| Open data | 30 train cards, 6 validation cards, and 216 coated readings; only `train` and `validation` splits. |
| Wavelengths | Strictly increasing, uniform grid covering at least 400–700 nm with steps no greater than 20 nm. |
| Optical setup | Saunderson mode `off`; both non-identical admitted black and white backing means. |
| Replication and DFT | Exactly three reposition spectra per `(card_id, backing)`; positive cell DFT; paired DFT-L/DFT-H train cards. |
| Design | Actual-NV train design has 15 columns, rank 15 after scaling, and finite condition diagnostics. |
| Candidate validity | Converged finite-film fit, finite predictions in `[0, 1]` within roundoff, no forbidden bound, and full-rank per-wavelength data Jacobians. |

The legacy three-point admission transport fixture remains valid for admission
testing but is intentionally not fit-ready.

## Fixed fitting and selection boundary

The fitter averages the three reposition spectra into 60 train and 12
validation card/backing cell spectra. It fits train cells only with the fixed
regularization grid `[0, 1e-6, 1e-4, 1e-2, 1]`, four deterministic starts,
and the contract-defined bounded `scipy.optimize.least_squares` specification.
Validation selects the frozen candidates by `(RMSE, MAE, max_abs,
regularization, model_payload_sha256)`; there is no train-plus-validation
refit. Reported metrics are diagnostics, not acceptance thresholds.

## Exact candidate package tree

The output root is published atomically and must contain only these regular
files; it contains no raw spectra, measurement roots, absolute paths, or
independent-evaluation metadata.

```text
fit-model.json
fit-model.json.sha256
selection-evaluation.json
selection-evaluation.json.sha256
fit-export-receipt.json
fit-export-receipt.json.sha256
```

The schemas are:

- `moocow-open-selection-km-fit-model-v1`
- `moocow-open-selection-km-selection-evaluation-v1`
- `moocow-open-selection-km-fit-export-receipt-v1`

Every candidate artifact keeps `dataset_status: open_selection_only`,
`production_pass: false`, every permission `false`, and
`runtime_compatible: false`. Browser ranking and runtime promotion remain
ineligible even if a caller later alters those booleans.

## Nonproduction status and accuracy limit

This protocol proves only reproducible, receipt-bound candidate fitting and
selection on its admitted data. It does not prove real-world color accuracy,
physical acceptance, repeatability adequacy, independent-evaluation accuracy,
ranking eligibility, or production readiness. Real accuracy remains unproven
until real instrument repeatability and independent evaluation evidence are
separately established.
