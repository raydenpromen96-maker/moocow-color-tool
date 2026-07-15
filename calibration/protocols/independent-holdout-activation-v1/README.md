# Independent holdout activation review v1

This slice is software-validation-only. It validates deterministic synthetic authority, signature, inventory, replay, leakage, and output-boundary behavior for one frozen finite-film K-M candidate and one separately frozen baseline. It is neither a fit, selection, tuning, refit, promotion, nor browser/runtime activation path.

`measured_current_batch` is explicitly unsupported. It fails with `AUTHORITY:MEASURED_AUTHORITY_UNAVAILABLE` before release-envelope or sealed-input access. Current software cannot emit `PASS` or `activation_review_eligible=true`; only `synthetic_test_only` is evaluable, and it always remains `INDETERMINATE`, ineligible, and false for every production, runtime, physical-ranking, promotion, and preflight permission.

## Commands and dispatch boundary

The integration exposes exactly these commands:

```powershell
python -m km_calibration verify-holdout-preregistration <authority arguments>
python -m km_calibration run-independent-holdout-evaluation <authority arguments> <release/evaluation arguments>
python -m km_calibration verify-independent-holdout-evaluation <authority arguments> <release/evaluation arguments>
```

Every command takes these exact authority arguments, which map directly to the
core public APIs `verify_holdout_preregistration(...)`,
`run_independent_holdout_evaluation(...)`, and
`verify_independent_holdout_evaluation(...)`:

```text
--acquisition-receipt <path>        --admission-receipt <path>
--dataset-root <path>               --shared-root <path>
--open-root <path>                  --measurement-root <path>
--candidate-export-root <path>      --baseline-model <path>
--custody-commitment <path>         --repeatability-receipt <path>
--acceptance-profile <path>         --colorimetry-profile <path>
--trust-store <path>                --preregistration-envelope <path>
```

`run-independent-holdout-evaluation` additionally requires:

```text
--release-envelope <path> --sealed-input <path> --output-dir <new-or-empty-directory>
```

`verify-independent-holdout-evaluation` additionally requires:

```text
--release-envelope <path> --sealed-input <path> --evaluation-root <evaluation-directory>
```

Both evaluation commands also accept:

```text
--release-ledger-dir <existing-ordinary-directory>
```

The option is syntactically optional. Synthetic software-validation runs may
omit it; if supplied, it protects the synthetic release from replay without
changing its ineligible status.

No command accepts a fit, threshold, selection, ranking, promotion, or enablement argument, and `fit-open-selection-candidate` remains unchanged.

Each command emits the existing stable JSON result on stdout. Validation errors remain CLI failures; a failed prerequisite must not write a partial output.

### Release replay ledger

For synthetic software-validation runs, `--release-ledger-dir` must name an
already existing ordinary directory (not a symlink, reparse point, or file).
The evaluator atomically records the preregistration/candidate/profile/custody
tuple there. That marker key is independent of release-envelope bytes, nonce,
and issue time, so re-signing the same logical release is rejected with
`RELEASE_REPLAY` before sealed input is opened. A ledger-based re-verification
uses the retained marker. In all produced outputs, production, runtime,
physical-ranking, and promotion permissions remain false.

## Required immutable artifacts

The preregistration binds all of the following before any sealed evaluation input is released:

1. The exact open candidate export: `fit-model.json`, `selection-evaluation.json`, and `fit-export-receipt.json`, including their hashes and current verified open lineage.
2. A separately frozen finite-film baseline package on the same wavelength grid, component/lot order, concentration basis, measured backings, and prediction equation.
3. A real repeatability-baseline receipt, an acceptance-profile receipt with every numeric criterion explicitly present, and a colorimetry profile with D65/10 plus at least one alternate 10-degree condition.
4. The public holdout-custody commitment, a trusted public-key store with distinct custodian and reviewer keys, and the evaluator implementation id.
5. A one-time custodian release binding that preregistration digest to the exact sealed evaluation-input digest.

The evaluation output is an atomic exact-tree package containing a private `sealed-holdout-evaluation-detail.json` with its SHA-256 sidecar and a public `independent-holdout-review-receipt.json` with its sidecar. The public receipt contains aliases and aggregate decision evidence only. It must contain no raw spectra, actual-NV vectors, formula/batch/card/measurement identifiers, evidence paths, sealed-root paths, or per-record hashes.

## Real evidence versus synthetic fixtures

Real measured review remains closed until a pinned physical evidence authority,
traceable criterion schema, frozen DFT-band/evidence receipt, and canonical
D65/10 plus alternate 10-degree standard tables are implemented and
independently reviewed. Runtime remains off.

`synthetic_test_only` is exclusively for deterministic software tests. It must
remain `INDETERMINATE`, `activation_review_eligible=false`, and retain every
production, runtime, physical-ranking, and promotion permission as `false`.
No CLI option can relabel synthetic evidence as measured; the fixture builder
always emits synthetic authorities, and a manually relabeled authority is
rejected.

## Signature and custody boundary

SHA-256 sidecars establish byte consistency only. The evaluator verifies Ed25519 signatures using the externally supplied custodian and reviewer **public keys only**. It never accepts, generates, stores, or derives a private key. Key fingerprints must be distinct; cryptographic validity does not by itself prove operational independence, which remains the laboratory's responsibility.

## Fixed holdout shape and calculations

The sealed release must contain exactly three holdout families, nine cards, eighteen card/backing decision cells, and fifty-four raw reposition spectra:

```text
3 families x 3 DFT cards x 2 backings x 3 positions (POS01/POS02/POS03)
= 9 cards, 18 decision cells, 54 spectra
```

The decision unit is one card/backing cell. The three position spectra are validated and then averaged within that cell; they are not fifty-four independent formulas. Evaluation uses the stored finite-film candidate and baseline with actual NV fractions, measured DFT in millimetres, and matching bare-backing means. It cannot refit, tune a threshold, select a model, or use open validation metrics to change the candidate.

All acceptance numbers must already be finite, explicit, and bound in the real preregistered acceptance profile. This protocol invents no dE00, RMSE, repeatability, DFT, confidence, or noninferiority threshold.

## Runtime boundary

The browser runtime may recognize a public review receipt only as structural-only evidence. Raw JSON, caller-mutated booleans, synthetic evidence, unsigned receipts, and `ACTIVATION_REVIEW_READY` all remain `enabled=false` and `cryptographicallyVerified=false`. A pinned production-authority verifier does not exist in the current runtime; no runtime activation is implemented by this protocol.

## Preconditions for a future measured-review implementation

A future, independently reviewed measured-review implementation would need to
obtain and freeze:

- current-batch real repeatability evidence, including same-position, reposition, bare-backing, DFT, and inter-card/process strata where required;
- a complete real acceptance profile derived without holdout access;
- canonical D65/10 and alternate 10-degree colorimetry tables, integration weights, reference whites, and source hashes;
- the frozen baseline package, verified current open lineage, and current custody commitment; and
- distinct custodian/reviewer public-key trust-store entries plus the one-time signed release envelope.

Until that separately reviewed implementation exists, use only synthetic
fixtures for software testing and do not release or inspect a real sealed
holdout.
