const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ColorCore = require('../src/color-core.js');
const RecipeSearch = require('../src/recipe-search.js');
const FamilySpectra = require('../src/family-spectra.js');
const PaintCatalog = require('../src/paint-catalog.js');
const ProductionRuntime = require('../src/production-runtime.js');
const qtcCatalogue = require('../src/qtc-ral-classic.js');

const REPRESENTATIVE_HASHES = Object.freeze({
  '1021': 'f46e202a2a6fb4df5a63794effcaee6a2985cba7620f52ad725a058484cf581e',
  '1035': '6f23f32c3fc65b7398866564a6a4f4dee05c834a4f3dfc94c64fe6948890b541',
  '2004': 'afe5c924b58054f0c965a3d25452a6d0eb99108cf07e285b8b459e694bd05d73',
  '3020': 'cac0cb1ec3928cea53589b7aa493e7071c91c92ac0b446d8e38e9cc4df2e5836',
  '4005': '963da0fce54f361ecfa764b59a21c878f2e32cf3167cebcdc7039b36ea1af2a6',
  '5005': 'a8334319cb9b102fb10bdec05a28c943d93571deb94216b9b486ff35030df9d9',
  '5015': '2e721c5a1890b42ea18db952975618b56b73c948279c9881f5479c7e8da7b92b',
  '6005': 'ca5fc2c0df67cee06844b879172f36ec9f055f8d390d7529d64599c7ff2015fb',
  '6037': '69c80b44d68a8140be86e9454afc07fda319d3e830d5567ff62ab5079238b8dc',
  '7035': 'db92440263ff572bdfb3edcf611076ac7ca7e4061b19b156837c77b103477d84',
  '8004': 'd5d3a415ac4ca8dfb667a23b39c57e22ed40c9f7c46c1ec12fcf32736514515b',
  '9005': 'fb80a56f4710a101f6dc98ee8fff55858f4a8a2f7852ba3e057146fa5d8d2e2b',
  '9010': '98bd259cf6c0779c82043eb83d1214b1700c1f35985aa0dac2b7aa807f25c269'
});

function createRuntime() {
  return ProductionRuntime.create({
    ColorCore,
    RecipeSearch,
    FamilySpectra,
    paintCatalog: PaintCatalog.PAINT_DATA
  });
}

function target(code) {
  return PaintCatalog.buildTargets(qtcCatalogue).find(color => color.code === code);
}

function representativeHash(runtime, code) {
  const color = target(code);
  const candidates = runtime.generateCandidates(color);
  const payload = {
    code,
    targetLabSource: runtime.resolveTargetColor(color).targetLabSource,
    candidates: candidates.map(candidate => ({
      recipe: candidate.recipe,
      metrics: candidate.metrics,
      score: candidate.score,
      supportKey: candidate.supportKey
    }))
  };
  return {
    candidates,
    hash: crypto.createHash('sha256').update(JSON.stringify(payload)).digest('hex')
  };
}

test('runtime is a DOM-free UMD factory with injected dependencies', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'production-runtime.js'), 'utf8');
  assert.doesNotMatch(source, /\bdocument\b|\bwindow\b/);
  const browserContext = {};
  vm.runInNewContext(source, browserContext);
  assert.deepEqual(Object.keys(browserContext.MooCowProductionRuntime), ['create']);
  assert.throws(() => ProductionRuntime.create({}), /requires ColorCore, RecipeSearch, and paintCatalog/);
  const runtime = createRuntime();
  assert.equal(Object.keys(runtime.catalog).length, 14);
  assert.equal(Object.isFrozen(runtime.catalog), true);
  Object.values(runtime.catalog).forEach(pigment => assert.equal(Object.isFrozen(pigment), true));
  assert.equal(runtime.preparePigments(), runtime.catalog);
});

test('QTC Lab takes precedence over display HEX with a finite fallback', () => {
  const runtime = createRuntime();
  const explicit = runtime.resolveTargetColor({ hex: '#FFFFFF', targetLab: [12.34, -5.67, 8.9] });
  const fallback = runtime.resolveTargetColor({ hex: '#FFFFFF' });
  assert.deepEqual(explicit, { targetRgb: [255, 255, 255], targetLab: [12.34, -5.67, 8.9], targetLabSource: 'qtcLab' });
  assert.equal(fallback.targetLabSource, 'hexFallback');
  assert.ok(fallback.targetLab.every(Number.isFinite));
});

test('candidate recipes use the canonical 106 g/L grid and recompute their metrics', { timeout: 120000 }, () => {
  const runtime = createRuntime();
  const color = target('1021');
  const { candidates, hash } = representativeHash(runtime, '1021');
  assert.equal(hash, REPRESENTATIVE_HASHES['1021']);
  assert.ok(candidates.length > 0);
  candidates.forEach(candidate => {
    const amounts = Object.values(candidate.recipeGpl);
    assert.ok(amounts.length <= 4);
    assert.equal(amounts.reduce((sum, value) => sum + value, 0), 106);
    amounts.forEach(value => {
      assert.ok(value >= 1);
      assert.ok(Number.isInteger(value / 0.5));
    });
    const evaluation = runtime.evaluateRecipe(candidate.recipePercent, color, { totalGramsPerLiter: 106 });
    assert.equal(candidate.metrics.dE, evaluation.dE);
    assert.equal(candidate.metrics.hidingAlpha, evaluation.double.alpha);
    assert.equal(candidate.metrics.modelSpread, evaluation.modelSpread);
    assert.equal(candidate.metrics.substrateShift, evaluation.substrateShift);
    assert.equal(candidate.metrics.referenceTrust, evaluation.referenceTrust);
    assert.equal(candidate.metrics.grade, evaluation.grade);
  });
  assert.deepEqual(runtime.generateCandidates(color), candidates);
});

test('recommended candidate prioritizes feasible hiding over a lower black-substrate dE', { timeout: 120000 }, () => {
  const runtime = createRuntime();
  const candidates = runtime.generateCandidates(target('5005'));
  assert.ok(candidates[0].metrics.hidingAlpha >= 0.96);
  assert.ok(candidates[0].metrics.substrateShift <= 3);
  assert.ok(candidates.some(candidate => candidate.metrics.dE < candidates[0].metrics.dE));
});

test('shared runtime preserves all required representative browser candidate outputs', { timeout: 300000 }, () => {
  const runtime = createRuntime();
  Object.entries(REPRESENTATIVE_HASHES).forEach(([code, expected]) => {
    assert.equal(representativeHash(runtime, code).hash, expected, `RAL ${code}`);
  });
});
