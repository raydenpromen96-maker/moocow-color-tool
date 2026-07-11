const assert = require('node:assert/strict');
const test = require('node:test');

const FamilySpectra = require('../src/family-spectra.js');

test('MIT optical-prior profiles are valid and preserve the declared 30 nm grid', () => {
  assert.deepEqual(FamilySpectra.WAVELENGTHS, [400, 430, 460, 490, 520, 550, 580, 610, 640, 670, 700]);
  for (const value of Object.values(FamilySpectra.PROFILES)) assert.deepEqual(FamilySpectra.validateProfile(value), { valid: true, errors: [] });
});

test('only exact CIs present in the licensed source are bundled', () => {
  assert.deepEqual(Object.keys(FamilySpectra.PROFILES).sort(), ['PB15:3', 'PG7', 'PR122', 'PV23', 'PW6', 'PY74'].sort());
  for (const value of Object.values(FamilySpectra.PROFILES)) assert.equal(value.status, 'exact_ci_optical_prior');
  for (const ci of ['PY83', 'PB15:1', 'PR254', 'PR101', 'PY42', 'PBk7', 'PO13']) assert.equal(FamilySpectra.PROFILES[ci], undefined);
});

test('numeric fixtures match the hashed source files', () => {
  assert.equal(FamilySpectra.PROFILES.PY74.absorption[0], 0.532006043);
  assert.equal(FamilySpectra.PROFILES.PY74.reducedScattering[0], 0.042481238);
  assert.equal(FamilySpectra.PROFILES['PB15:3'].absorption[7], 0.63616266);
  assert.equal(FamilySpectra.PROFILES.PW6.reducedScattering[10], 0.347988119);
  assert.equal(FamilySpectra.SOURCE.absorptionSha256, '8424BBFC20AE534D0ED295E82A022F3E4A617AAA5E5A4F9D16A9D8324F653014');
  assert.equal(FamilySpectra.SOURCE.reducedScatteringSha256, '6F13699B07CACB43605913F0C92F8E3D855DC8FD20466ED4FD4E7328EFDCF354');
});

test('shape normalization has exact fixtures and is scale invariant', () => {
  assert.deepEqual(FamilySpectra.normalizeShape([0, 1, 2]), [0, 0.5, 1]);
  assert.deepEqual(FamilySpectra.normalizeShape([0, 10, 20]), [0, 0.5, 1]);
  assert.deepEqual(FamilySpectra.normalizeShape([0, 0, 0]), [0, 0, 0]);
  assert.equal(FamilySpectra.normalizeShape([-1, 0, 1]), null);
  const shape = FamilySpectra.opticalShape(FamilySpectra.PROFILES.PG7);
  assert.equal(Math.max(...shape.absorption), 1);
  assert.equal(Math.max(...shape.reducedScattering), 1);
});

test('coverage is weighted and unsupported or unverified CIs fail closed', () => {
  const coverage = FamilySpectra.summarizeCoverage([{ ci: 'PW6', fraction: 0.5 }, { ci: 'PB15:3', fraction: 0.25 }, { ci: null, fraction: 0.25 }]);
  assert.equal(coverage.exactFraction, 0.75);
  assert.equal(coverage.proxyFraction, 0);
  assert.equal(coverage.missingFraction, 0.25);
  assert.deepEqual(coverage.proxyCi, []);
  assert.deepEqual(coverage.missingCi, ['CI-unverified']);
  assert.equal(coverage.predictiveEligible, false);
});

test('source metadata preserves modality, matrix, hashes, license, and non-predictive boundary', () => {
  assert.equal(FamilySpectra.SOURCE.license, 'MIT');
  assert.match(FamilySpectra.SOURCE.measurement, /pigment-in-epoxy/);
  assert.match(FamilySpectra.SOURCE.measurement, /absorption mu_a and reduced scattering mu_s_prime/);
  assert.equal(FamilySpectra.SOURCE.usage, 'shadow_shape_evidence_only');
  assert.equal(FamilySpectra.RESEARCH_REFERENCES.ritArtistPaint.dataBundled, false);
  assert.equal(FamilySpectra.RESEARCH_REFERENCES.colanylGreenGg.dataBundled, false);
});
