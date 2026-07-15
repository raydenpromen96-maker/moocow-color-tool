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
  '1021': '4dea112a02dbd99284aaeb82fc5786e984f5bb82560cfeab162832192dfff487',
  '1035': '8fc3ef2a40f076ec6a1f1500eb2ccf51e219cd47c5fda14798794c1f83367ff9',
  '2004': '15c2064b3207d35ad5c6f9258288b4e908eb080738b20aaa53e63b8a9b4f351b',
  '3020': '24bb35655bda47c2423d56b3eb8c5f0cd74c2a049758c5620d0e9091c9785f21',
  '4005': 'ebe268b66652a7454e946758d493ab38ba305df51b37686c38f22edbfbd9bd8d',
  '5005': 'cac80a72c917daafa0f5136067dfdffe02fa7ab954354583f3d8657f178b33a7',
  '5015': '6d662f6ee0e48ceb88b37e6cc168a8e6b87e8dde44223913eb52ad7c3f1a4b4e',
  '6005': '8009500cb836283fd13ce6aa66f9bf1e1836769ffae0c956a2da17597d69c88d',
  '6037': 'e99807ea1dbda368fc0ce48c46be10fe5ffb4033c691ec1f7a681bb3e1c2697b',
  '7035': '81d5a3e3d4929761d238d8a543941d85a63eba17f1bffa5b32e6d7c2109cf818',
  '8004': '548539b60de04ef65407ace9eee4751f58711c16e69e46e9ecd1b95a8300b0ea',
  '9005': '822742d106e9cd1b8f70932db248c08665649a9fcedf767a36f7a503e021d5a4',
  '9010': 'a5045921f6321eefba7711fb608e590e66d37ef08b55e693c3a0cbb18c11fa5d'
});
const LEGACY_SCREENING_PROVENANCE = Object.freeze({
  evidence_class: 'catalog_screen_approximation',
  calibration_status: 'uncalibrated_screening_only',
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
