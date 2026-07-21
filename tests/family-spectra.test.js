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

test('profiles bundle the nine exact Golden CIs plus declared measured/anchored proxies', () => {
  // v5: 新增 4 支 CHSOS 实测代理（PY74/PB15:1/PO73/PW6）+ 1 支 PG7 平移锚定（PG36），
  // 替换原先引擎内手工编造的近似曲线；全部为代理数据，待 45 卡实测校准替换。
  assert.deepEqual(
    Object.keys(FamilySpectra.PROFILES).sort(),
    ['PB15:1', 'PB15:3', 'PBk7', 'PG7', 'PG36', 'PO73', 'PR101', 'PR122', 'PR254', 'PV23', 'PW6', 'PY42', 'PY74', 'PY83'].sort()
  );
  const golden = Object.values(FamilySpectra.PROFILES).filter(p => p.status === 'exact_ci_waterborne_acrylic_reference');
  assert.deepEqual(golden.map(p => p.ci).sort(), ['PB15:3', 'PBk7', 'PG7', 'PR101', 'PR122', 'PR254', 'PV23', 'PY42', 'PY83'].sort());
  for (const ci of ['PY74', 'PB15:1', 'PO73', 'PW6']) {
    assert.equal(FamilySpectra.PROFILES[ci].status, 'chsos_measured_acrylic_proxy_reference');
    assert.equal(FamilySpectra.PROFILES[ci].sourceId, FamilySpectra.CHSOS_SOURCE.id);
  }
  const pg36 = FamilySpectra.PROFILES.PG36;
  assert.equal(pg36.status, 'pg7_shift_masstone_anchored_proxy_reference');
  assert.equal(pg36.sourceId, FamilySpectra.PG36_SOURCE.id);
  assert.deepEqual(pg36.source.anchorLab, [27.82, -11.83, -0.17]);
});

test('all bundled Golden profiles match the reproducible source manifest and full-profile digests', () => {
  assert.deepEqual(manifest.source.wavelengths, FamilySpectra.WAVELENGTHS);
  assert.equal(manifest.source.zipSha256, FamilySpectra.SOURCE.sourceZipSha256);
  assert.equal(manifest.source.workbookSha256, FamilySpectra.SOURCE.spreadsheetSha256);
  // manifest 只覆盖 GOLDEN 来源档案；CHSOS/PG36 代理档案另有来源声明，不在该 manifest 内。
  const goldenProfiles = Object.fromEntries(
    Object.entries(FamilySpectra.PROFILES).filter(([, p]) => p.sourceId === FamilySpectra.SOURCE.id)
  );
  assert.deepEqual(Object.keys(manifest.profiles).sort(), Object.keys(goldenProfiles).sort());

  for (const [ci, expected] of Object.entries(manifest.profiles)) {
    const actual = FamilySpectra.PROFILES[ci];
    assert.equal(actual.productNumber, expected.productNumber, `${ci} product number`);
    assert.deepEqual(actual.reflectance, expected.reflectance, `${ci} reflectance`);
    assert.deepEqual(actual.kOverS, expected.kOverS, `${ci} K/S`);
    const payload = JSON.stringify({ ci, productNumber: actual.productNumber, reflectance: actual.reflectance, kOverS: actual.kOverS });
    assert.equal(crypto.createHash('sha256').update(payload).digest('hex').toUpperCase(), expected.profileSha256, `${ci} digest`);
  }
});

test('coverage separates exact, measured-proxy, and unsupported CIs and fails closed', () => {
  const coverage = FamilySpectra.summarizeCoverage([
    { ci: 'PY83', fraction: 0.5 },
    { ci: 'PB15:3', fraction: 0.25 },
    { ci: 'PO73', fraction: 0.125 },
    { ci: null, fraction: 0.125 }
  ]);
  assert.equal(coverage.exactFraction, 0.75);
  assert.equal(coverage.proxyFraction, 0.125);
  assert.equal(coverage.missingFraction, 0.125);
  assert.deepEqual(coverage.proxyCi, ['PO73']);
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
