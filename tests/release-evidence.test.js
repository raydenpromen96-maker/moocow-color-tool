const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const baseline = require('./fixtures/ral216-v4.4-baseline.json');
const readme = fs.readFileSync(path.join(__dirname, '..', 'README.md'), 'utf8');
const changelog = fs.readFileSync(path.join(__dirname, '..', 'CHANGELOG.md'), 'utf8');

test('v4.5 release claims retain the reproducible v4.4 baseline receipt', () => {
  assert.equal(baseline.version, '4.4.0');
  assert.equal(baseline.summary.runs, 216);
  assert.equal(baseline.summary.stableRecommended, 109);
  assert.equal(baseline.summary.meanTwoCoatDE, 3.93697849);
  assert.equal(baseline.summary.grades.fail, 96);
  assert.match(baseline.outputSha256, /^[a-f0-9]{64}$/);
  assert.match(readme, /稳定首选由 `109` 提升到 `197`/);
  assert.match(readme, /失败等级由 `96` 降到 `73`/);
  assert.match(changelog, /197 stable recommendations, up from 109/);
  assert.match(changelog, /8\/31\/104\/73/);
});
