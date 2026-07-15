# Four-Card Diagnostic Operator Pack v2

This pack is deliberately invalid until every REQUIRED_ value is replaced with current physical evidence. It is a diagnostic-only preflight: it cannot fit a model, rank a formula, or promote a production artifact.

1. Update the copied registry snapshot from physical labels. Both base-waterborne-clear and colorant-W064 must carry their real batch_id and lot_verification_status=verified_physical_label; do not invent a missing lot.
2. Choose exactly one route: mass_solids_nonvolatile_density or wet_density_volume_solids. Use only the matching property, plan-input, and diagnostic-manifest templates. Mixed-route fields are rejected.
3. Fill the matching moocow-conversion-property-record-v2 canonical JSON for each current physical lot and save it beneath evidence/properties. Each property locator must select that exact JSON object; arbitrary text files are rejected. The route, component, lot, record ID, value, unit, method, and timezone-aware observed_at must exactly match every copied manifest or plan field.
4. Fill one weighing-plan input per formula batch and run generate-weighing-plan. Plan generation re-parses the bound canonical property JSON before calculating masses. The generated target_wet_mass_g values are a plan only, never actual weighing evidence.
5. Weigh each component no later than cure_start and save the observed events in a moocow-actual-weighing-record-v2 canonical JSON beneath evidence/weighing. One file may contain multiple unique events, but each manifest locator must select the exact JSON object and every formula, batch, event, record, component, lot, mass, unit, method, and timezone-aware time field must match. Arbitrary text and weighing-plan records are rejected.
6. Record measured DFT locations, cure/application conditions, at least three bare spectra per backing, and exactly 24 coated spectra. Do not enter DFT means/SDs, normalized fractions, deviations, or digests; software derives them. Use distinct non-overlapping byte ranges for shared raw or DFT exports.
7. Validate structure, run evidence-root preflight, then independently verify the receipt after copying the evidence root. A passing receipt remains diagnostic-only and never enables fitting, ranking, or promotion.

Example locator command:

    moocow-km-calibration bind-evidence-record --evidence-root acquisition-pack/evidence --relative-path raw/run.csv --byte-offset 0 --byte-length 120

Generate a route-specific weighing plan:

    moocow-km-calibration generate-weighing-plan --input acquisition-pack/weighing-plan-input.mass_solids_nonvolatile_density.w064.template.json --evidence-root acquisition-pack/evidence --output acquisition-pack/weighing-plan.w064.generated.json

Structural validation command:

    moocow-km-calibration validate-four-card-structure --format csv --manifest acquisition-pack/diagnostic-manifest.mass_solids_nonvolatile_density.template.json --input acquisition-pack/spectra-long.template.csv

Required preflight command:

    moocow-km-calibration preflight-four-card --format csv --manifest acquisition-pack/diagnostic-manifest.mass_solids_nonvolatile_density.template.json --input acquisition-pack/spectra-long.template.csv --evidence-root acquisition-pack/evidence --output-dir preflight-output

Independent receipt verification after relocating/copying evidence:

    moocow-km-calibration verify-four-card-receipt --receipt preflight-output/preflight-receipt.json --evidence-root copied-evidence
