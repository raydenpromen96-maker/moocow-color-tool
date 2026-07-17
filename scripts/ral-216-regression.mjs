import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { availableParallelism } from 'node:os';
import { isMainThread, parentPort, Worker, workerData } from 'node:worker_threads';

import ColorCore from '../src/color-core.js';
import RecipeSearch from '../src/recipe-search.js';
import FamilySpectra from '../src/family-spectra.js';
import PaintCatalog from '../src/paint-catalog.js';
import ProductionRuntime from '../src/production-runtime.js';
import qtcCatalogue from '../src/qtc-ral-classic.js';

const EXPECTED_INPUT_SHA256 = '8d34f1e17067729d94566eb9778af14d91ee28b0b2ab850bae549e5f5de2aa64';
const EXPECTED_COLOURS_SHA256 = 'f0eb5cdb548976d570c04e78c920e4b6f4b307d7398dfbaedb99af7d5a04ccc7';
const EXPECTED_OUTPUT_SHA256 = '7e73bafb42aa59141f52c5cc8d0549f0e344990062a18b99fd158204236bd415';
const RECORD_MODE = process.argv.includes('--record');

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

function createRuntime() {
  return ProductionRuntime.create({
    ColorCore,
    RecipeSearch,
    FamilySpectra,
    paintCatalog: PaintCatalog.PAINT_DATA
  });
}

function assertCandidate(candidate, code) {
  const amounts = Object.values(candidate.recipeGpl);
  if (!amounts.length) throw new Error(`RAL ${code}: candidate has no active pigments`);
  if (amounts.length > 4) throw new Error(`RAL ${code}: candidate exceeds four active pigments`);
  if (amounts.reduce((sum, value) => sum + value, 0) !== 106) throw new Error(`RAL ${code}: candidate total is not 106 g/L`);
  if (amounts.some(value => value < 1 || !Number.isInteger(value / 0.5))) throw new Error(`RAL ${code}: candidate violates the 0.5 g/L and 1.0 g/L minimum grid`);
  for (const key of ['score', 'dE', 'hidingAlpha', 'modelSpread', 'substrateShift', 'referenceTrust']) {
    if (!Number.isFinite(candidate.metrics[key])) throw new Error(`RAL ${code}: non-finite ${key}`);
  }
}

function feasibilityTier(candidate) {
  return candidate.metrics.hidingAlpha >= 0.96 && candidate.metrics.substrateShift <= 3 ? 0 : 1;
}

function assertRecommendationOrder(candidates, code) {
  for (let index = 1; index < candidates.length; index += 1) {
    const previous = candidates[index - 1];
    const current = candidates[index];
    const previousTier = feasibilityTier(previous);
    const currentTier = feasibilityTier(current);
    if (previousTier > currentTier || (previousTier === currentTier && previous.score > current.score)) {
      throw new Error(`RAL ${code}: candidates violate strict-feasibility-first ordering`);
    }
  }
}

function runCodes(codes) {
  const runtime = createRuntime();
  const targets = new Map(PaintCatalog.buildTargets(qtcCatalogue).map(target => [target.code, target]));
  return codes.map(code => {
    const target = targets.get(code);
    if (!target) throw new Error(`RAL ${code}: target missing from QTC catalogue`);
    const candidates = runtime.generateCandidates(target);
    if (!candidates.length) throw new Error(`RAL ${code}: no candidates generated`);
    candidates.forEach(candidate => assertCandidate(candidate, code));
    assertRecommendationOrder(candidates, code);
    return {
      code,
      targetLabSource: runtime.resolveTargetColor(target).targetLabSource,
      candidates: candidates.map(candidate => {
        const { provenance, ...metrics } = candidate.metrics;
        return {
          recipe: candidate.recipe,
          supportKey: candidate.supportKey,
          metrics,
          score: candidate.score
        };
      })
    };
  });
}

if (!isMainThread) {
  try {
    parentPort.postMessage({ results: runCodes(workerData.codes) });
  } catch (error) {
    parentPort.postMessage({ error: error instanceof Error ? error.stack : String(error) });
  }
} else {
  const inputHash = sha256(await readFile(new URL('../data/qtc-ral-classic.json', import.meta.url)));
  if (inputHash !== EXPECTED_INPUT_SHA256) throw new Error(`QTC input SHA mismatch: expected ${EXPECTED_INPUT_SHA256}, got ${inputHash}`);
  if (qtcCatalogue.colorsSha256 !== EXPECTED_COLOURS_SHA256) throw new Error(`QTC colours SHA mismatch: expected ${EXPECTED_COLOURS_SHA256}, got ${qtcCatalogue.colorsSha256}`);
  if (qtcCatalogue.count !== 216 || qtcCatalogue.colors.length !== 216) throw new Error('QTC catalogue must contain exactly 216 targets');

  const codes = qtcCatalogue.colors.map(target => target.code);
  const workerCount = Math.max(1, Math.min(8, availableParallelism(), codes.length));
  const chunks = Array.from({ length: workerCount }, (_, index) => codes.filter((_, codeIndex) => codeIndex % workerCount === index));
  const workerResults = await Promise.all(chunks.map(codesForWorker => new Promise((resolve, reject) => {
    const worker = new Worker(new URL(import.meta.url), { workerData: { codes: codesForWorker } });
    worker.once('message', message => message.error ? reject(new Error(message.error)) : resolve(message.results));
    worker.once('error', reject);
    worker.once('exit', code => { if (code !== 0) reject(new Error(`RAL worker exited with code ${code}`)); });
  })));
  const results = workerResults.flat().sort((left, right) => left.code.localeCompare(right.code));
  if (results.length !== 216) throw new Error(`Expected 216 runs, got ${results.length}`);

  const bestCandidates = results.map(result => result.candidates[0]);
  const mean = key => bestCandidates.reduce((sum, candidate) => sum + candidate.metrics[key], 0) / bestCandidates.length;
  const gradeCounts = Object.fromEntries(['excellent', 'pass', 'warning', 'fail'].map(grade => [
    grade,
    bestCandidates.filter(candidate => candidate.metrics.grade === grade).length
  ]));
  const summary = {
    runs: results.length,
    nonemptyCandidates: results.filter(result => result.candidates.length > 0).length,
    candidateCount: results.reduce((sum, result) => sum + result.candidates.length, 0),
    meanTwoCoatDE: Number(mean('dE').toFixed(8)),
    minTwoCoatDE: Number(Math.min(...bestCandidates.map(candidate => candidate.metrics.dE)).toFixed(8)),
    maxTwoCoatDE: Number(Math.max(...bestCandidates.map(candidate => candidate.metrics.dE)).toFixed(8)),
    stableRecommended: bestCandidates.filter(candidate => candidate.metrics.hidingAlpha >= 0.96 && candidate.metrics.substrateShift <= 3).length,
    meanModelSpread: Number(mean('modelSpread').toFixed(8)),
    grades: gradeCounts,
    inputSha256: inputHash,
    coloursSha256: qtcCatalogue.colorsSha256
  };
  const outputHash = sha256(JSON.stringify(results));
  console.log(JSON.stringify({ summary, outputHash }, null, 2));
  if (!RECORD_MODE && outputHash !== EXPECTED_OUTPUT_SHA256) {
    throw new Error(`RAL 216 output SHA mismatch: expected ${EXPECTED_OUTPUT_SHA256}, got ${outputHash}`);
  }
}
