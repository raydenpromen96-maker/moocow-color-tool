# 45-card pilot acquisition pack

This pack is deliberately invalid and cannot be frozen until every template value is replaced by real, current-lot physical evidence. It creates no model, evaluates no holdout, and never enables physical ranking or promotion.

1. Update the copied registry from 15 physical container labels. Every component needs `lot_verification_status=verified_physical_label`, a non-placeholder verification ID, a timezone-aware ISO timestamp, and a whole-file `physical_label_evidence` locator under `evidence/labels/`. Do not infer a missing lot.
2. Complete `pilot-design.template.json`: remove `template_status`, set `pilot_design_status` to `pilot_design_preregistered`, provide one real randomization plan ID and one real batch ID per formula family.
3. Preregister real positive DFT-L/M/H values: target L < M < H and acceptance intervals must be strictly ordered and non-overlapping. Do not invent DFT values, material properties, condition-number gates, or performance thresholds.
4. Complete and independently reverify the real four-card receipt first. Its base and W064 lots must equal this pilot registry.
5. Freeze only with `freeze-pilot-design --registry-evidence-root evidence`; it writes the two hash-bound artifacts into a new empty directory. The open roster has 216 primary slots and the sealed holdout roster has 54.
6. Use `verify-pilot-design-receipt --registry-evidence-root evidence` after copying evidence. Freeze permits acquisition only; fitting, holdout release, physical ranking, and promotion remain false.

The public roster preregisters requested `target_NV` acquisition inputs, not measured holdout outcomes. Completed actual-NV values, DFT observations, reflectance spectra, and holdout evaluation artifacts must remain outside this repository under separate custody.
