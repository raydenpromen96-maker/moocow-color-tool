const assert = require('node:assert/strict');
const test = require('node:test');

const RecipeSearch = require('../src/recipe-search');

const POLICY = Object.freeze({
  totalGpl: 106,
  gridGpl: 0.5,
  minActiveGpl: 1,
  maxActive: 4,
  candidateCount: 1
});

function compareRecipes(left, right) {
  return JSON.stringify(left).localeCompare(JSON.stringify(right));
}

function exactOneTwoGrid(catalog, evaluate) {
  const codes = Object.keys(catalog).sort();
  let best = null;
  const consider = recipe => {
    const score = evaluate(recipe).score;
    if (!best || score < best.score || (score === best.score && compareRecipes(recipe, best.recipe) < 0)) {
      best = { recipe, score };
    }
  };

  codes.forEach(code => consider({ [code]: POLICY.totalGpl }));
  for (let leftIndex = 0; leftIndex < codes.length - 1; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < codes.length; rightIndex += 1) {
      for (let leftCells = 2; leftCells <= 210; leftCells += 1) {
        consider({
          [codes[leftIndex]]: leftCells * POLICY.gridGpl,
          [codes[rightIndex]]: (212 - leftCells) * POLICY.gridGpl
        });
      }
    }
  }
  return best;
}

function assertLegal(recipe) {
  assert.equal(Object.values(recipe).reduce((sum, dose) => sum + dose, 0), POLICY.totalGpl);
  Object.values(recipe).forEach(dose => {
    assert.ok(dose >= POLICY.minActiveGpl);
    assert.equal(Number.isInteger(dose / POLICY.gridGpl), true);
  });
}

test('recovers the exact legal one-colour synthetic grid optimum', () => {
  const catalog = Object.freeze({ A: {} });
  const evaluate = recipe => ({ score: Math.abs((recipe.A || 0) - 106) });
  const global = exactOneTwoGrid(catalog, evaluate);
  const [recovered] = RecipeSearch.searchCandidates({ catalog, evaluate, policy: POLICY, maxSupports: 1 });

  assertLegal(recovered.recipe);
  assert.equal(recovered.score, global.score);
  assert.deepEqual(recovered.recipe, global.recipe);
});

test('recovers the exact two-colour optimum across a one-step score plateau deterministically', () => {
  const evaluate = recipe => {
    const distance = Math.abs((recipe.A || 0) - 53.5);
    return { score: distance === 0 ? 0 : distance <= 1 ? 1 : 2 + distance };
  };
  const search = (catalog, seeds) => RecipeSearch.searchCandidates({
    catalog,
    seeds,
    evaluate,
    policy: POLICY,
    maxSupports: 1
  });
  const initialSeed = Object.freeze({ A: 52.5, B: 53.5 });
  const global = exactOneTwoGrid({ A: {}, B: {} }, evaluate);
  const expected = search({ A: {}, B: {} }, [initialSeed]);

  assert.equal(evaluate(initialSeed).score, 1);
  assert.equal(evaluate({ A: 53, B: 53 }).score, 1);
  assert.equal(expected[0].score, global.score);
  assert.equal(expected[0].score, 0);
  assert.deepEqual(
    search({ B: {}, A: {} }, [Object.freeze({ B: 53.5, A: 52.5 })]),
    expected
  );
});
