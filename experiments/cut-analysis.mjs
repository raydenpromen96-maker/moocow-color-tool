// 色浆去留分析：使用统计 + 逐个砍除影响（基于合成模拟，非实测）
import { readFile, writeFile } from 'node:fs/promises';
import { availableParallelism } from 'node:os';
import { isMainThread, parentPort, Worker, workerData } from 'node:worker_threads';

import ColorCore from '../src/color-core.js';
import RecipeSearch from '../src/recipe-search.js';
import FamilySpectra from '../src/family-spectra.js';
import PaintCatalog from '../src/paint-catalog.js';
import ProductionRuntime from '../src/production-runtime.js';
import qtcCatalogue from '../src/qtc-ral-classic.js';

const WAVELENGTHS = [400, 430, 460, 490, 520, 550, 580, 610, 640, 670, 700];
const D65 = [82.7549, 86.6823, 117.812, 108.811, 104.79, 104.046, 95.788, 89.5991, 83.6991, 82.2778, 71.6091];
const CMF = {
  x: [0.01431, 0.2839, 0.2908, 0.03201, 0.06327, 0.4334499, 0.9163, 1.0026, 0.4479, 0.0874, 0.01135916],
  y: [0.000396, 0.0116, 0.06, 0.20802, 0.71, 0.9949501, 0.87, 0.503, 0.175, 0.032, 0.004102],
  z: [0.06785001, 1.3856, 1.6692, 0.46518, 0.07824999, 0.00875, 0.00165, 0.00034, 0.00002, 0, 0]
};
const REFERENCE_SPECTRA = {
  PY74: [0.06, 0.08, 0.12, 0.35, 0.72, 0.87, 0.90, 0.88, 0.84, 0.82, 0.80],
  'PB15:1': [0.12, 0.20, 0.34, 0.25, 0.08, 0.035, 0.025, 0.022, 0.025, 0.030, 0.040],
  PO13: [0.05, 0.05, 0.06, 0.08, 0.14, 0.30, 0.62, 0.78, 0.76, 0.70, 0.62],
  PW6: [0.92, 0.93, 0.94, 0.95, 0.96, 0.96, 0.95, 0.95, 0.94, 0.94, 0.93]
};
function getKS(r) { const rc = Math.max(0.001, Math.min(0.999, r)); return (1 - rc) ** 2 / (2 * rc); }
function getRfromKS(ks) { return 1 + ks - Math.sqrt(ks * ks + 2 * ks); }
function buildTruthKS() {
  const truth = {};
  for (const [code, pigment] of Object.entries(PaintCatalog.PAINT_DATA)) {
    const golden = FamilySpectra.PROFILES[pigment.ci];
    const spectrum = golden ? golden.reflectance
      : pigment.ci === 'PO73' ? REFERENCE_SPECTRA.PO13
      : REFERENCE_SPECTRA[pigment.ci];
    truth[code] = spectrum.map(getKS);
  }
  return truth;
}
function spectrumToLab(reflectance) {
  let X = 0, Y = 0, Z = 0, wX = 0, wY = 0, wZ = 0;
  reflectance.forEach((r, i) => {
    X += r * D65[i] * CMF.x[i]; Y += r * D65[i] * CMF.y[i]; Z += r * D65[i] * CMF.z[i];
    wX += D65[i] * CMF.x[i]; wY += D65[i] * CMF.y[i]; wZ += D65[i] * CMF.z[i];
  });
  X = X / wX * 95.047; Y = Y / wY * 100; Z = Z / wZ * 108.883;
  const f = v => v > 0.008856 ? Math.cbrt(v) : 7.787 * v + 16 / 116;
  const fx = f(X / 95.047), fy = f(Y / 100), fz = f(Z / 108.883);
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}
function truthLabOfRecipe(recipeGpl, truth) {
  const entries = Object.entries(recipeGpl).filter(([, v]) => v > 0);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  const mixKs = WAVELENGTHS.map(() => 0);
  for (const [code, w] of entries) truth[code].forEach((k, i) => { mixKs[i] += (w / total) * k; });
  return spectrumToLab(mixKs.map(getRfromKS));
}
const POLICY = { totalGpl: 106, gridGpl: 0.5, minActiveGpl: 1.0, maxActive: 4, candidateCount: 3 };

function oracleFor(code, targetLab, truth, runtime, exclude) {
  const codes = Object.keys(PaintCatalog.PAINT_DATA).filter(c => c !== exclude);
  const seeds = runtime.buildSeedRecipes(targetLab, null)
    .map(r => runtime.recipePercentToGpl(r))
    .map(r => Object.fromEntries(Object.entries(r).filter(([c]) => c !== exclude)))
    .filter(r => Object.keys(r).length);
  const results = RecipeSearch.searchCandidates({
    catalog: Object.fromEntries(codes.map(c => [c, {}])),
    seeds,
    evaluate: r => ({ dE: ColorCore.deltaE2000(targetLab, truthLabOfRecipe(r, truth)) }),
    policy: POLICY, maxSupports: 400, maxRefinementSteps: 120
  });
  return results.sort((a, b) => a.metrics.dE - b.metrics.dE)[0];
}

if (!isMainThread) {
  const { tasks } = workerData;
  const truth = buildTruthKS();
  const runtime = ProductionRuntime.create({ ColorCore, RecipeSearch, FamilySpectra, paintCatalog: PaintCatalog.PAINT_DATA });
  const targets = new Map(PaintCatalog.buildTargets(qtcCatalogue).map(t => [t.code, t]));
  const out = tasks.map(({ code, exclude }) => {
    const best = oracleFor(code, targets.get(code).targetLab, truth, runtime, exclude);
    return { code, exclude, dE: best.metrics.dE, recipe: best.recipe };
  });
  parentPort.postMessage({ results: out });
} else {
  const data = JSON.parse(await readFile(new URL('./whatif-results.json', import.meta.url), 'utf8'));
  const base = data.results;
  const ALL = Object.keys(PaintCatalog.PAINT_DATA);

  // 1) 使用统计（基于校准模拟配方）
  console.log('=== 色浆使用统计（216色 校准模拟配方）===');
  const usage = {};
  for (const c of ALL) usage[c] = { count: 0, totalDose: 0, heavy: [] };
  for (const r of base) {
    for (const [c, dose] of Object.entries(r.oracleRecipe)) {
      usage[c].count++; usage[c].totalDose += dose;
      if (dose >= 50) usage[c].heavy.push(`RAL ${r.code}`);
    }
  }
  for (const [c, u] of Object.entries(usage).sort((a, b) => b[1].count - a[1].count)) {
    console.log(`${c.padEnd(7)} 出现 ${String(u.count).padStart(3)}/216 色, 平均剂量 ${(u.totalDose / Math.max(1, u.count)).toFixed(1)} g/L, 主导(>=50g/L) ${u.heavy.length} 色`);
  }

  // 2) 逐个砍除：只重测该色浆参与的颜色
  console.log('\n=== 逐个砍除测试（该色浆参与的颜色，砍掉后重新搜索）===');
  const tasks = [];
  for (const c of ALL) {
    for (const r of base) {
      if (r.oracleRecipe[c] > 0) tasks.push({ code: r.code, exclude: c, baseDE: r.oracleTruthDE });
    }
  }
  const workerCount = Math.max(1, Math.min(8, availableParallelism()));
  const chunks = Array.from({ length: workerCount }, (_, i) => tasks.filter((_, j) => j % workerCount === i));
  const results = (await Promise.all(chunks.map(cs => new Promise((resolve, reject) => {
    const w = new Worker(new URL(import.meta.url), { workerData: { tasks: cs } });
    w.once('message', m => resolve(m.results));
    w.once('error', reject);
  })))).flat();

  const taskMap = new Map(tasks.map((t, i) => [`${t.code}|${t.exclude}`, t]));
  const impact = {};
  for (const c of ALL) impact[c] = { tested: 0, broke: [], maxJump: 0, sumJump: 0 };
  for (const r of results) {
    const key = `${r.code}|${r.exclude}`;
    const baseDE = taskMap.get(key).baseDE;
    const jump = r.dE - baseDE;
    const imp = impact[r.exclude];
    imp.tested++;
    imp.sumJump += Math.max(0, jump);
    imp.maxJump = Math.max(imp.maxJump, jump);
    if (baseDE <= 3 && r.dE > 3) imp.broke.push(`RAL ${r.code}(${baseDE.toFixed(1)}→${r.dE.toFixed(1)})`);
  }
  console.log('色浆      受影响色数  从<=3dE掉到>3dE  平均恶化  最大恶化');
  for (const [c, imp] of Object.entries(impact).sort((a, b) => b[1].broke.length - a[1].broke.length || b[1].sumJump - a[1].sumJump)) {
    console.log(`${c.padEnd(9)} ${String(imp.tested).padStart(5)}     ${String(imp.broke.length).padStart(5)}        ${(imp.sumJump / Math.max(1, imp.tested)).toFixed(2).padStart(6)}  ${imp.maxJump.toFixed(2).padStart(6)}`);
  }
  console.log('\n各候选"可砍"色浆砍后崩掉的颜色明细（前8条）:');
  for (const [c, imp] of Object.entries(impact)) {
    if (imp.broke.length <= 6) console.log(`${c}: [${imp.broke.slice(0, 8).join(', ')}]`);
  }
  await writeFile(new URL('./cut-analysis.json', import.meta.url), JSON.stringify({ disclaimer: '基于合成模拟，非实测，仅指示方向', usage, impact }, null, 2));
  console.log('\n明细已存 experiments/cut-analysis.json');
}
