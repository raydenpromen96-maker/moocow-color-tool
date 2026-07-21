// ============================================================
// 生产引擎验收：用升级后的生产引擎对 216 个 RAL 色出首选配方，
// 再用与 whatif-real-spectra.mjs 相同的"实测光谱真值"函数评估 ΔE2000。
// ------------------------------------------------------------
// 真值 = 11 波长点单常数 Kubelka-Munk 线性混合（光谱来自
// src/family-spectra.js：GOLDEN 实测 9 支 + CHSOS 实测 4 支 +
// PG7 平移锚定 PG36 1 支），按有效颜料质量（色浆克数 × pigmentContent%）
// 加权，D65/CIE1931 算 Lab，ΔE2000 对 QTC 电子色值。
// 与 whatif-real-spectra.mjs 的 oracle（均值 3.51）的区别仅在于：
// 配方来自生产引擎的启发式搜索（106g/L 网格、≤4 支、遮盖惩罚），
// 而非真值上的自由搜索。
// 注意：这是模型内自洽性验收，真值与引擎共用同一套代理光谱，
// 不能替代 45 卡实测校准。
// ============================================================
import { availableParallelism } from 'node:os';
import { isMainThread, parentPort, Worker, workerData } from 'node:worker_threads';
import { writeFile } from 'node:fs/promises';

import ColorCore from '../src/color-core.js';
import RecipeSearch from '../src/recipe-search.js';
import FamilySpectra from '../src/family-spectra.js';
import PaintCatalog from '../src/paint-catalog.js';
import ProductionRuntime from '../src/production-runtime.js';
import qtcCatalogue from '../src/qtc-ral-classic.js';

const WAVELENGTHS = [400, 430, 460, 490, 520, 550, 580, 610, 640, 670, 700];
const D65 = [82.7549, 86.6823, 117.812, 108.811, 104.79, 104.046, 95.788, 89.5991, 83.6992, 82.2778, 71.6091];
const CMF = {
  x: [0.01431, 0.2839, 0.2908, 0.03201, 0.06327, 0.4334499, 0.9163, 1.0026, 0.4479, 0.0874, 0.01135916],
  y: [0.000396, 0.0116, 0.06, 0.20802, 0.71, 0.9949501, 0.87, 0.503, 0.175, 0.032, 0.004102],
  z: [0.06785001, 1.3856, 1.6692, 0.46518, 0.07824999, 0.00875, 0.00165, 0.00034, 0.00002, 0, 0]
};

const getKS = r => { const rc = Math.max(0.001, Math.min(0.999, r)); return (1 - rc) ** 2 / (2 * rc); };
const getRfromKS = ks => 1 + ks - Math.sqrt(ks * ks + 2 * ks);

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

function buildTruth() {
  const truth = {};
  for (const [code, pigment] of Object.entries(PaintCatalog.PAINT_DATA)) {
    const profile = FamilySpectra.PROFILES[pigment.ci];
    if (!profile) throw new Error('no real spectrum for ' + code);
    truth[code] = { ks: profile.reflectance.map(getKS), source: profile.status };
  }
  return truth;
}

// 与 whatif-real-spectra.mjs truthLabOfRecipe 一致：有效颜料质量加权
function truthLabOfRecipe(recipeGpl, truth) {
  const entries = Object.entries(recipeGpl).filter(([, v]) => v > 0);
  const content = code => (PaintCatalog.PAINT_DATA[code]?.pigmentContent ?? 40) / 100;
  const total = entries.reduce((s, [c, v]) => s + v * content(c), 0);
  const mixKs = WAVELENGTHS.map(() => 0);
  for (const [code, w] of entries) {
    const m = w * content(code) / total;
    truth[code].ks.forEach((k, i) => { mixKs[i] += m * k; });
  }
  return spectrumToLab(mixKs.map(getRfromKS));
}

function runCodes(codes) {
  const truth = buildTruth();
  const runtime = ProductionRuntime.create({ ColorCore, RecipeSearch, FamilySpectra, paintCatalog: PaintCatalog.PAINT_DATA });
  const targets = new Map(PaintCatalog.buildTargets(qtcCatalogue).map(t => [t.code, t]));
  return codes.map(code => {
    const target = targets.get(code);
    const best = runtime.generateCandidates(target)[0];
    const truthLab = truthLabOfRecipe(best.recipeGpl, truth);
    const truthDE = ColorCore.deltaE2000(target.targetLab, truthLab);
    return { code, recipe: best.recipeGpl, engineDE: best.metrics.dE, grade: best.metrics.grade, truthDE };
  });
}

if (!isMainThread) {
  try { parentPort.postMessage({ results: runCodes(workerData.codes) }); }
  catch (e) { parentPort.postMessage({ error: e.stack || String(e) }); }
} else {
  const codes = qtcCatalogue.colors.map(t => t.code);
  const workerCount = Math.max(1, Math.min(8, availableParallelism()));
  const chunks = Array.from({ length: workerCount }, (_, i) => codes.filter((_, j) => j % workerCount === i));
  const results = (await Promise.all(chunks.map(cs => new Promise((resolve, reject) => {
    const w = new Worker(new URL(import.meta.url), { workerData: { codes: cs } });
    w.once('message', m => m.error ? reject(new Error(m.error)) : resolve(m.results));
    w.once('error', reject);
  })))).flat().sort((a, b) => a.code.localeCompare(b.code));

  const de = results.map(r => r.truthDE);
  const sorted = de.slice().sort((a, b) => a - b);
  const q = p => sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
  const summary = {
    mean: de.reduce((a, b) => a + b, 0) / de.length,
    median: q(0.5),
    p90: q(0.9),
    max: sorted[sorted.length - 1],
    over3: de.filter(v => v > 3).length,
    over5: de.filter(v => v > 5).length,
    over10: de.filter(v => v > 10).length
  };
  console.log('=== 生产引擎首选配方在实测光谱真值下的 ΔE2000（216 色）===');
  console.log(`均值=${summary.mean.toFixed(2)} 中位=${summary.median.toFixed(2)} P90=${summary.p90.toFixed(2)} 最大=${summary.max.toFixed(2)}`);
  console.log(`>3dE: ${summary.over3}/216  >5dE: ${summary.over5}/216  >10dE: ${summary.over10}/216`);
  console.log(`验收标准：均值 ≤ 5（oracle 理论值 3.51）→ ${summary.mean <= 5 ? '通过' : '未通过'}`);
  const worst = results.slice().sort((a, b) => b.truthDE - a.truthDE).slice(0, 15);
  console.log('\n最差的 15 个:');
  for (const r of worst) console.log(`  RAL ${r.code}: truth=${r.truthDE.toFixed(1)} engine=${r.engineDE.toFixed(1)} ${JSON.stringify(r.recipe)}`);

  await writeFile(new URL('./validate-engine-results.json', import.meta.url), JSON.stringify({
    disclaimer: '生产引擎配方在代理光谱真值下的自洽性验收，非物理实测，不能替代 45 卡校准。',
    summary,
    results
  }, null, 2));
  console.log('\n明细已存 experiments/validate-engine-results.json');
}
