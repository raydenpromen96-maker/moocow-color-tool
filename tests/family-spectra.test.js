const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const FamilySpectra = require('../src/family-spectra.js');
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'golden-family-spectra-manifest.json'), 'utf8'));

test('waterborne acrylic profiles are valid on the declared 30 nm grid', () => {
  assert.deepEqual(FamilySpectra.WAVELENGTHS, [400, 430, 460, 490, 520, 550, 580, 610, 640, 670, 700]);
  for (const value of Object.values(FamilySpectra.PROFILES)) {
    assert.deepEqual(FamilySpectra.validateProfile(value), { valid: true, errors: [] });
  }
});

test('only exact CIs present in the shared Golden source are bundled', () => {
  assert.deepEqual(Object.keys(FamilySpectra.PROFILES).sort(), ['PB15:3', 'PBk7', 'PG7', 'PR101', 'PR122', 'PR254', 'PV23', 'PY42', 'PY83'].sort());
  for (const value of Object.values(FamilySpectra.PROFILES)) assert.equal(value.status, 'exact_ci_waterborne_acrylic_reference');
  for (const ci of ['PY74', 'PB15:1', 'PW6', 'PO13']) assert.equal(FamilySpectra.PROFILES[ci], undefined);
});

test('all bundled profiles match the reproducible source manifest and full-profile digests', () => {
  assert.deepEqual(manifest.source.wavelengths, FamilySpectra.WAVELENGTHS);
  assert.equal(manifest.source.zipSha256, FamilySpectra.SOURCE.sourceZipSha256);
  assert.equal(manifest.source.workbookSha256, FamilySpectra.SOURCE.spreadsheetSha256);
  assert.deepEqual(Object.keys(manifest.profiles).sort(), Object.keys(FamilySpectra.PROFILES).sort());

  for (const [ci, expected] of Object.entries(manifest.profiles)) {
    const actual = FamilySpectra.PROFILES[ci];
    assert.equal(actual.productNumber, expected.productNumber, `${ci} product number`);
    assert.deepEqual(actual.reflectance, expected.reflectance, `${ci} reflectance`);
    assert.deepEqual(actual.kOverS, expected.kOverS, `${ci} K/S`);
    const payload = JSON.stringify({ ci, productNumber: actual.productNumber, reflectance: actual.reflectance, kOverS: actual.kOverS });
    assert.equal(crypto.createHash('sha256').update(payload).digest('hex').toUpperCase(), expected.profileSha256, `${ci} digest`);
  }
});

test('coverage is weighted and unsupported or unverified CIs fail closed', () => {
  const coverage = FamilySpectra.summarizeCoverage([
    { ci: 'PY83', fraction: 0.5 },
    { ci: 'PB15:3', fraction: 0.25 },
    { ci: null, fraction: 0.25 }
  ]);
  assert.equal(coverage.exactFraction, 0.75);
  assert.equal(coverage.proxyFraction, 0);
  assert.equal(coverage.missingFraction, 0.25);
  assert.deepEqual(coverage.proxyCi, []);
  assert.deepEqual(coverage.missingCi, ['CI-unverified']);
  assert.equal(coverage.predictiveEligible, false);
});

test('source metadata preserves matrix, conditions, permission wording, and non-ranking boundary', () => {
  assert.equal(FamilySpectra.SOURCE.matrix, 'water_based_heavy_body_acrylic');
  assert.match(FamilySpectra.SOURCE.measurement, /6 mil dry/);
  assert.match(FamilySpectra.SOURCE.measurement, /white Leneta card/);
  assert.match(FamilySpectra.SOURCE.permission, /allowed the hosts to share/);
  assert.match(FamilySpectra.SOURCE.permission, /no named data licence/);
  assert.equal(FamilySpectra.SOURCE.usage, 'waterborne_acrylic_shadow_reference_only');
});
