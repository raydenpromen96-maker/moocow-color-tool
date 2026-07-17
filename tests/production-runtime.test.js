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
  "1021": "7bdcb69ee7b04d536e98524e124cd11982a40b7e6e23e6f056db7405a4fb6966",
  "1035": "465c20bfbd7bbce42e5054847c5a7f51ebdc8afc5111b313694aef30ea0cc708",
  "2004": "5f226e65e79b7192fde4f03c6d483067d06e01916f9691f26d87e578a9a4286b",
  "3020": "efa6c5ceb154e9167aa9081f5a41fd6776c2ee50fd1fb95092f8b675355598c3",
  "4005": "4935f1a12d2c61a4b4877c82b22e577075fa2c297691dd63ca2b1e4936b1d2e4",
  "5005": "0ed9729f2b45895a6c7b042dd8eadd3f171b784d566e7f1caea836da88beb2df",
  "5015": "bca119d835c1d73b7a834c47ba451acfe2fb6bdae8d452b5329237056bb9dd90",
  "6005": "a5d52db893866668c5638d8c7c6879b135832a947ef73e0bdc4b02e4dc99f880",
  "6037": "8514432e6c248994532a50a4d7dfa6fcfd49e65e1c204529c58c23f1c6454ed6",
  "7035": "b95baa0105d00625c563a59425ef123450ed10724c63331464088e0d7e82b065",
  "8004": "7029a48955cacdcf3d44b77949116f24e5e9e83b10028640b3e870ec35e4aee3",
  "9005": "7511c8728ee4102589d2018e725f8387c83924613ecb778d752c7863d129c4d6",
  "9010": "fb6d07cca7dc899a3f60700d413ad19643ff7d3e887a78adbb7fbad89cca363d"
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
