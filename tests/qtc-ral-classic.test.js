const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const ColorCore = require('../src/color-core.js');
const catalogue = require('../src/qtc-ral-classic.js');
const snapshot = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'qtc-ral-classic.json'), 'utf8'));
const EXPECTED_CODE_SET_SHA256 = '50cbb1bd23b9510fa5c8d7d561717166c0a87b7439b93f7b8de5d12ccae688fa';

test('QTC snapshot contains the complete ordered 216-colour catalogue', () => {
  assert.equal(catalogue.schemaVersion, 1);
  assert.equal(catalogue.count, 216);
  assert.equal(catalogue.colors.length, 216);
  assert.equal(new Set(catalogue.colors.map(color => color.code)).size, 216);
  assert.equal(catalogue.colors[0].code, '1000');
  assert.equal(catalogue.colors.at(-1).code, '9023');
  assert.deepEqual(catalogue.colors.map(color => color.qtcIndex), Array.from({ length: 216 }, (_, index) => index + 1));
  assert.equal(catalogue.source.approvedCodeSetSha256, EXPECTED_CODE_SET_SHA256);
});

test('QTC colour records have consistent HEX, RGB, and finite Lab targets', () => {
  let maxDeltaE = 0;
  for (const color of catalogue.colors) {
    assert.match(color.ral, /^RAL \d{4}$/);
    assert.match(color.hex, /^#[0-9A-F]{6}$/);
    assert.equal(ColorCore.rgbToHex(color.rgb), color.hex);
    assert.equal(color.rgb.length, 3);
    assert.equal(color.targetLab.length, 3);
    assert.ok(color.rgb.every(value => Number.isInteger(value) && value >= 0 && value <= 255));
    assert.ok(color.targetLab.every(Number.isFinite));
    assert.ok(color.name_zh.length > 0);
    assert.equal(color.name_en, color.name_en.trim());
    assert.equal(color.name_zh, color.name_zh.trim());
    assert.ok(Number.isInteger(color.qtcColorId) && color.qtcColorId > 0);
    maxDeltaE = Math.max(maxDeltaE, ColorCore.deltaE2000(color.targetLab, ColorCore.hexToLab(color.hex)));
  }
  assert.ok(maxDeltaE < 0.02, `QTC Lab/HEX mismatch too large: ${maxDeltaE}`);
  assert.deepEqual(catalogue.colors.filter(color => !color.name_en).map(color => color.code), ['7048']);
});

test('runtime JS and provenance JSON contain the same immutable snapshot payload', () => {
  assert.deepEqual(catalogue, snapshot);
  assert.equal(catalogue.source.provider, 'QTC Color');
  assert.equal(catalogue.source.valueType, 'computer-simulated screen reference');
  assert.match(catalogue.source.disclaimer, /physical colour card/);
  assert.match(catalogue.colorsSha256, /^[0-9a-f]{64}$/);
  assert.match(catalogue.source.directoryResponseSha256, /^[0-9a-f]{64}$/);
  assert.match(catalogue.source.detailResponsesSha256, /^[0-9a-f]{64}$/);
});
