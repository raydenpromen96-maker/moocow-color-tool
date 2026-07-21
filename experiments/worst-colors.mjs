// 实验1：多线程跑全部216色，输出最差颜色明细
import { availableParallelism } from 'node:os';
import { isMainThread, parentPort, Worker, workerData } from 'node:worker_threads';

import ColorCore from '../src/color-core.js';
import RecipeSearch from '../src/recipe-search.js';
import FamilySpectra from '../src/family-spectra.js';
import PaintCatalog from '../src/paint-catalog.js';
import ProductionRuntime from '../src/production-runtime.js';
import qtcCatalogue from '../src/qtc-ral-classic.js';

function runCodes(codes) {
  const runtime = ProductionRuntime.create({ ColorCore, RecipeSearch, FamilySpectra, paintCatalog: PaintCatalog.PAINT_DATA });
  const targets = new Map(PaintCatalog.buildTargets(qtcCatalogue).map(t => [t.code, t]));
  return codes.map(code => {
    const best = runtime.generateCandidates(targets.get(code))[0];
    return {
      code,
      dE: best.metrics.dE,
      modelSpread: best.metrics.modelSpread,
      substrateShift: best.metrics.substrateShift,
      grade: best.metrics.grade,
      recipe: best.recipeGpl
    };
  });
}

if (!isMainThread) {
  parentPort.postMessage({ results: runCodes(workerData.codes) });
} else {
  const codes = qtcCatalogue.colors.map(t => t.code);
  const workerCount = Math.max(1, Math.min(8, availableParallelism()));
  const chunks = Array.from({ length: workerCount }, (_, i) => codes.filter((_, j) => j % workerCount === i));
  const results = (await Promise.all(chunks.map(cs => new Promise((resolve, reject) => {
    const w = new Worker(new URL(import.meta.url), { workerData: { codes: cs } });
    w.once('message', m => resolve(m.results));
    w.once('error', reject);
  })))).flat();

  results.sort((a, b) => b.dE - a.dE);
  console.log('=== 最差 15 个颜色（模型内部两遍 dE2000）===');
  for (const r of results.slice(0, 15)) {
    console.log(`${r.code.padEnd(10)} dE=${r.dE.toFixed(2).padStart(6)}  spread=${r.modelSpread.toFixed(1).padStart(5)}  shift=${r.substrateShift.toFixed(1).padStart(5)}  ${r.grade.padEnd(9)} recipe=${JSON.stringify(r.recipe)}`);
  }
  const spreads = results.map(r => r.modelSpread).sort((a, b) => a - b);
  console.log(`\nmodelSpread 中位数=${spreads[Math.floor(spreads.length / 2)].toFixed(2)}`);
  console.log(`modelSpread>8 的颜色数=${results.filter(r => r.modelSpread > 8).length}/216`);
  console.log(`dE>6(fail) 的颜色数=${results.filter(r => r.dE > 6).length}/216, dE>3 的颜色数=${results.filter(r => r.dE > 3).length}/216`);
}
