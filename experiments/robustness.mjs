// 稳健性实验：评估模型对色浆参数误差的敏感性（模拟真实批次波动）
import ColorCore from '../src/color-core.js';
import RecipeSearch from '../src/recipe-search.js';
import FamilySpectra from '../src/family-spectra.js';
import PaintCatalog from '../src/paint-catalog.js';
import ProductionRuntime from '../src/production-runtime.js';
import qtcCatalogue from '../src/qtc-ral-classic.js';

function makeRuntime(mutate) {
  const catalog = JSON.parse(JSON.stringify(PaintCatalog.PAINT_DATA).replace(/\\u[\dA-Fa-f]{4}/g, m => m));
  if (mutate) mutate(catalog);
  return ProductionRuntime.create({ ColorCore, RecipeSearch, FamilySpectra, paintCatalog: catalog });
}

const base = makeRuntime();
const targets = new Map(PaintCatalog.buildTargets(qtcCatalogue).map(t => [t.code, t]));

// ---- 实验 1：全部216色，找出最差的颜色 ----
const all = [];
for (const [code, target] of targets) {
  const candidates = base.generateCandidates(target);
  const best = candidates[0];
  all.push({
    code,
    dE: best.metrics.dE,
    modelSpread: best.metrics.modelSpread,
    substrateShift: best.metrics.substrateShift,
    hiding: best.metrics.hidingAlpha,
    grade: best.metrics.grade,
    recipe: best.recipeGpl
  });
}
all.sort((a, b) => b.dE - a.dE);
console.log('=== 最差 15 个颜色（模型内部两遍 dE2000）===');
for (const r of all.slice(0, 15)) {
  console.log(`${r.code.padEnd(10)} dE=${r.dE.toFixed(2).padStart(6)}  spread=${r.modelSpread.toFixed(1).padStart(5)}  shift=${r.substrateShift.toFixed(1).padStart(5)}  grade=${r.grade}  recipe=${JSON.stringify(r.recipe)}`);
}
const spreads = all.map(r => r.modelSpread).sort((a, b) => a - b);
console.log(`\nmodelSpread 中位数=${spreads[108].toFixed(2)}, >8dE 的颜色数=${all.filter(r => r.modelSpread > 8).length}/216`);

// ---- 实验 2：批次强度 ±5% 扰动下的 dE 漂移 ----
// 物理含义：如果你的实际色浆批次强度与假设值差 5%，同一配方会偏多少
console.log('\n=== 实验 2：色浆强度 ±5% 批次波动导致的 dE 漂移 ===');
const samples = ['RAL 1023', 'RAL 3020', 'RAL 5015', 'RAL 6024', 'RAL 7035', 'RAL 8017', 'RAL 1015', 'RAL 3000'];
for (const code of samples) {
  const target = targets.get(code);
  if (!target) { console.log(`${code}: 不存在`); continue; }
  const best = base.generateCandidates(target)[0];
  const recipePct = base.recipeGplToPercent(best.recipeGpl);
  const baseEval = base.evaluateRecipe(recipePct, target, { totalGramsPerLiter: 106 });
  let worst = 0, worstPigment = '';
  for (const pig of Object.keys(best.recipeGpl)) {
    for (const dir of [0.95, 1.05]) {
      const rt = makeRuntime(cat => { if (cat[pig]) cat[pig].strength = (cat[pig].strength || 1) * dir; });
      const ev = rt.evaluateRecipe(recipePct, target, { totalGramsPerLiter: 106 });
      const drift = Math.abs(ev.dE - baseEval.dE);
      if (drift > worst) { worst = drift; worstPigment = `${pig}×${dir}`; }
    }
  }
  console.log(`${code.padEnd(10)} 基准dE=${baseEval.dE.toFixed(2).padStart(5)}  ±5%强度扰动最大漂移=${worst.toFixed(2).padStart(5)}  (最敏感: ${worstPigment})`);
}

// ---- 实验 3：manualLab ±1 单位误差的影响 ----
// 物理含义：色浆本身的 Lab 参考值如果有 1 个单位的测量/录入误差
console.log('\n=== 实验 3：色浆 Lab 参考值 ±1 单位误差导致的 dE 漂移 ===');
for (const code of samples.slice(0, 5)) {
  const target = targets.get(code);
  if (!target) continue;
  const best = base.generateCandidates(target)[0];
  const recipePct = base.recipeGplToPercent(best.recipeGpl);
  const baseEval = base.evaluateRecipe(recipePct, target, { totalGramsPerLiter: 106 });
  let worst = 0, worstDesc = '';
  for (const pig of Object.keys(best.recipeGpl)) {
    for (const ch of [0, 1, 2]) {
      for (const d of [-1, 1]) {
        const rt = makeRuntime(cat => {
          if (cat[pig] && cat[pig].manualLab) cat[pig].manualLab[ch] += d;
        });
        const ev = rt.evaluateRecipe(recipePct, target, { totalGramsPerLiter: 106 });
        const drift = Math.abs(ev.dE - baseEval.dE);
        if (drift > worst) { worst = drift; worstDesc = `${pig} Lab[${ch}]${d > 0 ? '+' : '-'}1`; }
      }
    }
  }
  console.log(`${code.padEnd(10)} 基准dE=${baseEval.dE.toFixed(2).padStart(5)}  Lab±1 最大漂移=${worst.toFixed(2).padStart(5)}  (${worstDesc})`);
}

// ---- 实验 4：目标值本身的不确定性 ----
// QTC 电子参考值 vs 实体 RAL 卡实测可能差 1-2 dE，看候选排序是否稳定
console.log('\n=== 实验 4：目标 Lab 扰动 ±1 后候选配方变化 ===');
for (const code of samples.slice(0, 4)) {
  const target = targets.get(code);
  if (!target) continue;
  const best = base.generateCandidates(target)[0];
  const perturbed = { ...target, targetLab: [target.targetLab[0] + 1, target.targetLab[1] - 0.7, target.targetLab[2] + 0.7] };
  const best2 = base.generateCandidates(perturbed)[0];
  const same = JSON.stringify(best.recipeGpl) === JSON.stringify(best2.recipeGpl);
  console.log(`${code.padEnd(10)} 首选配方${same ? '不变' : '改变'}: ${JSON.stringify(best.recipeGpl)} -> ${JSON.stringify(best2.recipeGpl)}`);
}
console.log('\n完成。');
