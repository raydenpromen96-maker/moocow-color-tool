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

test('073 remains on the K/S path when a supported pigment is added', () => {
  const runtime = createRuntime();
  const withoutBlueReference = structuredClone(PaintCatalog.PAINT_DATA);
  withoutBlueReference.B153S.ci = null;
  const controlRuntime = createRuntime(withoutBlueReference);
  const pure = runtime.evaluateRecipe({ '073': 100 }, '#EF7622');
  const mixed = runtime.evaluateRecipe({ '073': 99, B153S: 1 }, '#EF7622');
  const controlMixed = controlRuntime.evaluateRecipe({ '073': 99, B153S: 1 }, '#EF7622');

  assert.equal(pure.referenceRgb, null);
  assert.equal(mixed.referenceRgb, null);
  assert.equal(mixed.referenceTrust, 0);
  assert.deepEqual(mixed.topRgb, mixed.kmRgb);
  assert.deepEqual(mixed.topRgb, controlMixed.topRgb);
  assert.ok(mixed.topRgb[0] < pure.topRgb[0]);
});

test('unsupported active coverage fails closed instead of normalizing a partial spectrum', () => {
  const runtime = createRuntime();
  const evaluation = runtime.evaluateRecipe({ '073': 99, B153S: 1 }, '#EF7622');

  assert.equal(evaluation.referenceRgb, null);
  assert.equal(evaluation.referenceTrust, 0);
  assert.equal(evaluation.familySpectralCoverage.exactFraction, 0.01);
  assert.equal(evaluation.familySpectralCoverage.missingFraction, 0.99);
  assert.deepEqual(evaluation.familySpectralCoverage.missingCi, ['PO73']);
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

test('fail-closed floating-point evaluation remains deterministic', () => {
  const runtime = createRuntime();
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
