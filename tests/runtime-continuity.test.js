const assert = require('node:assert/strict');
const test = require('node:test');

const ColorCore = require('../src/color-core.js');
const RecipeSearch = require('../src/recipe-search.js');
const FamilySpectra = require('../src/family-spectra.js');
const PaintCatalog = require('../src/paint-catalog.js');
const ProductionRuntime = require('../src/production-runtime.js');

function createRuntime(paintCatalog = PaintCatalog.PAINT_DATA) {
  return ProductionRuntime.create({ ColorCore, RecipeSearch, FamilySpectra, paintCatalog });
}

test('a pigment without a spectral profile stays on the K/S path even beside a supported pigment', () => {
  // v5: 073 现在自带 CHSOS 实测代理光谱；改用"去掉 CI 的克隆目录"验证 fail-closed 语义不变。
  const runtime = createRuntime();
  const withoutOrangeProfile = structuredClone(PaintCatalog.PAINT_DATA);
  withoutOrangeProfile['073'].ci = null;
  const controlRuntime = createRuntime(withoutOrangeProfile);
  const pure = controlRuntime.evaluateRecipe({ '073': 100 }, '#EF7622');
  const mixed = controlRuntime.evaluateRecipe({ '073': 99, B153S: 1 }, '#EF7622');

  assert.equal(pure.referenceRgb, null);
  assert.equal(mixed.referenceRgb, null);
  assert.equal(mixed.referenceTrust, 0);
  assert.deepEqual(mixed.topRgb, mixed.kmRgb);
  // 完整目录下同一配方走 11 点光谱路径（两支均有实测/代理光谱）
  const spectralMixed = runtime.evaluateRecipe({ '073': 99, B153S: 1 }, '#EF7622');
  assert.notEqual(spectralMixed.referenceRgb, null);
  assert.ok(spectralMixed.referenceTrust > 0);
  assert.notDeepEqual(spectralMixed.topRgb, mixed.topRgb);
});

test('unsupported active coverage fails closed instead of normalizing a partial spectrum', () => {
  const withoutOrangeProfile = structuredClone(PaintCatalog.PAINT_DATA);
  withoutOrangeProfile['073'].ci = null;
  const runtime = createRuntime(withoutOrangeProfile);
  const evaluation = runtime.evaluateRecipe({ '073': 99, B153S: 1 }, '#EF7622');

  assert.equal(evaluation.referenceRgb, null);
  assert.equal(evaluation.referenceTrust, 0);
  assert.equal(evaluation.familySpectralCoverage.exactFraction, 0.01);
  assert.equal(evaluation.familySpectralCoverage.missingFraction, 0.99);
  assert.deepEqual(evaluation.familySpectralCoverage.missingCi, ['CI-unverified']);
  assert.equal(evaluation.familySpectralCoverage.predictiveEligible, false);
});

test('measured-proxy coverage is reported as proxy, never promoted to exact', () => {
  const runtime = createRuntime();
  const evaluation = runtime.evaluateRecipe({ '073': 99, B153S: 1 }, '#EF7622');

  assert.notEqual(evaluation.referenceRgb, null);
  assert.equal(evaluation.familySpectralCoverage.exactFraction, 0.01);
  assert.equal(evaluation.familySpectralCoverage.proxyFraction, 0.99);
  assert.deepEqual(evaluation.familySpectralCoverage.proxyCi, ['PO73']);
  assert.equal(evaluation.familySpectralCoverage.missingFraction, 0);
  assert.equal(evaluation.familySpectralCoverage.predictiveEligible, false);
});

test('empty reference curves fail closed instead of falling back to a partial mix', () => {
  const paintCatalog = structuredClone(PaintCatalog.PAINT_DATA);
  paintCatalog.B153S.referenceSpectrum = [];
  const runtime = createRuntime(paintCatalog);
  const evaluation = runtime.evaluateRecipe({ Y83S: 99, B153S: 1 }, '#FEDD00');

  assert.equal(evaluation.referenceRgb, null);
  assert.equal(evaluation.referenceTrust, 0);
  assert.deepEqual(evaluation.topRgb, evaluation.kmRgb);
});

test('runtime evaluation keeps floating-point mix and substrate values until display conversion', () => {
  const runtime = createRuntime();
  const target = { hex: '#6C7890', targetLab: ColorCore.rgbToLab(108, 120, 144) };
  const evaluation = runtime.evaluateRecipe({ Y83S: 73.3, B153S: 26.7 }, target, { totalGramsPerLiter: 106 });
  const roundedDouble = evaluation.double.rgb.map(Math.round);
  const roundedDoubleDE = ColorCore.deltaE2000(evaluation.targetLab, ColorCore.rgbToLab(...roundedDouble));

  assert.ok(evaluation.topRgb.some(value => !Number.isInteger(value)));
  assert.ok(evaluation.double.rgb.some(value => !Number.isInteger(value)));
  assert.notEqual(evaluation.dE, roundedDoubleDE);
  assert.equal(ColorCore.rgbToHex(evaluation.double.rgb), ColorCore.rgbToHex(roundedDouble));
});

test('fail-closed fallback evaluation remains deterministic', () => {
  const withoutOrangeProfile = structuredClone(PaintCatalog.PAINT_DATA);
  withoutOrangeProfile['073'].ci = null;
  const runtime = createRuntime(withoutOrangeProfile);
  const recipe = { '073': 99, B153S: 1 };
  const expected = JSON.stringify(runtime.evaluateRecipe(recipe, '#EF7622'));

  for (let index = 0; index < 100; index += 1) {
    assert.equal(JSON.stringify(runtime.evaluateRecipe(recipe, '#EF7622')), expected);
  }
});

test('search score preserves the runtime strict-feasibility-first contract', () => {
  const runtime = createRuntime();
  const feasible = {
    dE: 5,
    double: { alpha: 0.96 },
    substrateShift: 3,
    modelSpread: 20,
    referenceTrust: 0
  };
  const lowerDeltaEButInfeasible = {
    dE: 0,
    double: { alpha: 0.9599 },
    substrateShift: 3,
    modelSpread: 0,
    referenceTrust: 1
  };

  assert.ok(runtime.candidateMetricScore(feasible, 4)
    < runtime.candidateMetricScore(lowerDeltaEButInfeasible, 1));
});
