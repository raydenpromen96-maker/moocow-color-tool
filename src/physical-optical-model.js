(function (root, factory) {
  const api = factory();

  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.MooCowPhysicalOpticalModel = api;
}(typeof window !== 'undefined' ? window : typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const DATASET_SCHEMA = 'moocow-km-calibration-dataset-v1';
  const MODEL_SCHEMA = 'moocow-km-two-constant-model-v1';
  const RESEARCH_RECEIPT_SCHEMA = 'moocow-km-research-receipt-v1';
  const INDEPENDENT_HOLDOUT_REVIEW_RECEIPT_SCHEMA = 'moocow-independent-holdout-review-receipt-v1';
  const ACTIVATION_REVIEW_READY = 'ACTIVATION_REVIEW_READY';
  const PRODUCTION_SCHEMA = Object.freeze({
    dataset: 'moocow-km-production-calibration-dataset-v1',
    model: 'moocow-km-two-constant-production-model-v1',
    evaluation: 'moocow-km-production-evaluation-v1',
    receipt: 'moocow-km-production-receipt-v1',
    status: 'production_candidate'
  });
  const HASH = /^[a-f0-9]{64}$/;

  class PhysicalOpticalModelError extends Error {
    constructor(message) {
      super(message);
      this.name = 'PhysicalOpticalModelError';
    }
  }

  function fail(message) {
    throw new PhysicalOpticalModelError(message);
  }

  function isObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function expectObject(value, label) {
    if (!isObject(value)) fail(`${label} must be an object`);
    return value;
  }

  function expectExactKeys(value, keys, label) {
    const actual = Object.keys(expectObject(value, label)).sort();
    const expected = keys.slice().sort();
    if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
      fail(`${label} has unexpected or missing fields`);
    }
  }

  function expectString(value, label) {
    if (typeof value !== 'string' || !value.trim()) fail(`${label} must be a non-empty string`);
    return value;
  }

  function expectFiniteNumber(value, label) {
    if (typeof value !== 'number' || !Number.isFinite(value)) fail(`${label} must be a finite number`);
    return value;
  }

  function expectHash(value, label) {
    if (typeof value !== 'string' || !HASH.test(value)) fail(`${label} must be a lowercase SHA-256 digest`);
    return value;
  }

  function expectWavelengthGrid(value, label) {
    if (!Array.isArray(value) || value.length < 3) fail(`${label} must contain at least three wavelengths`);
    const wavelengths = value.map((item, index) => expectFiniteNumber(item, `${label}[${index}]`));
    if (wavelengths[0] < 360 || wavelengths[wavelengths.length - 1] > 830) {
      fail(`${label} must remain in the 360-830 nm range`);
    }
    const interval = wavelengths[1] - wavelengths[0];
    if (!(interval > 0)) fail(`${label} must be strictly increasing and uniformly sampled`);
    for (let index = 2; index < wavelengths.length; index += 1) {
      const difference = wavelengths[index] - wavelengths[index - 1];
      if (!(difference > 0) || Math.abs(difference - interval) > 1e-9) {
        fail(`${label} must be strictly increasing and uniformly sampled`);
      }
    }
    return wavelengths;
  }

  function expectMatchingWavelengths(left, right) {
    if (left.length !== right.length || left.some((value, index) => value !== right[index])) {
      fail('Model wavelength_nm does not exactly match manifest wavelength_nm');
    }
  }

  function expectReflectance(value, length, label) {
    if (!Array.isArray(value) || value.length !== length) fail(`${label} must match the wavelength grid`);
    return value.map((item, index) => {
      const reflectance = expectFiniteNumber(item, `${label}[${index}]`);
      if (reflectance < 0 || reflectance > 1) fail(`${label} must remain in [0, 1]`);
      return reflectance;
    });
  }

  function normalizeSaunderson(value, label) {
    expectObject(value, label);
    if (value.mode === 'off') {
      expectExactKeys(value, ['mode'], label);
      return Object.freeze({ mode: 'off' });
    }
    if (value.mode !== 'fixed') fail(`${label}.mode must be exactly off or fixed`);
    expectExactKeys(value, ['mode', 'k1', 'k2'], label);
    const k1 = expectFiniteNumber(value.k1, `${label}.k1`);
    const k2 = expectFiniteNumber(value.k2, `${label}.k2`);
    if (k1 < 0 || k1 >= 1 || k2 < 0 || k2 >= 1) fail(`${label} fixed constants must be in [0, 1)`);
    return Object.freeze({ mode: 'fixed', k1, k2 });
  }

  function sameSaunderson(left, right) {
    return left.mode === right.mode
      && (left.mode === 'off' || (left.k1 === right.k1 && left.k2 === right.k2));
  }

  function validateManifestComponents(value) {
    if (!Array.isArray(value) || !value.length) fail('manifest.components must be a non-empty array');
    const byId = new Map();
    let baseCount = 0;

    value.forEach((component, index) => {
      const label = `manifest.components[${index}]`;
      expectExactKeys(component, ['component_id', 'batch_id', 'role'], label);
      const componentId = expectString(component.component_id, `${label}.component_id`);
      const batchId = expectString(component.batch_id, `${label}.batch_id`);
      if (component.role !== 'base' && component.role !== 'colorant') fail(`${label}.role must be base or colorant`);
      if (byId.has(componentId)) fail(`manifest.components repeats ${componentId}`);
      byId.set(componentId, Object.freeze({ componentId, batchId, role: component.role }));
      baseCount += Number(component.role === 'base');
    });

    if (baseCount !== 1) fail('manifest.components must contain exactly one base component');
    return byId;
  }

  function validateSourceBindings(manifest, model) {
    if (!Array.isArray(manifest.source_files) || !manifest.source_files.length) {
      fail('manifest.source_files must be a non-empty array');
    }
    if (!Array.isArray(model.provenance.source_files) || model.provenance.source_files.length !== manifest.source_files.length) {
      fail('Model provenance source-file bindings do not match manifest');
    }

    manifest.source_files.forEach((source, index) => {
      const label = `manifest.source_files[${index}]`;
      expectExactKeys(source, ['kind', 'path', 'sha256'], label);
      expectString(source.kind, `${label}.kind`);
      const path = expectString(source.path, `${label}.path`);
      const hash = expectHash(source.sha256, `${label}.sha256`);
      const modelSource = model.provenance.source_files[index];
      expectExactKeys(modelSource, ['path', 'sha256'], `model.provenance.source_files[${index}]`);
      if (modelSource.path !== path || modelSource.sha256 !== hash) {
        fail('Model provenance source-file bindings do not exactly match manifest');
      }
    });
  }

  function validateModelComponents(modelComponents, manifestComponents, wavelengths) {
    if (!Array.isArray(modelComponents) || modelComponents.length !== manifestComponents.size) {
      fail('Model components do not exactly match manifest components');
    }
    const manifestIds = Array.from(manifestComponents.keys());
    const curves = new Map();

    modelComponents.forEach((component, index) => {
      const label = `model.components[${index}]`;
      expectExactKeys(component, ['component_id', 'batch_id', 'K_mm_inv', 'S_mm_inv'], label);
      const expectedId = manifestIds[index];
      const expected = manifestComponents.get(expectedId);
      if (component.component_id !== expectedId || component.batch_id !== expected.batchId) {
        fail('Model component IDs and batch IDs must exactly match manifest components in order');
      }
      const k = component.K_mm_inv;
      const s = component.S_mm_inv;
      if (!Array.isArray(k) || !Array.isArray(s) || k.length !== wavelengths.length || s.length !== wavelengths.length) {
        fail(`Model component ${expectedId} curves must match the wavelength grid`);
      }
      const normalizedK = k.map((value, curveIndex) => {
        const coefficient = expectFiniteNumber(value, `Model ${expectedId} K_mm_inv[${curveIndex}]`);
        if (coefficient < 0) fail(`Model ${expectedId} K_mm_inv must be non-negative`);
        return coefficient;
      });
      const normalizedS = s.map((value, curveIndex) => {
        const coefficient = expectFiniteNumber(value, `Model ${expectedId} S_mm_inv[${curveIndex}]`);
        if (coefficient <= 0) fail(`Model ${expectedId} S_mm_inv must be strictly positive`);
        return coefficient;
      });
      curves.set(expectedId, Object.freeze({ k: normalizedK, s: normalizedS }));
    });

    return curves;
  }

  function validateFit(value) {
    expectExactKeys(value, [
      'backings_used',
      'dft_um_used',
      'jacobian_rank_min',
      'jacobian_rank_required',
      'training_fit',
      'training_records'
    ], 'model.fit');
    if (!Array.isArray(value.backings_used) || value.backings_used.length !== 2
      || value.backings_used[0] !== 'black' || value.backings_used[1] !== 'white') {
      fail('model.fit.backings_used must be exactly black and white');
    }
    if (!Array.isArray(value.dft_um_used) || value.dft_um_used.length < 2) {
      fail('model.fit.dft_um_used must contain at least two DFT values');
    }
    const seenDft = new Set();
    value.dft_um_used.forEach((dft, index) => {
      const normalizedDft = expectFiniteNumber(dft, `model.fit.dft_um_used[${index}]`);
      if (normalizedDft <= 0 || normalizedDft > 5000 || seenDft.has(normalizedDft)) {
        fail('model.fit.dft_um_used must contain unique realistic positive DFT values');
      }
      seenDft.add(normalizedDft);
    });
    ['jacobian_rank_min', 'jacobian_rank_required', 'training_records'].forEach(field => {
      if (!Number.isInteger(value[field]) || value[field] <= 0) fail(`model.fit.${field} must be a positive integer`);
    });
    if (value.jacobian_rank_min < value.jacobian_rank_required) {
      fail('model.fit must not report a deficient Jacobian rank');
    }
    expectExactKeys(value.training_fit, ['reflectance_rmse', 'reflectance_mae', 'reflectance_max_abs'], 'model.fit.training_fit');
    Object.entries(value.training_fit).forEach(([field, metric]) => {
      if (expectFiniteNumber(metric, `model.fit.training_fit.${field}`) < 0) {
        fail(`model.fit.training_fit.${field} must be non-negative`);
      }
    });
  }

  function validateModel(model, manifest) {
    expectExactKeys(manifest, [
      'schema_version',
      'dataset_status',
      'physical_ranking_enabled',
      'concentration_basis',
      'wavelength_nm',
      'locked_conditions',
      'components',
      'backings',
      'saunderson',
      'source_files',
      'splits'
    ], 'manifest');
    expectExactKeys(model, [
      'schema_version',
      'model_version',
      'status',
      'physical_ranking_enabled',
      'concentration_basis',
      'wavelength_nm',
      'saunderson',
      'components',
      'provenance',
      'fit'
    ], 'model');
    if (manifest.schema_version !== DATASET_SCHEMA) fail('Unsupported manifest schema_version');
    if (model.schema_version !== MODEL_SCHEMA) fail('Unsupported model schema_version');
    if (manifest.dataset_status !== 'synthetic_only' && manifest.dataset_status !== 'research_only') {
      fail('manifest.dataset_status must be synthetic_only or research_only');
    }
    if (model.status !== manifest.dataset_status) fail('Model status does not match manifest dataset status');
    if (model.physical_ranking_enabled !== false || manifest.physical_ranking_enabled !== false) {
      fail('v1 calibration artifacts may never enable physical ranking');
    }
    if (model.concentration_basis !== 'nonvolatile_volume_fraction'
      || manifest.concentration_basis !== 'nonvolatile_volume_fraction') {
      fail('Model and manifest concentration_basis must be nonvolatile_volume_fraction');
    }
    expectString(model.model_version, 'model.model_version');
    const manifestWavelengths = expectWavelengthGrid(manifest.wavelength_nm, 'manifest.wavelength_nm');
    const modelWavelengths = expectWavelengthGrid(model.wavelength_nm, 'model.wavelength_nm');
    expectMatchingWavelengths(modelWavelengths, manifestWavelengths);
    if (!isObject(manifest.locked_conditions) || !Object.keys(manifest.locked_conditions).length) {
      fail('manifest.locked_conditions must be a non-empty object');
    }

    const manifestSaunderson = normalizeSaunderson(manifest.saunderson, 'manifest.saunderson');
    const modelSaunderson = normalizeSaunderson(model.saunderson, 'model.saunderson');
    if (!sameSaunderson(modelSaunderson, manifestSaunderson)) {
      fail('Model Saunderson settings do not exactly match manifest settings');
    }
    const components = validateManifestComponents(manifest.components);
    const backings = expectObject(manifest.backings, 'manifest.backings');
    expectExactKeys(backings, ['black', 'white'], 'manifest.backings');
    const black = expectReflectance(backings.black && backings.black.reflectance, modelWavelengths.length, 'manifest.backings.black.reflectance');
    const white = expectReflectance(backings.white && backings.white.reflectance, modelWavelengths.length, 'manifest.backings.white.reflectance');
    expectExactKeys(backings.black, ['reflectance'], 'manifest.backings.black');
    expectExactKeys(backings.white, ['reflectance'], 'manifest.backings.white');
    if (black.every((value, index) => Math.abs(value - white[index]) <= 1e-12)) {
      fail('Black and white backing spectra must differ');
    }
    expectExactKeys(manifest.splits, ['train', 'validation', 'holdout'], 'manifest.splits');
    const splitFamilies = new Set();
    Object.entries(manifest.splits).forEach(([split, families]) => {
      if (!Array.isArray(families) || !families.length) {
        fail(`manifest.splits.${split} must be a non-empty array`);
      }
      families.forEach((family, index) => {
        const familyId = expectString(family, `manifest.splits.${split}[${index}]`);
        if (splitFamilies.has(familyId)) fail(`manifest.splits repeats formula family ${familyId}`);
        splitFamilies.add(familyId);
      });
    });

    expectExactKeys(model.provenance, ['dataset_manifest_sha256', 'source_files', 'fit_split'], 'model.provenance');
    expectHash(model.provenance.dataset_manifest_sha256, 'model.provenance.dataset_manifest_sha256');
    if (model.provenance.fit_split !== 'train') fail('model.provenance.fit_split must be train');
    validateSourceBindings(manifest, model);
    const curves = validateModelComponents(model.components, components, modelWavelengths);
    validateFit(model.fit);

    return Object.freeze({
      wavelengths: Object.freeze(modelWavelengths.slice()),
      saunderson: modelSaunderson,
      backings: Object.freeze({ black: Object.freeze(black), white: Object.freeze(white) }),
      components,
      curves
    });
  }

  function normalizeFormula(record, calibration) {
    expectObject(record, 'record');
    if (record.backing !== 'black' && record.backing !== 'white') fail('record.backing must be black or white');
    const dftUm = expectFiniteNumber(record.dft_um, 'record.dft_um');
    if (dftUm <= 0 || dftUm > 5000) fail('record.dft_um must be positive and no more than 5000');
    if (!Array.isArray(record.components) || !record.components.length) fail('record.components must be a non-empty array');

    const components = [];
    const seen = new Set();
    let total = 0;
    let hasBase = false;
    record.components.forEach((component, index) => {
      const label = `record.components[${index}]`;
      expectExactKeys(component, ['component_id', 'nonvolatile_volume_fraction'], label);
      const componentId = expectString(component.component_id, `${label}.component_id`);
      const fraction = expectFiniteNumber(component.nonvolatile_volume_fraction, `${label}.nonvolatile_volume_fraction`);
      if (fraction < 0) fail(`${label}.nonvolatile_volume_fraction must be non-negative`);
      if (seen.has(componentId)) fail(`record.components repeats ${componentId}`);
      const manifestComponent = calibration.components.get(componentId);
      if (!manifestComponent || !calibration.curves.has(componentId)) fail(`record uses undeclared component ${componentId}`);
      seen.add(componentId);
      total += fraction;
      hasBase = hasBase || (manifestComponent.role === 'base' && fraction > 0);
      components.push(Object.freeze({ componentId, fraction }));
    });
    if (!hasBase) fail('record.components must include a positive base fraction');
    if (Math.abs(total - 1) > 1e-9) fail('record component fractions must sum to 1');

    return Object.freeze({ backing: record.backing, thicknessMm: dftUm / 1000, components: Object.freeze(components) });
  }

  function mixCoefficients(calibration, formula) {
    const k = new Array(calibration.wavelengths.length).fill(0);
    const s = new Array(calibration.wavelengths.length).fill(0);
    formula.components.forEach(({ componentId, fraction }) => {
      const curves = calibration.curves.get(componentId);
      curves.k.forEach((coefficient, index) => { k[index] += fraction * coefficient; });
      curves.s.forEach((coefficient, index) => { s[index] += fraction * coefficient; });
    });
    return Object.freeze({ k: Object.freeze(k), s: Object.freeze(s) });
  }

  function finiteFilmReflectance(kMmInv, sMmInv, thicknessMm, backingReflectance) {
    if (!Array.isArray(kMmInv) || !Array.isArray(sMmInv) || !Array.isArray(backingReflectance)
      || kMmInv.length !== sMmInv.length || kMmInv.length !== backingReflectance.length || !kMmInv.length) {
      fail('K, S, and backing reflectance must be non-empty arrays of the same length');
    }
    const thickness = expectFiniteNumber(thicknessMm, 'thickness_mm');
    if (thickness <= 0) fail('thickness_mm must be strictly positive');

    return kMmInv.map((kValue, index) => {
      const k = expectFiniteNumber(kValue, `K_mm_inv[${index}]`);
      const s = expectFiniteNumber(sMmInv[index], `S_mm_inv[${index}]`);
      const backing = expectFiniteNumber(backingReflectance[index], `backing_reflectance[${index}]`);
      if (k < 0) fail('K_mm_inv must be non-negative');
      if (s <= 0) fail('S_mm_inv must be strictly positive');
      if (backing < 0 || backing > 1) fail('backing_reflectance must remain in [0, 1]');

      const ratio = k / s;
      const a = 1 + ratio;
      const b = Math.sqrt(ratio * (ratio + 2));
      const scatteringThickness = s * thickness;
      const argument = b * scatteringThickness;
      const u = Math.abs(argument) < 1e-5
        ? 1 / scatteringThickness
          + (b * b * scatteringThickness) / 3
          - (Math.pow(b, 4) * Math.pow(scatteringThickness, 3)) / 45
          + (2 * Math.pow(b, 6) * Math.pow(scatteringThickness, 5)) / 945
        : b / Math.tanh(argument);
      const denominator = a - backing + u;
      if (!Number.isFinite(denominator) || denominator <= 0) fail('Finite-film K-M denominator is invalid');
      const reflectance = (1 - backing * (a - u)) / denominator;
      if (!Number.isFinite(reflectance)) fail('Finite-film K-M result is non-finite');
      return Math.max(0, Math.min(1, reflectance));
    });
  }

  function applySaunderson(intrinsicReflectance, saunderson) {
    const normalized = normalizeSaunderson(saunderson, 'saunderson');
    const reflectance = expectReflectance(intrinsicReflectance, intrinsicReflectance && intrinsicReflectance.length, 'intrinsic_reflectance');
    if (normalized.mode === 'off') return reflectance;
    return reflectance.map(value => {
      const denominator = 1 - normalized.k2 * value;
      if (!Number.isFinite(denominator) || denominator <= 0) fail('Saunderson denominator is invalid');
      const measured = normalized.k1 + (1 - normalized.k1) * (1 - normalized.k2) * value / denominator;
      if (!Number.isFinite(measured)) fail('Saunderson result is non-finite');
      return Math.max(0, Math.min(1, measured));
    });
  }

  function predictReflectance(model, manifest, record) {
    const calibration = validateModel(model, manifest);
    const formula = normalizeFormula(record, calibration);
    const mixed = mixCoefficients(calibration, formula);
    const intrinsic = finiteFilmReflectance(mixed.k, mixed.s, formula.thicknessMm, calibration.backings[formula.backing]);
    return applySaunderson(intrinsic, calibration.saunderson);
  }

  function disabled(reason, structuralEligible) {
    return Object.freeze({
      enabled: false,
      disabled: true,
      structuralEligible: Boolean(structuralEligible),
      verification: structuralEligible ? 'structural_only' : 'none',
      cryptographicallyVerified: false,
      reason
    });
  }

  function expectProductionBinding(value, expectedHash, label) {
    expectExactKeys(value, ['path', 'sha256'], label);
    expectString(value.path, `${label}.path`);
    if (expectHash(value.sha256, `${label}.sha256`) !== expectedHash) {
      fail(`${label}.sha256 does not match the supplied artifact hash`);
    }
  }

  function assessFutureStructuralGate(artifacts) {
    const { manifest, model, evaluation, receipt, artifactHashes } = artifacts;
    expectExactKeys(artifactHashes, ['manifest', 'model', 'evaluation'], 'artifactHashes');
    const manifestHash = expectHash(artifactHashes.manifest, 'artifactHashes.manifest');
    const modelHash = expectHash(artifactHashes.model, 'artifactHashes.model');
    const evaluationHash = expectHash(artifactHashes.evaluation, 'artifactHashes.evaluation');

    if (manifest.schema_version !== PRODUCTION_SCHEMA.dataset || model.schema_version !== PRODUCTION_SCHEMA.model
      || evaluation.schema_version !== PRODUCTION_SCHEMA.evaluation || receipt.schema_version !== PRODUCTION_SCHEMA.receipt) {
      fail('Future structural production gate requires distinct production schemas');
    }
    [manifest, model, evaluation, receipt].forEach((artifact, index) => {
      if (artifact.status !== PRODUCTION_SCHEMA.status || artifact.production_pass !== true) {
        fail(`Future production artifact ${index} must have production_candidate status and production_pass true`);
      }
    });
    if (manifest.physical_ranking_enabled !== true || model.physical_ranking_enabled !== true) {
      fail('Future production artifacts must explicitly request physical ranking');
    }
    expectObject(model.provenance, 'model.provenance');
    if (expectHash(model.provenance.dataset_manifest_sha256, 'model.provenance.dataset_manifest_sha256') !== manifestHash) {
      fail('Future model provenance does not bind the supplied manifest hash');
    }
    if (expectHash(evaluation.dataset_manifest_sha256, 'evaluation.dataset_manifest_sha256') !== manifestHash
      || expectHash(evaluation.model_sha256, 'evaluation.model_sha256') !== modelHash) {
      fail('Future evaluation does not bind the supplied manifest/model hashes');
    }
    expectObject(receipt.bindings, 'receipt.bindings');
    expectProductionBinding(receipt.bindings.manifest, manifestHash, 'receipt.bindings.manifest');
    expectProductionBinding(receipt.bindings.model, modelHash, 'receipt.bindings.model');
    expectProductionBinding(receipt.bindings.evaluation, evaluationHash, 'receipt.bindings.evaluation');

    return disabled(
      'Structural production bindings are complete, but this DOM-free adapter does not cryptographically verify artifacts or activate ranking.',
      true
    );
  }

  function assessProductionRankingEligibility(artifacts) {
    try {
      expectObject(artifacts, 'artifacts');
      const { manifest, model, evaluation, receipt } = artifacts;
      expectObject(manifest, 'artifacts.manifest');
      expectObject(model, 'artifacts.model');
      expectObject(evaluation, 'artifacts.evaluation');
      expectObject(receipt, 'artifacts.receipt');

      const isIndependentHoldoutReviewReceipt = receipt.schema_version === INDEPENDENT_HOLDOUT_REVIEW_RECEIPT_SCHEMA
        || receipt.state === ACTIVATION_REVIEW_READY;
      if (isIndependentHoldoutReviewReceipt) {
        const structuralEligible = receipt.schema_version === INDEPENDENT_HOLDOUT_REVIEW_RECEIPT_SCHEMA
          && receipt.state === ACTIVATION_REVIEW_READY;
        const syntheticEvidence = receipt.evidence_class === 'synthetic_test_only';
        return disabled(
          syntheticEvidence
            ? 'Synthetic independent-holdout evidence cannot activate ranking; a future pinned production-authority verifier is absent.'
            : 'Independent-holdout review receipts are structural evidence only; a future pinned production-authority verifier is absent.',
          structuralEligible
        );
      }

      if (receipt.schema_version === RESEARCH_RECEIPT_SCHEMA
        || model.status === 'synthetic_only' || model.status === 'research_only'
        || manifest.dataset_status === 'synthetic_only' || manifest.dataset_status === 'research_only') {
        validateModel(model, manifest);
        return disabled('Synthetic-only and research-only calibration artifacts cannot enable production ranking.');
      }
      return assessFutureStructuralGate(artifacts);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return disabled(`Invalid physical optical artifacts: ${message}`);
    }
  }

  return Object.freeze({
    PhysicalOpticalModelError,
    applySaunderson,
    assessProductionRankingEligibility,
    finiteFilmReflectance,
    predictReflectance,
    validateModel
  });
}));
