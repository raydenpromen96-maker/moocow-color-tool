const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

test('page loads local ColorCore and recipe search without external spectral runtime data', () => {
  const colorCore = html.indexOf('<script src="./src/color-core.js"></script>');
  const recipeSearch = html.indexOf('<script src="./src/recipe-search.js"></script>');

  assert.ok(colorCore >= 0);
  assert.ok(recipeSearch > colorCore);
  assert.doesNotMatch(html, /family-spectra\.js|MooCowFamilySpectra|familySpectralProfile|familySpectralCoverage/);
  assert.doesNotMatch(html, /spectral\.js|window\.spectral|simulateMixSpectral/);
});

test('standard spectral integration uses D65 and CIE 1931 2-degree samples', () => {
  assert.match(html, /const D65_30NM = \[82\.7549, 86\.6823/);
  assert.match(html, /const CIE_1931_2DEG_30NM = \{/);
  assert.match(html, /0\.9163, 1\.0026/);
  assert.doesNotMatch(html, /CIE_1931_10DEG_APPROX|1\.0622/);
});

test('incompatible epoxy evidence is absent and the waterborne data gap is explicit', () => {
  assert.doesNotMatch(html, /MultipigmentPhantoms|pigment-in-epoxy|mu_a|mu_s'|MIT实测家族/);
  assert.match(html, /当前未内置获得明确商业再分发授权的水性丙烯酸实测光谱曲线/);
  assert.match(html, /No measured waterborne-acrylic spectral curves with explicit commercial redistribution permission are bundled/);
  const scoreFunction = html.match(/function candidateMetricScore\(evaluation, activeCount\) \{[\s\S]*?\n\s*\}/)?.[0] || '';
  assert.doesNotMatch(scoreFunction, /familySpectral|MooCowFamilySpectra|waterborne/);
  assert.match(html, /"073": \{[^\n]*ci: null,[^\n]*C\.I\. identity unverified/);
});

test('page derives pigment model inputs through ColorCore', () => {
  assert.match(html, /const modelColor = deriveModelColor\(p\)/);
  assert.match(html, /p\.physicsRgb = modelColor\.modelRgb/);
  assert.match(html, /p\.modelInput = modelColor\.modelLabSource/);
});

test('candidate state remains independent from static RAL preset recipes', () => {
  assert.match(html, /generatedCandidates: \[\], selectedCandidateId: null/);
  assert.doesNotMatch(html, /currentState\.ral\.baseRecipe\s*=/);
  assert.match(html, /currentState\.generatedRecipe = \{ \.\.\.candidate\.recipePercent \}/);
  assert.match(html, /activateRalPreset\(currentState, d\)/);
  assert.match(html, /const recipe = resolveActiveRecipe\(currentState\)/);
});

test('generation uses one bounded g/L evaluator for search and constrained proposals', () => {
  assert.match(html, /const CANDIDATE_SEARCH_POLICY = Object\.freeze\(\{ totalGpl: 106, gridGpl: 0\.5, minActiveGpl: 1\.0, maxActive: 4, candidateCount: 3 \}\)/);
  assert.match(html, /const CANDIDATE_SEARCH_BOUNDS = Object\.freeze\(\{ maxSupports: 30, maxRefinementSteps: 60 \}\)/);
  assert.match(html, /window\.MooCowRecipeSearch\.searchCandidates\(\{/);
  assert.match(html, /const seedRecipes = buildSeedRecipes\(targetLab, currentState\.ral\.baseRecipe\);/);
  assert.match(html, /const evaluateCandidateGpl = recipeGpl => \{[\s\S]*?const recipePercent = recipeGplToPercent\(recipeGpl\);[\s\S]*?score: candidateMetricScore\(evaluation, Object\.keys\(recipeGpl\)\.length\),[\s\S]*?modelSpread: evaluation\.modelSpread,[\s\S]*?substrateShift: evaluation\.substrateShift,[\s\S]*?referenceTrust: evaluation\.referenceTrust,[\s\S]*?grade: evaluation\.grade[\s\S]*?\};/);
  assert.match(html, /searchCandidates\(\{[\s\S]*?evaluate: evaluateCandidateGpl/);
  assert.doesNotMatch(html, /function refineRecipe\(/);
  assert.doesNotMatch(html, /function cleanRecipeForDisplay\(/);
});

test('Phase-1 objective and bounded dE-first proposals are canonicalized before final merge and UI', () => {
  assert.match(html, /function twoCoatProposalScore\(recipe, targetHex\) \{[\s\S]*?candidateMetricScore\(evaluation, Object\.values\(recipe\)\.filter\(v => v > 0\.25\)\.length\);/);
  assert.match(html, /function refineObjectiveSeedProposal\(seedRecipe, targetHex\) \{[\s\S]*?let bestScore = objectiveForRecipe\(best, targetHex\);[\s\S]*?let step = 14;[\s\S]*?while \(step >= 0\.08\) \{[\s\S]*?const score = objectiveForRecipe\(normalized, targetHex\);[\s\S]*?const canonical = cleanRecipe\(best, 0\.05\);[\s\S]*?Math\.round\(canonical\[code\] \* 100\) \/ 100\);[\s\S]*?const recipe = normalizeRecipe\(canonical\);[\s\S]*?return \{ recipe, score: objectiveForRecipe\(recipe, targetHex\) \};/);
  assert.match(html, /function refineSeedProposal\(seedRecipe, targetHex, scoreRecipe\) \{[\s\S]*?let bestScore = scoreRecipe\(best, targetHex\);[\s\S]*?let step = 14;[\s\S]*?while \(step >= 0\.08 && passes < 48\) \{[\s\S]*?const score = scoreRecipe\(normalized, targetHex\);[\s\S]*?return \{ recipe: cleanRecipe\(best\), score: bestScore \};/);
  assert.match(html, /const seedRecipes = buildSeedRecipes\(targetLab, currentState\.ral\.baseRecipe\);[\s\S]*?const objectiveSeedProposal = seedRecipes\.reduce\([\s\S]*?refineObjectiveSeedProposal\(seedRecipe, currentState\.ral\.hex\)[\s\S]*?const twoCoatSeedProposal = seedRecipes\.reduce\([\s\S]*?refineSeedProposal\(seedRecipe, currentState\.ral\.hex, twoCoatProposalScore\)[\s\S]*?const seeds = seedRecipes\.map\(recipePercentToGpl\);[\s\S]*?const searchResults = window\.MooCowRecipeSearch\.searchCandidates\(\{[\s\S]*?seeds,/);
  assert.match(html, /const constrainedProposalCandidates = \[objectiveSeedProposal, twoCoatSeedProposal\][\s\S]*?canonicalizeDoseRecipe\([\s\S]*?recipePercentToGpl\(proposal\.recipe\),[\s\S]*?CANDIDATE_SEARCH_POLICY[\s\S]*?const metrics = evaluateCandidateGpl\(recipe\);[\s\S]*?supportKey: window\.MooCowRecipeSearch\.recipeSupportKey\(recipe\),[\s\S]*?modelOnly: true/);
  assert.match(html, /const combinedResults = window\.MooCowRecipeSearch\.selectDiverseCandidates\([\s\S]*?\[\.\.\.searchResults, \.\.\.constrainedProposalCandidates\],[\s\S]*?CANDIDATE_SEARCH_POLICY/);
  assert.equal((html.match(/\bobjectiveSeedProposal\b/g) || []).length, 3);
  assert.equal((html.match(/\btwoCoatSeedProposal\b/g) || []).length, 3);
  assert.match(html, /currentState\.generatedCandidates = combinedResults\.map\(/);
  assert.doesNotMatch(html, /currentState\.generatedCandidates = searchResults/);
  assert.doesNotMatch(html, /currentState\.generatedCandidates\s*=\s*(?:objectiveSeedProposal|twoCoatSeedProposal)/);
  assert.doesNotMatch(html, /currentState\.generatedRecipe\s*=\s*\{\s*\.\.\.(?:objectiveSeedProposal|twoCoatSeedProposal)\.recipe/);
});

test('candidate ranking makes two-coat model dE the decisive metric', () => {
  assert.match(html, /function candidateMetricScore\(evaluation, activeCount\) \{[\s\S]*?twoCoatDe[\s\S]*?modelSpread[\s\S]*?substrateShift[\s\S]*?referenceTrustPenalty[\s\S]*?activeCountPenalty[\s\S]*?return twoCoatDe \* 100000000000[\s\S]*?modelSpread \* 100000000[\s\S]*?substrateShift \* 100000[\s\S]*?referenceTrustPenalty \* 100[\s\S]*?activeCountPenalty/);
  assert.doesNotMatch(html, /score: objectiveForRecipe\(recipePercent, currentState\.ral\.hex\)/);
});

test('candidate selector renders three compact delegated model-candidate controls', () => {
  assert.match(html, /id="candidateSelector"/);
  assert.match(html, /id="candidateList"/);
  assert.match(html, /candidate-option/);
  assert.match(html, /slice\(0, CANDIDATE_SEARCH_POLICY\.candidateCount\)/);
  assert.match(html, /data-candidate-id="\$\{candidate\.id\}"/);
  assert.match(html, /elements\.candidateList\.addEventListener\('click'/);
  assert.match(html, /currentState\.generatedCandidates\[0\]\?\.id/);
  assert.match(html, /updatePaintWeights\(currentState\.generatedRecipe\)/);
});

test('candidate state clears only for explicit recipe changes and rerenders for display changes', () => {
  assert.match(html, /function resetAllSliders\(notify = false\) \{[\s\S]*?if \(notify\) \{[\s\S]*?clearGeneratedCandidates\(\)/);
  assert.match(html, /function activateColorSelection\(d\) \{[\s\S]*?activateRalPreset\(currentState, d\);[\s\S]*?clearGeneratedCandidates\(\)/);
  assert.match(html, /elements\.addFormulaBtn\.addEventListener\('click',[\s\S]*?clearGeneratedCandidates\(\)/);
  assert.match(html, /elements\.paintSlidersContainer\.addEventListener\('input',[\s\S]*?clearGeneratedCandidates\(\)/);
  assert.match(html, /elements\.volumeSelect\.addEventListener\('change',[\s\S]*?renderCandidateSelector\(\)/);
  assert.match(html, /function switchLanguage\(l\) \{[\s\S]*?renderCandidateSelector\(\)/);
});

test('candidate wording and exported output retain the uncalibrated model caveat', () => {
  assert.match(html, /candidate_label: "Candidate"/);
  assert.match(html, /candidate_policy: "Temporary uncalibrated model policy: 106g\/L total, 0\.5g\/L grid, >=1\.0g\/L active, max4; not verified for equipment or physical accuracy\."/);
  assert.match(html, /policyDetail: '106g\/L total; 0\.5g\/L grid; >=1\.0g\/L active; max4; unverified equipment policy and physical accuracy\.'/);
  assert.match(html, /Selected model candidate/);
  assert.match(html, /physical drawdown/);
  assert.match(html, /spectralBoundary: 'Spectral-data boundary: no measured waterborne-acrylic curves/);
  assert.match(html, /t \+= `\$\{labels\.policy\}: \$\{labels\.policyDetail\}\\n\$\{labels\.spectralBoundary\}/);
});

test('substrate comparison exposes black and white two-coat metrics', () => {
  assert.match(html, /const whiteDE = deltaE2000\(targetLab, doubleWhiteLab\);/);
  assert.match(html, /doubleWhite, dE, singleDE, whiteDE, topDE, substrateShift/);
  assert.match(html, /grid-cols-2 gap-2 lg:grid-cols-4/);
  assert.match(html, /id="whiteTwoCoatSwatch"/);
  assert.match(html, /id="whiteTwoCoatValue"/);
  assert.match(html, /id="substrateShiftValue"/);
  assert.match(html, /whiteTwoCoatValue: document\.getElementById\('whiteTwoCoatValue'\)/);
  assert.match(html, /substrateShiftValue: document\.getElementById\('substrateShiftValue'\)/);
  assert.doesNotMatch(html, /deltaEValue/);
  assert.match(html, /elements\.whiteTwoCoatValue\.textContent = '--';/);
  assert.match(html, /elements\.substrateShiftValue\.textContent = '--';/);
  assert.match(html, /elements\.whiteTwoCoatValue\.textContent = `dE \$\{evaluation\.whiteDE\.toFixed\(1\)\}`;/);
  assert.match(html, /elements\.substrateShiftValue\.textContent = `dE \$\{evaluation\.substrateShift\.toFixed\(1\)\}`;/);
});

test('substrate comparison localizes labels and exports all four dE values', () => {
  assert.match(html, /white_two_coat_label: "两遍白底"/);
  assert.match(html, /white_two_coat_label: "White substrate, 2 coats"/);
  assert.match(html, /white_two_coat_label: "白下地二層"/);
  assert.match(html, /substrate_shift_label: "黑白基材差异"/);
  assert.match(html, /substrate_shift_label: "Black-white substrate shift"/);
  assert.match(html, /substrate_shift_label: "黒白下地差"/);
  assert.match(html, /Black\/white substrate comparison/);
  assert.match(html, /whiteTwoCoat: 'White-substrate two-coat model dE2000'/);
  assert.match(html, /t \+= `\\n\$\{labels\.oneCoat\}: \$\{e\.singleDE\.toFixed\(2\)\}`;/);
  assert.match(html, /t \+= `\\n\$\{labels\.twoCoat\}: \$\{e\.dE\.toFixed\(2\)\}`;/);
  assert.match(html, /t \+= `\\n\$\{labels\.whiteTwoCoat\}: \$\{e\.whiteDE\.toFixed\(2\)\}`;/);
  assert.match(html, /t \+= `\\n\$\{labels\.substrate\}: \$\{e\.substrateShift\.toFixed\(2\)\}`;/);
});
