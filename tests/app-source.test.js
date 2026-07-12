const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

test('page loads the shared catalog and production runtime after their dependencies', () => {
  const qtcCatalogue = html.indexOf('<script src="./src/qtc-ral-classic.js"></script>');
  const paintCatalog = html.indexOf('<script src="./src/paint-catalog.js"></script>');
  const colorCore = html.indexOf('<script src="./src/color-core.js"></script>');
  const recipeSearch = html.indexOf('<script src="./src/recipe-search.js"></script>');
  const familySpectra = html.indexOf('<script src="./src/family-spectra.js"></script>');
  const productionRuntime = html.indexOf('<script src="./src/production-runtime.js"></script>');

  assert.ok(qtcCatalogue >= 0);
  assert.ok(paintCatalog > qtcCatalogue);
  assert.ok(colorCore > paintCatalog);
  assert.ok(recipeSearch > colorCore);
  assert.ok(familySpectra > recipeSearch);
  assert.ok(productionRuntime > familySpectra);
});

test('page delegates target construction and model search to the shared runtime', () => {
  assert.match(html, /window\.MooCowPaintCatalog\.buildTargets\(targetCatalogue\)/);
  assert.match(html, /window\.MooCowProductionRuntime\.create\(\{/);
  assert.match(html, /paintCatalog: window\.MooCowPaintCatalog\.PAINT_DATA/);
  assert.match(html, /const PAINT_DATA = runtime\.catalog;/);
  assert.match(html, /const \{ activateRalPreset, resolveActiveRecipe, rgbToHex \} = window\.MooCowColorCore;/);
  assert.match(html, /currentState\.generatedCandidates = generateCandidates\(currentState\.ral\);/);
  assert.doesNotMatch(html, /const PAINT_DATA = \{/);
  assert.doesNotMatch(html, /const RAL_BASE_RECIPES/);
  assert.doesNotMatch(html, /function evaluateRecipe\(/);
  assert.doesNotMatch(html, /function buildSeedRecipes\(/);
  assert.doesNotMatch(html, /function refineObjectiveSeedProposal\(/);
  assert.doesNotMatch(html, /function refineSeedProposal\(/);
  assert.doesNotMatch(html, /function recipePercentToGpl\(/);
  assert.doesNotMatch(html, /function recipeGplToPercent\(/);
});

test('UI state and target source display wiring remain intact', () => {
  assert.match(html, /generatedCandidates: \[\], selectedCandidateId: null/);
  assert.match(html, /activateRalPreset\(currentState, d\)/);
  assert.match(html, /const recipe = resolveActiveRecipe\(currentState\)/);
  assert.match(html, /elements\.currentHex\.textContent = d\.hex;/);
  assert.match(html, /target_source_notice: "Target colours use QTC computer-simulated screen references/);
  assert.match(html, /id="candidateSelector"/);
  assert.match(html, /id="candidateList"/);
  assert.match(html, /data-candidate-id="\$\{candidate\.id\}"/);
});
