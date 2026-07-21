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

// v5 重录：引擎从"编造 REFERENCE_SPECTRA + 3 通道混合"切换为"实测/锚定代理光谱 +
// 有效颜料质量加权单常数 K-M"，候选配方与指标全部改变，代表色哈希按新引擎重录。
const REPRESENTATIVE_HASHES = Object.freeze({
  "1021": "4be03d2182c4d059b24871e74ff7a566ee30b4cb3b751124f50e08eed5fbb137",
  "1035": "0e9c8b281f107c6a14217bd54cab213004d288d98609f3429f2d2c9280384b43",
  "2004": "497c90c43af1fd787e8d2eee53a605bf1afa51d82dbafc2dfb6555c4ddbb08bf",
  "3020": "f36ccb4e894126fbc0d44b434ad4a1d3776d577a0d6aca4f0474d744866d88f9",
  "4005": "6ff2896c5090dc0c962c74932e663769cae70eafef59f35d822e1c483b3b979a",
  "5005": "1d57ee0edb96d798579b3b3bee314ee1d3f64b6fca66aa82281859af0cbbac50",
  "5015": "8703ccc536eeca03bc9ee7bd59eb3c6c68c86629dde3d29f8c9b344f5b1a51ea",
  "6005": "9878611db3ac8776afc6259cb6876422fd4addc23934a90da870517b31981c08",
  "6037": "9055b93a768ee519bd9bc3edffe8bf3459357fd92af45dd13bc0ddaa389909d2",
  "7035": "b5e61bcfd4be805be961d254b62afd67b83baa9867f520b3b952c52bd2ab4167",
  "8004": "2e9895f0a998f27ba16242f1aa8046491992aee174ab2f36b415305be3669f46",
  "9005": "f90410b829a9fe6fffa05a68f3ab6a9ed19afde5a55298902469de0f394625e2",
  "9010": "19992bc29bde374a43feaa963452abf8aa84254a8727ba25b1ca9914e550c189"
});
// v5: 引擎改用实测/锚定代理光谱 + 有效颜料质量加权的单常数 K-M，
// 但仍是未校准代理模型（等待 45 卡实测），provenance 文案相应更新。
const LEGACY_SCREENING_PROVENANCE = Object.freeze({
  evidence_class: 'proxy_measured_spectra_km_model',
  calibration_status: 'uncalibrated_proxy_spectra_pending_drawdown_measurement',
  physical_accuracy_verified: false,
  measured_current_batch: false,
  runtime_activation_permitted: false
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
    candidates: candidates.map(candidate => {
      const { provenance, ...metrics } = candidate.metrics;
      return {
        recipe: candidate.recipe,
        metrics,
        score: candidate.score,
        supportKey: candidate.supportKey
      };
    })
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
  assert.equal(Object.keys(runtime.catalog).length, 15);
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

test('legacy screening provenance is exact, frozen, and present on evaluations and candidate metrics', () => {
  const runtime = createRuntime();
  const color = target('1021');
  const evaluation = runtime.evaluateRecipe({ Y83S: 100 }, color);
  const candidates = runtime.generateCandidates(color);

  assert.deepEqual(runtime.provenance, LEGACY_SCREENING_PROVENANCE);
  assert.equal(Object.isFrozen(runtime.provenance), true);
  assert.throws(() => Object.defineProperty(runtime.provenance, 'evidence_class', { value: 'measured' }), TypeError);
  assert.deepEqual(evaluation.provenance, LEGACY_SCREENING_PROVENANCE);
  assert.equal(evaluation.provenance, runtime.provenance);
  candidates.forEach(candidate => {
    assert.deepEqual(candidate.metrics.provenance, LEGACY_SCREENING_PROVENANCE);
    assert.equal(Object.isFrozen(candidate.metrics.provenance), true);
    assert.equal(candidate.metrics.provenance, runtime.provenance);
  });
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

test('recommended candidate satisfies strict feasibility and recommendation ordering', { timeout: 120000 }, () => {
  const runtime = createRuntime();
  const candidates = runtime.generateCandidates(target('5005'));
  assert.ok(candidates[0].metrics.hidingAlpha >= 0.96);
  assert.ok(candidates[0].metrics.substrateShift <= 3);
  candidates.slice(1).forEach(candidate => {
    assert.ok(runtime.compareRecommendedCandidates(candidates[0], candidate) <= 0);
  });
});

test('shared runtime preserves all required representative browser candidate outputs', { timeout: 300000 }, () => {
  const runtime = createRuntime();
  Object.entries(REPRESENTATIVE_HASHES).forEach(([code, expected]) => {
    assert.equal(representativeHash(runtime, code).hash, expected, `RAL ${code}`);
  });
});
