// ============================================================
// 合成模拟（WHAT-IF）：校准后这套工具能变多准？
// ------------------------------------------------------------
// 这不是物理测量结果，不适用于生产配方。
// 方法：
//   1. 构造"虚拟物理真值"：14支色浆的逐波长 K/S。
//      9支用 GOLDEN 实测光谱（真实测量，但非科莱恩CN批次），
//      其余5支用仓库里的近似曲线。单常数K-M线性混合作为"真值物理"。
//   2. 当前模型（3通道K/S+近似光谱混合）照常生成216色首选配方，
//      然后把这个配方放到"真值"下评估 → 现在的物理偏差量级。
//   3. 在"真值"上直接做网格搜索（模拟校准后的理想模型），
//      得到同样14支色浆能达到的最好结果 → 校准后能恢复多少。
//   两者之差 = 模型误差（可校准恢复）；后者的残差 = 色域/目标极限（校准也救不了）。
// ============================================================
import { availableParallelism } from 'node:os';
import { isMainThread, parentPort, Worker, workerData } from 'node:worker_threads';

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
// 仓库 production-runtime.js 中的近似参考光谱（非实测）
const REFERENCE_SPECTRA = {
  PY74: [0.06, 0.08, 0.12, 0.35, 0.72, 0.87, 0.90, 0.88, 0.84, 0.82, 0.80],
  'PB15:1': [0.12, 0.20, 0.34, 0.25, 0.08, 0.035, 0.025, 0.022, 0.025, 0.030, 0.040],
  PO13: [0.05, 0.05, 0.06, 0.08, 0.14, 0.30, 0.62, 0.78, 0.76, 0.70, 0.62],
  PW6: [0.92, 0.93, 0.94, 0.95, 0.96, 0.96, 0.95, 0.95, 0.94, 0.94, 0.93]
};

function getKS(r) { const rc = Math.max(0.001, Math.min(0.999, r)); return (1 - rc) ** 2 / (2 * rc); }
function getRfromKS(ks) { return 1 + ks - Math.sqrt(ks * ks + 2 * ks); }

// 构造每支色浆的"真值" K/S 光谱
function buildTruthKS() {
  const truth = {};
  for (const [code, pigment] of Object.entries(PaintCatalog.PAINT_DATA)) {
    const golden = FamilySpectra.PROFILES[pigment.ci];
    let spectrum;
    let source;
    if (golden) { spectrum = golden.reflectance; source = 'GOLDEN实测(代用)'; }
    else if (pigment.ci === 'PO73') { spectrum = REFERENCE_SPECTRA.PO13; source = 'PO13近似(PO73无数据)'; }
    else { spectrum = REFERENCE_SPECTRA[pigment.ci]; source = '仓库近似曲线'; }
    truth[code] = { ks: spectrum.map(getKS), source };
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
  const f = v => { const t = v / 100 * 100; return t > 0.008856 * 100 ? 0 : 0; }; // placeholder
  const xyz2lab = (X, Y, Z) => {
    const fx = v => { const t = v; return t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116; };
    const fxn = fx(X / 95.047), fyn = fx(Y / 100), fzn = fx(Z / 108.883);
    return [116 * fyn - 16, 500 * (fxn - fyn), 200 * (fyn - fzn)];
  };
  return xyz2lab(X, Y, Z);
}

function truthLabOfRecipe(recipeGpl, truth) {
  const entries = Object.entries(recipeGpl).filter(([, v]) => v > 0);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  const mixKs = WAVELENGTHS.map(() => 0);
  for (const [code, w] of entries) {
    const t = truth[code];
    if (!t) throw new Error('unknown code ' + code);
    t.ks.forEach((k, i) => { mixKs[i] += (w / total) * k; });
  }
  return spectrumToLab(mixKs.map(getRfromKS));
}

const POLICY = { totalGpl: 106, gridGpl: 0.5, minActiveGpl: 1.0, maxActive: 4, candidateCount: 3 };

function runCodes(codes) {
  const truth = buildTruthKS();
  const runtime = ProductionRuntime.create({ ColorCore, RecipeSearch, FamilySpectra, paintCatalog: PaintCatalog.PAINT_DATA });
  const targets = new Map(PaintCatalog.buildTargets(qtcCatalogue).map(t => [t.code, t]));
  const catalogCodes = Object.keys(PaintCatalog.PAINT_DATA);

  return codes.map(code => {
    const target = targets.get(code);
    const targetLab = target.targetLab;

    // A) 当前模型首选配方 → 放到真值下评估
    const current = runtime.generateCandidates(target)[0];
    const currentTruthLab = truthLabOfRecipe(current.recipeGpl, truth);
    const currentTruthDE = ColorCore.deltaE2000(targetLab, currentTruthLab);

    // B) 真值上直接搜索（模拟校准后的理想模型）
    // 种子沿用当前模型的色相/明度启发式，支持集覆盖全部单色+双色组合
    const seedRecipes = runtime.buildSeedRecipes(targetLab, null).map(r => runtime.recipePercentToGpl(r));
    const oracleResults = RecipeSearch.searchCandidates({
      catalog: Object.fromEntries(catalogCodes.map(c => [c, {}])),
      seeds: seedRecipes,
      evaluate: recipeGpl => ({ dE: ColorCore.deltaE2000(targetLab, truthLabOfRecipe(recipeGpl, truth)) }),
      policy: POLICY,
      maxSupports: 400,
      maxRefinementSteps: 120
    });
    const oracle = oracleResults.sort((a, b) => (a.metrics.dE - b.metrics.dE))[0];
    return {
      code,
      currentRecipe: current.recipeGpl,
      currentModelDE: current.metrics.dE,
      currentTruthDE,
      oracleRecipe: oracle.recipe,
      oracleTruthDE: oracle.metrics.dE
    };
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
  })))).flat();

  const stats = arr => {
    const s = arr.slice().sort((a, b) => a - b);
    const q = p => s[Math.min(s.length - 1, Math.floor(p * s.length))];
    return { mean: arr.reduce((a, b) => a + b, 0) / arr.length, median: q(0.5), p90: q(0.9), max: s[s.length - 1], over3: arr.filter(v => v > 3).length, over1: arr.filter(v => v > 1).length };
  };
  const cur = stats(results.map(r => r.currentTruthDE));
  const ora = stats(results.map(r => r.oracleTruthDE));
  const fmt = s => `均值=${s.mean.toFixed(2)} 中位=${s.median.toFixed(2)} P90=${s.p90.toFixed(2)} 最大=${s.max.toFixed(2)}  >3dE:${s.over3}/216  >1dE:${s.over1}/216`;

  console.log('【合成模拟 · 非实测 · 不可用于生产配方】');
  console.log('\nA) 当前模型的首选配方，放到虚拟物理真值下的偏差:');
  console.log('  ' + fmt(cur));
  console.log('\nB) 校准后理想模型（真值直接搜索）的偏差:');
  console.log('  ' + fmt(ora));

  const improved = results.slice().sort((a, b) => (b.currentTruthDE - b.oracleTruthDE) - (a.currentTruthDE - a.oracleTruthDE));
  console.log('\n校准收益最大的 10 个颜色（当前真值dE → 校准后dE）:');
  for (const r of improved.slice(0, 10)) {
    console.log(`  RAL ${r.code}: ${r.currentTruthDE.toFixed(1)} → ${r.oracleTruthDE.toFixed(1)}  (当前配方 ${JSON.stringify(r.currentRecipe)} | 校准配方 ${JSON.stringify(r.oracleRecipe)})`);
  }
  console.log('\n校准后也救不了的（oracle 仍 >3dE，色域/目标极限）:');
  const stuck = results.filter(r => r.oracleTruthDE > 3).sort((a, b) => b.oracleTruthDE - a.oracleTruthDE);
  for (const r of stuck) console.log(`  RAL ${r.code}: oracle=${r.oracleTruthDE.toFixed(1)}`);
  console.log(`  共 ${stuck.length}/216 个`);

  const { writeFile } = await import('node:fs/promises');
  await writeFile(new URL('./whatif-results.json', import.meta.url), JSON.stringify({
    disclaimer: '合成模拟结果，非物理实测，不可用于生产配方。真值光谱=GOLDEN实测代用+仓库近似曲线的单常数K-M混合。',
    summary: { currentModel: cur, calibratedOracle: ora },
    results
  }, null, 2));
  console.log('\n明细已存 experiments/whatif-results.json');
}
