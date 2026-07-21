// ============================================================
// 合成模拟 v2：真值曲线全部换成实测光谱后的 216 色精度评估
// ------------------------------------------------------------
// 这不是物理测量结果，不适用于生产配方。
// 与 whatif-calibrated.mjs 的唯一区别：真值光谱来源
//   - Y74S(PY74) / B150S(PB15:1) / 073(PO73) / W064(PW6金红石)：
//     由"仓库手编近似曲线"换成 CHSOS Pigments Checker 实测反射率
//     （丙烯粘合剂涂布样本，光纤反射光谱仪实测，chsopensource.org）
//   - G36(PG36，新增第15支)：CHSOS PG36 样本不具代表性（过浅且无法拟合
//     官方本色 Lab），弃用；改用 PG7 实测平移+官方本色 Lab 锚定
//   - 其余 9 支：继续用 GOLDEN 官方实测光谱（family-spectra.js）
// 模拟方法不变：11 波长点单常数 K-M 线性混合作为"虚拟物理真值"，
// D65/CIE1931 计算 Lab，dE2000 评估。
// 已知局限（如实声明）：
//   1. 实测光谱来自 GOLDEN/通用颜料样本，非科莱恩 Colanyl 色浆批次
//   2. CHSOS PB15 未区分 15:1/15:3 晶型（B150S 用它近似）
//   3. GOLDEN 数据是白卡上的半透明刮膜，白卡会抬升透明颜料（酞菁类）
//      的长波反射率，导致冲淡色偏红的倾向被放大（50xx 蓝色系偏悲观）
//   4. GOLDEN 炭黑本身偏弱（R≈4.6%），深黑/深灰结论偏保守
//   5. 单常数 K-M 对深色/高浓度配方精度有限
// ============================================================
import { availableParallelism } from 'node:os';
import { isMainThread, parentPort, Worker, workerData } from 'node:worker_threads';
import { readFile, writeFile } from 'node:fs/promises';

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

// ---- 载入 CHSOS 实测 CSV 并重采样到 11 波长点 ----
const SPECTRA_DIR = new URL('../../spectral_data/pigments/', import.meta.url);
const CHSOS_FILES = {
  PY74: 'PY74_arylide_yellow_5GX.csv',
  'PB15:1': 'PB15_phthalo_blue.csv',   // CHSOS 未分晶型，代用
  PO73: 'PO73_pyrrole_orange.csv',
  PW6: 'PW6_titanium_white.csv'        // 金红石型
};

async function loadChsos(file) {
  const text = await readFile(new URL(file, SPECTRA_DIR), 'utf8');
  const pts = [];
  for (const line of text.split(/\r?\n/).slice(1)) {
    const [wl, r] = line.split(',').map(Number);
    if (Number.isFinite(wl) && Number.isFinite(r)) pts.push([wl, Math.max(0.001, Math.min(0.999, r / 100))]);
  }
  pts.sort((a, b) => a[0] - b[0]);
  // 线性插值到 11 点
  return WAVELENGTHS.map(wl => {
    if (wl <= pts[0][0]) return pts[0][1];
    if (wl >= pts[pts.length - 1][0]) return pts[pts.length - 1][1];
    let lo = 0;
    while (lo < pts.length - 2 && pts[lo + 1][0] < wl) lo++;
    const [w0, r0] = pts[lo], [w1, r1] = pts[lo + 1];
    return r0 + (r1 - r0) * (wl - w0) / (w1 - w0);
  });
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

let CHSOS = null; // worker 内惰性加载
async function ensureChsos() {
  if (CHSOS) return CHSOS;
  CHSOS = {};
  for (const [ci, file] of Object.entries(CHSOS_FILES)) CHSOS[ci] = await loadChsos(file);
  return CHSOS;
}

async function buildTruthKS() {
  const chsos = await ensureChsos();
  const truth = {};
  const sources = {};
  for (const [code, pigment] of Object.entries(PaintCatalog.PAINT_DATA)) {
    const golden = FamilySpectra.PROFILES[pigment.ci];
    let spectrum, source;
    if (chsos[pigment.ci]) { spectrum = chsos[pigment.ci]; source = 'CHSOS实测'; }
    else if (golden) { spectrum = golden.reflectance; source = 'GOLDEN实测'; }
    else throw new Error('no real spectrum for ' + code);
    truth[code] = { ks: spectrum.map(getKS), source, masstoneLab: spectrumToLab(spectrum) };
    sources[code] = source;
  }
  // 新增第 15 支：G36 = PG36 黄相酞菁绿
  // CHSOS 实测样本不可用：涂布过浅（L=66.8，GOLDEN 官方 PG36 本色 L=27.8），
  // 且其曲线形状在任何 K/S 强度缩放（含除白修正）下都无法达到官方本色 Lab
  // （最优误差 17.7，b* 停在 +14 vs 官方 -0.17）——判定该样本不具代表性，弃用。
  // 处理：以 GOLDEN 实测 PG7（同族颜料）曲线做波长平移+增益拟合，
  // 锚定 GOLDEN 官方公布 PG36 本色 Lab (27.82,-11.83,-0.17)，拟合误差 ~0.3。
  // 形状=同族实测外推，强度/色相=官方公布锚点；属"有锚点的近似"，非纯实测。
  const pg36fit = fitPG36Shift(FamilySpectra.PROFILES.PG7.reflectance);
  truth.G36 = { ks: pg36fit.spec.map(getKS), source: `PG7实测平移+官方本色锚定(移${pg36fit.shift}nm,误差${pg36fit.err.toFixed(2)})`, masstoneLab: pg36fit.lab };
  return { truth, sources, pg36fit };
}

// PG7 平移+增益拟合 PG36（溴代使吸收边向长波移动），锚定官方本色 Lab
function fitPG36Shift(pg7) {
  const targetLab = [27.82, -11.83, -0.17]; // GOLDEN 官方公布 PG36 本色 Lab
  function shifted(shiftNm, gain) {
    return WAVELENGTHS.map(wl => {
      const pos = (wl - shiftNm - 400) / 30;
      let v;
      if (pos <= 0) v = pg7[0];
      else if (pos >= pg7.length - 1) v = pg7[pg7.length - 1];
      else { const lo = Math.floor(pos), f = pos - lo; v = pg7[lo] * (1 - f) + pg7[lo + 1] * f; }
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

// 科莱恩官方手册（Heubach Colanyl 100/500 手册，2022-2023 版）的颜料含量%
// 用于把混合权重从"色浆总重"改为"有效颜料质量"——水/乙二醇/助剂成膜后
// 不占体积，按颜料质量混合更接近真实漆膜物理。
// 注意：① CN 系列含量未公开，用全球同牌号值近似 ② 标 * 的为估计值
const PIGMENT_CONTENT = {
  Y74S: 48,   // Yellow 2GXD 130 = PY74, 官方 48%
  Y83S: 35,   // Yellow HR 132 = PY83, 官方 35%
  '073': 30,  // Orange D2R 100-CN = PO73（用户确认 CI 56117，2026-07-21）；含量未公开按 30% 估
  R254D: 50,  // DPP Red GD 131-CN = PR254（用户确认，2026-07-21）；同系 D3GD 500 官方 50%
  R122S: 20,  // Pink E 130 = PR122, 官方 20%
  R101V: 65,  // Oxide Red G 130 = PR101, 官方 65%
  R101Y: 72,  // Oxide Red B 132 = PR101, 官方 72%
  Y42S: 66,   // Oxide Yellow R 132 = PY42, 官方 66%
  BK7H: 42,   // Black N 131 = PBk7, 官方 42%
  W064: 55,   // *White TQ 100-CN 未公开（密度 1.83 < White R 的 2.18@70%），估 55%
  V23: 30,    // Violet RL 132 = PV23, 官方 30%
  G7: 50,     // Green GG 131 = PG7, 官方 50%
  B150S: 40,  // Blue A2R 131 = PB15:1, 官方 40%
  B153S: 47,  // Blue B2G 131 = PB15:3, 官方 47%
  G36: 30     // *Green 8G 500 (PG36) 未公开，估 30%
};

function truthLabOfRecipe(recipeGpl, truth) {
  const entries = Object.entries(recipeGpl).filter(([, v]) => v > 0);
  // 有效颜料质量 = 色浆克数 × 官方颜料含量
  const total = entries.reduce((s, [c, v]) => s + v * (PIGMENT_CONTENT[c] ?? 40) / 100, 0);
  const mixKs = WAVELENGTHS.map(() => 0);
  for (const [code, w] of entries) {
    const m = w * (PIGMENT_CONTENT[code] ?? 40) / 100 / total;
    truth[code].ks.forEach((k, i) => { mixKs[i] += m * k; });
  }
  return spectrumToLab(mixKs.map(getRfromKS));
}

const POLICY = { totalGpl: 106, gridGpl: 0.5, minActiveGpl: 1.0, maxActive: 4, candidateCount: 3 };

async function runCodes(codes) {
  const { truth } = await buildTruthKS();
  const runtime = ProductionRuntime.create({ ColorCore, RecipeSearch, FamilySpectra, paintCatalog: PaintCatalog.PAINT_DATA });
  const targets = new Map(PaintCatalog.buildTargets(qtcCatalogue).map(t => [t.code, t]));
  const catalogCodes = Object.keys(PaintCatalog.PAINT_DATA);
  const codes15 = catalogCodes.concat(['G36']);

  return codes.map(code => {
    const target = targets.get(code);
    const targetLab = target.targetLab;

    // A) 当前模型首选配方 → 放到"实测光谱真值"下评估
    const current = runtime.generateCandidates(target)[0];
    const currentTruthLab = truthLabOfRecipe(current.recipeGpl, truth);
    const currentTruthDE = ColorCore.deltaE2000(targetLab, currentTruthLab);

    // B) 15 支（含 G36）在真值上直接搜索 = 校准后的理想上限
    const seeds = runtime.buildSeedRecipes(targetLab, null).map(r => runtime.recipePercentToGpl(r));
    seeds.push({ G36: 80, W064: 26 }, { G36: 53, Y74S: 26.5, W064: 26.5 }, { G36: 53, B150S: 26.5, W064: 26.5 }, { G36: 106 });
    const oracleResults = RecipeSearch.searchCandidates({
      catalog: Object.fromEntries(codes15.map(c => [c, {}])),
      seeds,
      evaluate: r => ({ dE: ColorCore.deltaE2000(targetLab, truthLabOfRecipe(r, truth)) }),
      policy: POLICY, maxSupports: 500, maxRefinementSteps: 120
    });
    const oracle = oracleResults.sort((a, b) => a.metrics.dE - b.metrics.dE)[0];
    return { code, currentRecipe: current.recipeGpl, currentTruthDE, oracleRecipe: oracle.recipe, oracleTruthDE: oracle.metrics.dE };
  });
}

if (!isMainThread) {
  try { parentPort.postMessage({ results: await runCodes(workerData.codes) }); }
  catch (e) { parentPort.postMessage({ error: e.stack || String(e) }); }
} else {
  const { truth, sources } = await buildTruthKS();
  console.log('=== 真值光谱来源（全部实测，无手编曲线）===');
  for (const [code, t] of Object.entries(truth)) {
    console.log(`  ${code.padEnd(6)} ${t.source}  本色Lab=(${t.masstoneLab.map(v => v.toFixed(1)).join(', ')})`);
  }

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

  // 与旧结果对比（旧真值含手编曲线；v2=按色浆总重混合的实测真值）
  const prev14 = JSON.parse(await readFile(new URL('./whatif-results.json', import.meta.url), 'utf8'));
  const prev15 = JSON.parse(await readFile(new URL('./pg36-results.json', import.meta.url), 'utf8'));
  const prevV2 = JSON.parse(await readFile(new URL('./real-spectra-results.json', import.meta.url), 'utf8'));
  const p14 = new Map(prev14.results.map(r => [r.code, r]));
  const p15 = new Map(prev15.results.map(r => [r.code, r]));
  const pV2 = new Map(prevV2.results.map(r => [r.code, r]));

  console.log('\n=== 对比：编造曲线 → 实测光谱(按色浆重) → 实测+官方颜料含量(按颜料质量) ===');
  console.log('A) 当前模型配方在真值下的偏差:');
  console.log('  编造曲线真值:      ' + fmt(stats(results.map(r => p14.get(r.code).currentTruthDE))));
  console.log('  实测,按色浆重:     ' + fmt(stats(results.map(r => pV2.get(r.code).currentTruthDE))));
  console.log('  实测,按颜料质量:   ' + fmt(cur));
  console.log('B) 校准后理想模型（oracle）的偏差:');
  console.log('  编造曲线14支:      ' + fmt(stats(results.map(r => p14.get(r.code).oracleTruthDE))));
  console.log('  编造曲线15支:      ' + fmt(stats(results.map(r => p15.get(r.code).dE))));
  console.log('  实测15支,按色浆重: ' + fmt(stats(results.map(r => pV2.get(r.code).oracleTruthDE))));
  console.log('  实测15支,按颜料质量:' + fmt(ora));

  // 结论变化最大的颜色：v2 oracle vs v3 oracle
  const shifted = results.map(r => ({ code: r.code, oldOracle: pV2.get(r.code).oracleTruthDE, newOracle: r.oracleTruthDE, recipe: r.oracleRecipe }))
    .sort((a, b) => Math.abs(b.newOracle - b.oldOracle) - Math.abs(a.newOracle - a.oldOracle));
  console.log('\n按颜料质量混合后结论差异最大的 10 个颜色（按色浆重oracle → 按颜料质量oracle）:');
  for (const s of shifted.slice(0, 10)) {
    console.log(`  RAL ${s.code}: ${s.oldOracle.toFixed(1)} → ${s.newOracle.toFixed(1)}  ${JSON.stringify(s.recipe)}`);
  }

  const stuck = results.filter(r => r.oracleTruthDE > 3).sort((a, b) => b.oracleTruthDE - a.oracleTruthDE);
  console.log(`\n新真值下 oracle 仍 >3dE 的 ${stuck.length}/216 个（前15）:`);
  for (const r of stuck.slice(0, 15)) console.log(`  RAL ${r.code}: ${r.oracleTruthDE.toFixed(1)}  ${JSON.stringify(r.oracleRecipe)}`);

  await writeFile(new URL('./real-spectra-v3-results.json', import.meta.url), JSON.stringify({
    disclaimer: '合成模拟结果，非物理实测，不可用于生产配方。真值=CHSOS+GOLDEN实测光谱，按科莱恩官方颜料含量%折算颜料质量做单常数K-M混合；CN系列含量为估计值。',
    pigmentContent: PIGMENT_CONTENT,
    spectraSources: sources,
    summary: { currentModel: cur, oracle15: ora },
    results
  }, null, 2));
  console.log('\n明细已存 experiments/real-spectra-v3-results.json');
}
