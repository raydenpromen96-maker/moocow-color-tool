const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const RecipeSearch = require('../src/recipe-search');

const catalog = Object.freeze({ D: {}, B: {}, A: {}, C: {} });
const seeds = Object.freeze([
  Object.freeze({ A: 73.3, B: 32.7 }),
  Object.freeze({ A: 44.1, C: 61.9 }),
  Object.freeze({ B: 18.2, D: 87.8 }),
  Object.freeze({ C: 51.4, D: 54.6 })
]);

function evaluate(recipe) {
  const values = Object.fromEntries(['A', 'B', 'C', 'D'].map(code => [code, recipe[code] || 0]));
  return {
    score: Math.abs(values.A - 60) + Math.abs(values.B - 20) * 1.1
      + Math.abs(values.C - 15) * 1.2 + Math.abs(values.D - 11) * 1.3,
    totals: values,
    active: Object.keys(recipe).length
  };
}

function assertCanonical(recipe) {
  const doses = Object.values(recipe);

  assert.equal(doses.reduce((sum, dose) => sum + dose, 0), 106);
  assert.ok(doses.length >= 1 && doses.length <= 4);
  doses.forEach(dose => {
    assert.ok(dose >= 1);
    assert.equal(Number.isInteger(dose / 0.5), true);
  });
  assert.deepEqual(Object.keys(recipe), Object.keys(recipe).slice().sort());
}

function search(input = {}) {
  return RecipeSearch.searchCandidates({ catalog, seeds, evaluate, ...input });
}

test('exports the recipe search API in CommonJS and browser UMD contexts', () => {
  assert.deepEqual(Object.keys(RecipeSearch).sort(), [
    'canonicalizeDoseRecipe',
    'recipeSupportKey',
    'searchCandidates',
    'selectDiverseCandidates',
    'supportDistance'
  ]);

  const browserContext = { window: {} };
  const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'recipe-search.js'), 'utf8');
  vm.runInNewContext(source, browserContext);

  assert.deepEqual(Object.keys(browserContext.window.MooCowRecipeSearch).sort(), Object.keys(RecipeSearch).sort());
});

test('canonicalizes doses to the default provisional constraints', () => {
  const recipe = RecipeSearch.canonicalizeDoseRecipe({ Z: 0.1, A: 75, Q: 30, B: 0.2, C: 0.3 });

  assertCanonical(recipe);
  assert.deepEqual(Object.keys(recipe), ['A', 'B', 'C', 'Q']);
});

test('search candidates are canonical, model-only, and use unique supports', () => {
  const candidates = search();

  assert.equal(candidates.length, 3);
  assert.equal(new Set(candidates.map(candidate => candidate.supportKey)).size, 3);
  candidates.forEach(candidate => {
    assertCanonical(candidate.recipe);
    assert.equal(candidate.supportKey, RecipeSearch.recipeSupportKey(candidate.recipe));
    assert.equal(candidate.modelOnly, true);
  });
});

test('search is deterministic across 100 calls', () => {
  const expected = JSON.stringify(search());

  for (let index = 0; index < 100; index += 1) {
    assert.equal(JSON.stringify(search()), expected);
  }
});

test('catalog and seed input order do not affect candidates', () => {
  const reversedCatalog = Object.freeze({ C: {}, A: {}, D: {}, B: {} });
  const reversedSeeds = Object.freeze([
    Object.freeze({ D: 54.6, C: 51.4 }),
    Object.freeze({ D: 87.8, B: 18.2 }),
    Object.freeze({ C: 61.9, A: 44.1 }),
    Object.freeze({ B: 32.7, A: 73.3 })
  ]);

  assert.deepEqual(
    RecipeSearch.searchCandidates({ catalog, seeds, evaluate }),
    RecipeSearch.searchCandidates({ catalog: reversedCatalog, seeds: reversedSeeds, evaluate })
  );
});

test('chooses the best same-support seed regardless of seed order', () => {
  const betterSeed = Object.freeze({ A: 53, B: 53 });
  const sameSupportSeeds = Object.freeze([
    Object.freeze({ A: 20, B: 86 }),
    betterSeed
  ]);
  const searchSameSupportSeeds = inputSeeds => RecipeSearch.searchCandidates({
    catalog: { A: {}, B: {} },
    seeds: inputSeeds,
    evaluate: recipe => ({ score: Math.abs(recipe.A - betterSeed.A) }),
    policy: { candidateCount: 1 },
    maxSupports: 1,
    maxRefinementSteps: -1
  });

  const expected = [{
    recipe: betterSeed,
    supportKey: 'A|B',
    metrics: { score: 0 },
    score: 0,
    modelOnly: true
  }];

  assert.deepEqual(searchSameSupportSeeds(sameSupportSeeds), expected);
  assert.deepEqual(searchSameSupportSeeds(sameSupportSeeds.slice().reverse()), expected);
});

test('search does not mutate supplied catalog or seeds', () => {
  const beforeCatalog = JSON.stringify(catalog);
  const beforeSeeds = JSON.stringify(seeds);

  search();

  assert.equal(JSON.stringify(catalog), beforeCatalog);
  assert.equal(JSON.stringify(seeds), beforeSeeds);
});

test('stored metrics are freshly evaluated from final canonical recipes', () => {
  search().forEach(candidate => {
    assert.deepEqual(candidate.metrics, evaluate(candidate.recipe));
  });
});

test('support distance depends only on active pigment support', () => {
  assert.ok(Math.abs(RecipeSearch.supportDistance({ A: 50, B: 56 }, { B: 100, C: 6 }) - 2 / 3) < 1e-12);
  assert.equal(RecipeSearch.supportDistance({ A: 1 }, { A: 106 }), 0);
});

test('selects the best-scoring candidate that meets the minimum support distance', () => {
  const candidates = [
    { recipe: { A: 106 }, supportKey: 'A', score: 2.63 },
    { recipe: { A: 53, B: 53 }, supportKey: 'A|B', score: 6.46 },
    { recipe: { C: 53, D: 53 }, supportKey: 'C|D', score: 16.85 }
  ];

  assert.deepEqual(
    RecipeSearch.selectDiverseCandidates(candidates, { candidateCount: 2 }).map(candidate => candidate.supportKey),
    ['A', 'A|B']
  );
  assert.deepEqual(
    RecipeSearch.selectDiverseCandidates(candidates, { candidateCount: 2, minSupportDistance: 0.75 })
      .map(candidate => candidate.supportKey),
    ['A', 'C|D']
  );
});

test('evaluates sorted unique seed supports before generic supports regardless of input order', () => {
  const seedSupports = Object.freeze([
    Object.freeze({ D: 50, C: 56 }),
    Object.freeze({ D: 50, B: 56 })
  ]);
  const reversedCatalog = Object.freeze({ D: {}, B: {}, A: {}, C: {} });
  const traceSupports = (catalogInput, seedInput) => {
    const evaluatedSupports = [];

    RecipeSearch.searchCandidates({
      catalog: catalogInput,
      seeds: seedInput,
      evaluate: recipe => {
        evaluatedSupports.push(RecipeSearch.recipeSupportKey(recipe));
        return { score: 0 };
      },
      policy: { candidateCount: 3 },
      maxSupports: 3,
      maxRefinementSteps: -1
    });

    return Array.from(new Set(evaluatedSupports));
  };

  const expected = ['B|D', 'C|D', 'A'];
  assert.deepEqual(traceSupports(catalog, seedSupports), expected);
  assert.deepEqual(traceSupports(reversedCatalog, seedSupports.slice().reverse()), expected);
});
