# Receipt-derived open measurement operator pack v1

This protocol prepares the open train/validation measurement roster and assembles completed, instrument-neutral operator files into `moocow-open-measurement-admission-input-v1`. It does not admit data, fit a model, evaluate an independent holdout, rank recipes, promote a candidate, release a formula, or enable browser/runtime use. Every permission remains `false`.

## Prerequisites

- A verified acquisition-preflight receipt and its currently copied `shared/` and `open/` roots.
- The real current-lot material identities, conversion-property evidence, actual open-batch weighings, preregistered DFT bands, and 36-card open roster already bound by that receipt.
- A private local measurement root with no symlinks, junctions, reparse points, cloud-sync mutation, or untrusted concurrent writer.
- Raw evidence kept below the measurement root. Open evidence must never share a root with sealed holdout evidence.

Run commands from the repository's `calibration/` directory.

## 1. Prepare the incomplete operator pack

~~~powershell
python -m km_calibration prepare-open-measurement-pack --acquisition-receipt <path> --shared-root <path> --open-root <path> --output-dir <new-or-empty-pack-directory>
~~~

The command re-verifies the predecessor receipt and emits immutable templates for exactly:

- 36 open cards: 30 train and 6 validation;
- 72 card/backing DFT records;
- 6 bare-backing readings;
- 216 coated readings;
- 222 spectrum identities.

The pack is deliberately incomplete. Copy the six files under `operator-input/`, remove `.template` from each filename, and replace every `REQUIRED_*` value with a real operator value. Do not edit the original templates or manifest.

## 2. Complete the operator files

- `measurement-profile.json`: real session, instrument, fixture protocol, calibration-evidence path, and run-log-evidence path.
- `backings.csv`: exactly one `black` and one `white` backing row with real backing and lot IDs.
- `bare-readings.csv`: exactly six immutable backing/reposition rows.
- `dft-readings.csv`: exactly 72 immutable card/backing rows; provide positive measured micrometre points separated by semicolons. Do not supply a calculated mean.
- `coated-readings.csv`: exactly 216 immutable card/backing/reposition rows and the two accepted physical-status values.
- `spectra-long.csv`: one row per spectrum identity and wavelength on one common, strictly increasing, uniform grid. Reflectance must be a fraction in `[0,1]`, not a percentage.

The assembler permits grids within 360-830 nm. The downstream fitter additionally requires uniform coverage of at least 400-700 nm with a step no larger than 20 nm.

Every evidence reference must be a portable relative path to a nonempty regular file below the measurement root. Paths and SHA-256 values must both be unique within one assembly. If an instrument produces one multi-record file or byte-identical exports, stop and extend the evidence-locator contract; do not alter raw evidence merely to bypass duplicate detection.

## 3. Assemble admission input

~~~powershell
python -m km_calibration assemble-open-measurement-input --acquisition-receipt <path> --shared-root <path> --open-root <path> --pack-root <pack-directory> --operator-input-dir <completed-operator-directory> --measurement-root <measurement-root> --output-relative-path admission/open-measurements-input.json
~~~

The command re-verifies the predecessor and pack, validates the exact roster, spectra, DFT ordering, timestamps, IDs, evidence bindings, and input limits, then atomically writes:

~~~text
<measurement-root>/admission/open-measurements-input.json
<measurement-root>/admission/open-measurements-input.json.sha256
~~~

It accepts no holdout, sealed, promotion, release, or activation option.

## 4. Continue through the existing offline boundaries

Use `admit-open-measurements` and `verify-open-measurement-admission` as documented in `../open-measurement-admission-v1/README.md`. Only after a valid admission may `fit-open-selection-candidate` and `verify-open-selection-candidate-export` create and verify a non-runtime candidate as documented in `../open-selection-fit-export-v1/README.md`.

Successful assembly or fitting proves transport and reproducibility only. Physical accuracy remains unverified until a frozen candidate passes a separately measured, independently controlled holdout. Legacy browser output remains `catalog_screen_approximation` and `runtime_activation_permitted: false`.

## Residual filesystem boundary

The implementation rejects static symlink, junction, and reparse-point paths and publishes through temporary files/directories. Python's standard library cannot make Windows path publication race-free against a hostile concurrent directory replacement. Keep acquisition, pack, operator, and measurement roots private, local, ACL-restricted, and free of untrusted concurrent writers.
