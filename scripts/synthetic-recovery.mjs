import { performance } from 'node:perf_hooks';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const RecipeSearch = require('../src/recipe-search');

const CODES = Object.freeze(['A', 'B', 'C', 'D']);
const CATALOG = Object.freeze(Object.fromEntries(CODES.map(code => [code, {}])));
const POLICY = Object.freeze({
  totalGpl: 106,
  gridGpl: 0.5,
  minActiveGpl: 1,
  maxActive: 4,
  candidateCount: 3
});

function recipeKey(recipe) {
  return Object.entries(recipe).map(([code, dose]) => `${code}:${dose}`).join('|');
}

function compareResults(left, right) {
  return left.score - right.score || recipeKey(left.recipe).localeCompare(recipeKey(right.recipe));
}

function exactOneTwoGrid(evaluate) {
  let best = null;
  const consider = recipe => {
    const result = { recipe, score: evaluate(recipe).score };
    if (!best || compareResults(result, best) < 0) best = result;
  };

  CODES.forEach(code => consider({ [code]: POLICY.totalGpl }));
  for (let leftIndex = 0; leftIndex < CODES.length - 1; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < CODES.length; rightIndex += 1) {
      for (let leftCells = 2; leftCells <= 210; leftCells += 1) {
        consider({
          [CODES[leftIndex]]: leftCells * POLICY.gridGpl,
          [CODES[rightIndex]]: (212 - leftCells) * POLICY.gridGpl
        });
      }
    }
  }
  return best;
}

function renderSynthetic(recipe) {
  const dose = Object.fromEntries(CODES.map(code => [code, recipe[code] || 0]));
  return [
    Math.round(21 + dose.A * 1.19 + dose.B * 0.37 + dose.C * 0.12 + dose.D * 0.71),
    Math.round(242 - dose.A * 0.46 + dose.B * 0.82 - dose.C * 0.33 + dose.D * 0.17),
    Math.round(32 + dose.A * 0.18 + dose.B * 0.31 + dose.C * 0.98 - dose.D * 0.41)
  ];
}

function syntheticEvaluate(target) {
  const renderedTarget = renderSynthetic(target);
  return recipe => {
    const rendered = renderSynthetic(recipe);
    return {
      score: Math.hypot(...rendered.map((value, index) => value - renderedTarget[index]))
    };
  };
}

function percentile(values, fraction) {
  const sorted = values.slice().sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * fraction) - 1)];
}

function runCase(name, target) {
  const evaluate = syntheticEvaluate(target);
  const oracleStart = performance.now();
  const global = exactOneTwoGrid(evaluate);
  const oracleMs = performance.now() - oracleStart;
  const searchStart = performance.now();
  const candidates = RecipeSearch.searchCandidates({
    catalog: CATALOG,
    evaluate,
    policy: POLICY,
    maxSupports: 10
  });
  const searchMs = performance.now() - searchStart;
  const recovered = candidates.slice().sort(compareResults)[0];

  return {
    name,
    truthRecipe: target,
    globalRecipe: global.recipe,
    globalScore: global.score,
    recoveredRecipe: recovered.recipe,
    recoveredScore: recovered.score,
    regret: recovered.score - global.score,
    oracleMs: Number(oracleMs.toFixed(3)),
    searchMs: Number(searchMs.toFixed(3))
  };
}

const supports = [
  ['A'], ['B'], ['C'], ['D'],
  ['A', 'B'], ['A', 'C'], ['A', 'D'], ['B', 'C'], ['B', 'D'], ['C', 'D']
];
const fixtureCases = [
  runCase('one-colour-A', { A: 106 }),
  runCase('two-colour-B-C', { B: 52.5, C: 53.5 })
];
const broaderCases = Array.from({ length: 30 }, (_, index) => {
  const support = supports[index % supports.length];
  if (support.length === 1) return runCase(`sample-${index + 1}`, { [support[0]]: 106 });
  const leftCells = 2 + (index * 37 % 209);
  return runCase(`sample-${index + 1}`, {
    [support[0]]: leftCells * POLICY.gridGpl,
    [support[1]]: (212 - leftCells) * POLICY.gridGpl
  });
});
const report = {
  kind: 'synthetic-search-recovery-benchmark',
  scope: 'deterministic software-search fixture; not a physical paint accuracy result',
  policy: POLICY,
  legalGrid: { oneColourPoints: 1, twoColourPointsPerSupport: 209 },
  fixtures: fixtureCases,
  broaderSample: {
    count: broaderCases.length,
    p95Regret: percentile(broaderCases.map(result => result.regret), 0.95),
    p95SearchMs: percentile(broaderCases.map(result => result.searchMs), 0.95),
    maxRegret: Math.max(...broaderCases.map(result => result.regret))
  }
};

console.log(JSON.stringify(report, null, 2));
if (report.fixtures.some(result => result.regret !== 0) || report.broaderSample.maxRegret !== 0) process.exitCode = 1;
