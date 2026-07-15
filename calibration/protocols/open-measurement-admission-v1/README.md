# Open-only measurement admission v1.1

Revision v1.1 makes the predecessor receipt a required verification input. The verifier reconstructs the expected source, manifest, and admission receipt from the sidecar-bound admission input plus current copied roots, then requires canonical content and hash equality. This supersedes the original v1 verifier invocation that omitted `--acquisition-receipt`.

This operator protocol binds independently supplied open measurements into a portable, non-promotable open-selection dataset. It is an admission and verification boundary only: it does not infer physical values or enable fitting, evaluation, ranking, release, promotion, or runtime activation. Every permission in the emitted artifacts remains `false`.

Before admission, retain the verified acquisition receipt plus copied `shared/` and `open/` roots. Place the admission input JSON, its mandatory `.sha256` sidecar, and all referenced evidence below one non-link measurement root. The input must contain the receipt-derived 36-card roster, its 216 black/white/POS01-POS03 coated readings, and at least three distinct bare-backing observations per backing. Do not add operator formula, lot, component, actual-NV, or DFT-mean fields; those values are derived only from the reverified receipt or supplied point measurements.

Run admission into a new or empty output directory:

```powershell
python -m km_calibration admit-open-measurements `
  --acquisition-receipt <path> `
  --shared-root <path> `
  --open-root <path> `
  --measurement-root <path> `
  --admission-input <relative-posix-path> `
  --output-dir <new-or-empty-directory>
```

Reverify a portable copy without changing it:

```powershell
python -m km_calibration verify-open-measurement-admission `
  --acquisition-receipt <path> `
  --admission-receipt <path> `
  --dataset-root <path> `
  --shared-root <path> `
  --open-root <path> `
  --measurement-root <path>
```

Verification calls the full acquisition-preflight verifier, reopens the exact admission input named by its persisted whole-file binding, re-enforces the complete card/reading/DFT/global-ID/evidence roster, and reconstructs canonical expected artifacts. The supplied admission receipt must be `dataset-root/admission-receipt.json`, its predecessor digest must equal the actual supplied acquisition receipt, and the dataset tree must contain only the `sources/` directory and the exact six v1 artifact files with no links, reparse points, or extras.

Successful output is limited to redacted hashes, roster counts, the output directory, status/state, and the fixed false permissions. The published tree contains `manifest.json`, `sources/open-measurements.json`, `admission-receipt.json`, and SHA-256 sidecars; it never copies raw evidence.
