// 加入 PG36（黄相酞菁绿，对应科莱恩 Colanyl Green 8G 500）后的 216 色模拟
// PG36 光谱构建（透明声明）：
//   - 形状：以 GOLDEN 实测 PG7 反射率为基础做波长平移+缩放（溴代使吸收边向长波移动）
//   - 锚点：拟合到 GOLDEN 官方公布的 PG36 本色 Lab (27.82, -11.83, -0.17)（真实公布值）
//   - 这是"有真实锚点的近似"，不是实测光谱；结论看方向，不看小数点
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
const REFERENCE_SPECTRA = {
  PY74: [0.06, 0.08, 0.12, 0.35, 0.72, 0.87, 0.90, 0.88, 0.84, 0.82, 0.80],
  'PB15:1': [0.12, 0.20, 0.34, 0.25, 0.08, 0.035, 0.025, 0.022, 0.025, 0.030, 0.040],
  PO13: [0.05, 0.05, 0.06, 0.08, 0.14, 0.30, 0.62, 0.78, 0.76, 0.70, 0.62],
  PW6: [0.92, 0.93, 0.94, 0.95, 0.96, 0.96, 0.95, 0.95, 0.94, 0.94, 0.93]
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

// 用 GOLDEN 实测 PG7 曲线拟合 PG36：平移 shift（nm）+ 增益 gain，锚定官方 Lab
function buildPG36Spectrum() {
  const pg7 = FamilySpectra.PROFILES.PG7.reflectance;
  const targetLab = [27.82, -11.83, -0.17]; // GOLDEN 官方公布 PG36 本色 Lab
  const wlAt30nm = i => 400 + i * 30;
  function shifted(shiftNm, gain) {
    return WAVELENGTHS.map(wl => {
      const srcWl = wl - shiftNm;
      const pos = (srcWl - 400) / 30;
      let v;
      if (pos <= 0) v = pg7[0];
      else if (pos >= pg7.length - 1) v = pg7[pg7.length - 1];
      else {
        const lo = Math.floor(pos), frac = pos - lo;
        v = pg7[lo] * (1 - frac) + pg7[lo + 1] * frac;
      }
      return Math.max(0.001, Math.min(0.999, v * gain));
    });
  }
  let best = null;
  for (let shift = -30; shift <= 60; shift += 1) {
    for (let gain = 0.7; gain <= 2.5; gain += 0.05) {
      const spec = shifted(shift, gain);
      const lab = spectrumToLab(spec);
      const err = Math.hypot(lab[0] - targetLab[0], lab[1] - targetLab[1], lab[2] - targetLab[2]);
      if (!best || err < best.err) best = { shift, gain, spec, lab, err };
    }
  }
  return best;
}

function buildTruthKS(pg36Spec) {
  const truth = {};
  for (const [code, pigment] of Object.entries(PaintCatalog.PAINT_DATA)) {
    const golden = FamilySpectra.PROFILES[pigment.ci];
    const spectrum = golden ? golden.reflectance
      : pigment.ci === 'PO73' ? REFERENCE_SPECTRA.PO13
      : REFERENCE_SPECTRA[pigment.ci];
    truth[code] = spectrum.map(getKS);
  }
  truth.G36 = pg36Spec.map(getKS); // 新增虚拟色浆 PG36
  return truth;
}
function truthLabOfRecipe(recipeGpl, truth) {
  const entries = Object.entries(recipeGpl).filter(([, v]) => v > 0);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  const mixKs = WAVELENGTHS.map(() => 0);
  for (const [code, w] of entries) truth[code].forEach((k, i) => { mixKs[i] += (w / total) * k; });
  return spectrumToLab(mixKs.map(getRfromKS));
}
const POLICY = { totalGpl: 106, gridGpl: 0.5, minActiveGpl: 1.0, maxActive: 4, candidateCount: 3 };

function oracleFor(targetLab, truth, runtime, codes) {
  const seeds = runtime.buildSeedRecipes(targetLab, null)
    .map(r => runtime.recipePercentToGpl(r));
  // 给 PG36 也注入种子（绿色系色相）
  seeds.push({ G36: 80, W064: 26 }, { G36: 53, Y74S: 26.5, W064: 26.5 }, { G36: 53, B150S: 26.5, W064: 26.5 }, { G36: 106 });
  const results = RecipeSearch.searchCandidates({
    catalog: Object.fromEntries(codes.map(c => [c, {}])),
    seeds,
    evaluate: r => ({ dE: ColorCore.deltaE2000(targetLab, truthLabOfRecipe(r, truth)) }),
    policy: POLICY, maxSupports: 500, maxRefinementSteps: 120
  });
  return results.sort((a, b) => a.metrics.dE - b.metrics.dE)[0];
}

function runCodes(codes) {
  const pg36 = buildPG36Spectrum();
  const truth = buildTruthKS(pg36.spec);
  const runtime = ProductionRuntime.create({ ColorCore, RecipeSearch, FamilySpectra, paintCatalog: PaintCatalog.PAINT_DATA });
  const targets = new Map(PaintCatalog.buildTargets(qtcCatalogue).map(t => [t.code, t]));
  const codes15 = Object.keys(PaintCatalog.PAINT_DATA).concat(['G36']);
  return codes.map(code => {
    const best = oracleFor(targets.get(code).targetLab, truth, runtime, codes15);
    return { code, dE: best.metrics.dE, recipe: best.recipe };
  });
}

if (!isMainThread) {
  parentPort.postMessage({ results: runCodes(workerData.codes), pg36fit: null });
} else {
  const pg36 = buildPG36Spectrum();
  console.log(`PG36 光谱构建：平移 ${pg36.shift}nm, 增益 ${pg36.gain.toFixed(2)}, 拟合 Lab=(${pg36.lab.map(v => v.toFixed(2)).join(',')}) 目标=(27.82,-11.83,-0.17) 误差=${pg36.err.toFixed(2)}`);
  console.log(`PG36 光谱(400-700nm,30nm间隔): [${pg36.spec.map(v => v.toFixed(3)).join(', ')}]`);

  const codes = qtcCatalogue.colors.map(t => t.code);
  const workerCount = Math.max(1, Math.min(8, availableParallelism()));
  const chunks = Array.from({ length: workerCount }, (_, i) => codes.filter((_, j) => j % workerCount === i));
  const results = (await Promise.all(chunks.map(cs => new Promise((resolve, reject) => {
    const w = new Worker(new URL(import.meta.url), { workerData: { codes: cs } });
    w.once('message', m => resolve(m.results));
    w.once('error', reject);
  })))).flat();

  // 与之前14支的 oracle 结果对比
  const { readFile } = await import('node:fs/promises');
  const prev = JSON.parse(await readFile(new URL('./whatif-results.json', import.meta.url), 'utf8'));
  const prevMap = new Map(prev.results.map(r => [r.code, r.oracleTruthDE]));

  const stats = arr => {
    const s = arr.slice().sort((a, b) => a - b);
    const q = p => s[Math.min(s.length - 1, Math.floor(p * s.length))];
    return { mean: arr.reduce((a, b) => a + b, 0) / arr.length, median: q(0.5), p90: q(0.9), max: s[s.length - 1], over3: arr.filter(v => v > 3).length };
  };
  const now = stats(results.map(r => r.dE));
  const before = stats(results.map(r => prevMap.get(r.code)));
  console.log('\n=== 加入 PG36 前后（校准模拟 oracle dE）===');
  console.log(`14支: 均值=${before.mean.toFixed(2)} 中位=${before.median.toFixed(2)} P90=${before.p90.toFixed(2)} >3dE:${before.over3}/216`);
  console.log(`15支: 均值=${now.mean.toFixed(2)} 中位=${now.median.toFixed(2)} P90=${now.p90.toFixed(2)} >3dE:${now.over3}/216`);

  const gained = results.map(r => ({ code: r.code, before: prevMap.get(r.code), after: r.dE, recipe: r.recipe }))
    .sort((a, b) => (b.before - b.after) - (a.before - a.after));
  console.log('\n改善最大的 12 个颜色:');
  for (const g of gained.slice(0, 12)) {
    const usesG36 = g.recipe.G36 ? `G36=${g.recipe.G36}` : '未用G36';
    console.log(`  RAL ${g.code}: ${g.before.toFixed(1)} → ${g.after.toFixed(1)}  (${usesG36}) ${JSON.stringify(g.recipe)}`);
  }
  const still = results.filter(r => r.dE > 3).sort((a, b) => b.dE - a.dE);
  console.log(`\n仍 >3dE 的 ${still.length} 个颜色（前 15）:`);
  for (const r of still.slice(0, 15)) console.log(`  RAL ${r.code}: ${r.dE.toFixed(1)}  ${JSON.stringify(r.recipe)}`);

  await writeFile(new URL('./pg36-results.json', import.meta.url), JSON.stringify({
    disclaimer: '合成模拟，PG36光谱为PG7平移拟合官方Lab的近似，非实测；结论仅指示方向',
    pg36fit: { shift: pg36.shift, gain: pg36.gain, lab: pg36.lab, err: pg36.err, spectrum: pg36.spec },
    before: { mean: before.mean, over3: before.over3 }, after: { mean: now.mean, over3: now.over3 },
    results
  }, null, 2));
  console.log('\n明细已存 experiments/pg36-results.json');
}
