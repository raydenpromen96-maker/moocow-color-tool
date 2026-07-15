const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const PhysicalOpticalModel = require('../src/physical-optical-model');

const ROOT = path.join(__dirname, '..');
const DATASET = path.join(ROOT, 'data', 'calibration', 'example-synthetic');

function readJson(...parts) {
  return JSON.parse(fs.readFileSync(path.join(...parts), 'utf8'));
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function loadArtifacts() {
  return {
    fixture: readJson(ROOT, 'tests', 'fixtures', 'physical', 'km_synthetic_fixture.json'),
    manifest: readJson(DATASET, 'manifest.json'),
    model: readJson(DATASET, 'km-synthetic-v1-model.json'),
    evaluation: readJson(DATASET, 'km-synthetic-v1-evaluation.json'),
    receipt: readJson(DATASET, 'km-synthetic-v1-receipt.json'),
    measurements: readJson(DATASET, 'sources', 'synthetic-measurements.json')
  };
}

function maxAbsoluteDifference(left, right) {
  return Math.max(...left.map((value, index) => Math.abs(value - right[index])));
}

test('exports a deterministic CommonJS and browser UMD API without a DOM dependency', () => {
  const source = fs.readFileSync(path.join(ROOT, 'src', 'physical-optical-model.js'), 'utf8');
  const page = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
  const browserContext = { window: {} };

  vm.runInNewContext(source, browserContext);

  assert.deepEqual(Object.keys(browserContext.window.MooCowPhysicalOpticalModel).sort(), Object.keys(PhysicalOpticalModel).sort());
  assert.equal(source.includes('document.'), false);
  assert.doesNotMatch(page, /physical-optical-model|MooCowPhysicalOpticalModel/);
});

test('matches Python synthetic black and white measurement records with the real model artifacts', () => {
  const { fixture, manifest, model, measurements } = loadArtifacts();
  const records = measurements.measurements;
  const black = records.filter(record => record.backing === 'black');
  const white = records.filter(record => record.backing === 'white');

  assert.equal(records.length, fixture.expected.measurement_count);
  assert.ok(black.length > 0);
  assert.ok(white.length > 0);
  PhysicalOpticalModel.validateModel(model, manifest);

  [black, white].forEach(backingRecords => {
    backingRecords.forEach(record => {
      const predicted = PhysicalOpticalModel.predictReflectance(model, manifest, record);
      assert.equal(predicted.length, record.reflectance.length);
      assert.ok(
        maxAbsoluteDifference(predicted, record.reflectance) < 2e-10,
        `${record.measurement_id} exceeds Python parity tolerance`
      );
    });
  });
});

test('uses the stable finite-film small-argument branch and fixed/off Saunderson transforms', () => {
  const thin = PhysicalOpticalModel.finiteFilmReflectance([0], [10], 1e-8, [0.5]);
  const off = PhysicalOpticalModel.applySaunderson([0.2, 0.8], { mode: 'off' });
  const fixed = PhysicalOpticalModel.applySaunderson([0.2, 0.8], { mode: 'fixed', k1: 0.035, k2: 0.075 });

  assert.ok(Number.isFinite(thin[0]));
  assert.deepEqual(off, [0.2, 0.8]);
  assert.ok(fixed[0] > off[0]);
  assert.ok(fixed[1] < off[1]);
});

test('always disables the current research receipt, including a tampered production_pass flag', () => {
  const { manifest, model, evaluation, receipt } = loadArtifacts();
  const disabled = PhysicalOpticalModel.assessProductionRankingEligibility({ manifest, model, evaluation, receipt });
  const tamperedReceipt = clone(receipt);
  tamperedReceipt.production_pass = true;
  const stillDisabled = PhysicalOpticalModel.assessProductionRankingEligibility({
    manifest,
    model,
    evaluation,
    receipt: tamperedReceipt
  });

  [disabled, stillDisabled].forEach(result => {
    assert.equal(result.enabled, false);
    assert.equal(result.disabled, true);
    assert.equal(result.structuralEligible, false);
    assert.equal(result.cryptographicallyVerified, false);
  });
});

test('keeps open-selection fit-export candidates ineligible despite caller-mutated promotion booleans', () => {
  const disabledCandidate = {
    manifest: {
      schema_version: 'moocow-open-selection-dataset-v1',
      dataset_status: 'open_selection_only'
    },
    model: {
      schema_version: 'moocow-open-selection-km-fit-model-v1',
      status: 'open_selection_fit_candidate',
      dataset_status: 'open_selection_only'
    },
    evaluation: {
      schema_version: 'moocow-open-selection-km-selection-evaluation-v1',
      status: 'open_selection_fit_candidate',
      dataset_status: 'open_selection_only'
    },
    receipt: {
      schema_version: 'moocow-open-selection-km-fit-export-receipt-v1',
      status: 'open_selection_fit_exported',
      state: 'OPEN_SELECTION_FIT_EXPORTED',
      dataset_status: 'open_selection_only'
    }
  };
  const callerMutatedCandidate = clone(disabledCandidate);
  const promotionFields = [
    'production_pass',
    'runtime_compatible',
    'pilot_acquisition_permitted',
    'open_admission_permitted',
    'model_fitting_permitted',
    'holdout_release_permitted',
    'physical_ranking_enabled',
    'promotion_permitted'
  ];

  Object.values(callerMutatedCandidate).forEach(artifact => {
    artifact.status = 'production_candidate';
    artifact.dataset_status = 'production_candidate';
    promotionFields.forEach(field => {
      artifact[field] = true;
    });
  });

  [disabledCandidate, callerMutatedCandidate].forEach(candidate => {
    const result = PhysicalOpticalModel.assessProductionRankingEligibility(candidate);
    assert.equal(result.enabled, false);
    assert.equal(result.disabled, true);
    assert.equal(result.structuralEligible, false);
    assert.equal(result.cryptographicallyVerified, false);
  });
});

test('fails closed on malformed curves, partial fractions, and batch mismatches', () => {
  const { manifest, model, evaluation, receipt, measurements } = loadArtifacts();
  const malformed = clone(model);
  malformed.components[0].S_mm_inv.pop();
  const wrongBatch = clone(model);
  wrongBatch.components[1].batch_id = 'WRONG-BATCH';
  const emptyHoldout = clone(manifest);
  emptyHoldout.splits.holdout = [];
  const partial = clone(measurements.measurements[0]);
  partial.components[0].nonvolatile_volume_fraction = 0.9;

  assert.throws(
    () => PhysicalOpticalModel.validateModel(malformed, manifest),
    PhysicalOpticalModel.PhysicalOpticalModelError
  );
  assert.throws(
    () => PhysicalOpticalModel.validateModel(wrongBatch, manifest),
    PhysicalOpticalModel.PhysicalOpticalModelError
  );
  assert.throws(
    () => PhysicalOpticalModel.validateModel(model, emptyHoldout),
    PhysicalOpticalModel.PhysicalOpticalModelError
  );
  assert.throws(
    () => PhysicalOpticalModel.predictReflectance(model, manifest, partial),
    PhysicalOpticalModel.PhysicalOpticalModelError
  );
  assert.equal(
    PhysicalOpticalModel.assessProductionRankingEligibility({ manifest, model: wrongBatch, evaluation, receipt }).enabled,
    false
  );
});

test('future structural bindings remain disabled without cryptographic verification', () => {
  const hashes = {
    manifest: 'a'.repeat(64),
    model: 'b'.repeat(64),
    evaluation: 'c'.repeat(64)
  };
  const result = PhysicalOpticalModel.assessProductionRankingEligibility({
    artifactHashes: hashes,
    manifest: {
      schema_version: 'moocow-km-production-calibration-dataset-v1',
      status: 'production_candidate',
      production_pass: true,
      physical_ranking_enabled: true
    },
    model: {
      schema_version: 'moocow-km-two-constant-production-model-v1',
      status: 'production_candidate',
      production_pass: true,
      physical_ranking_enabled: true,
      provenance: { dataset_manifest_sha256: hashes.manifest }
    },
    evaluation: {
      schema_version: 'moocow-km-production-evaluation-v1',
      status: 'production_candidate',
      production_pass: true,
      dataset_manifest_sha256: hashes.manifest,
      model_sha256: hashes.model
    },
    receipt: {
      schema_version: 'moocow-km-production-receipt-v1',
      status: 'production_candidate',
      production_pass: true,
      bindings: {
        manifest: { path: 'manifest.json', sha256: hashes.manifest },
        model: { path: 'model.json', sha256: hashes.model },
        evaluation: { path: 'evaluation.json', sha256: hashes.evaluation }
      }
    }
  });

  assert.equal(result.enabled, false);
  assert.equal(result.structuralEligible, true);
  assert.equal(result.verification, 'structural_only');
  assert.equal(result.cryptographicallyVerified, false);
});

test('keeps independent-holdout review receipts disabled regardless of raw or caller-mutated activation fields', () => {
  const { manifest, model, evaluation } = loadArtifacts();
  const rawReviewReceipt = {
    schema_version: 'moocow-independent-holdout-review-receipt-v1',
    state: 'ACTIVATION_REVIEW_READY',
    evidence_class: 'measured_current_batch'
  };
  const callerMutatedReceipt = clone(rawReviewReceipt);
  const syntheticReceipt = clone(rawReviewReceipt);
  const unsignedReceipt = clone(rawReviewReceipt);
  const promotionFields = [
    'production_pass',
    'runtime_compatible',
    'physical_ranking_enabled',
    'promotion_permitted',
    'activation_review_eligible'
  ];

  promotionFields.forEach(field => {
    callerMutatedReceipt[field] = true;
  });
  callerMutatedReceipt.release_replay_protected = true;
  syntheticReceipt.evidence_class = 'synthetic_test_only';
  syntheticReceipt.state = 'SYNTHETIC_EVALUATION_ONLY';
  unsignedReceipt.signature = { algorithm: 'Ed25519', verified: true };

  [rawReviewReceipt, callerMutatedReceipt, syntheticReceipt, unsignedReceipt].forEach(receipt => {
    const result = PhysicalOpticalModel.assessProductionRankingEligibility({ manifest, model, evaluation, receipt });

    assert.equal(result.enabled, false);
    assert.equal(result.disabled, true);
    assert.equal(result.cryptographicallyVerified, false);
    assert.match(result.reason, /pinned production-authority verifier is absent/);
  });
});
