// 实验2/3/4：少量代表色的稳健性分析（单线程即可）
import ColorCore from '../src/color-core.js';
import RecipeSearch from '../src/recipe-search.js';
import FamilySpectra from '../src/family-spectra.js';
import PaintCatalog from '../src/paint-catalog.js';
import ProductionRuntime from '../src/production-runtime.js';
import qtcCatalogue from '../src/qtc-ral-classic.js';

function makeRuntime(mutate) {
  const catalog = JSON.parse(JSON.stringify(PaintCatalog.PAINT_DATA));
  if (mutate) mutate(catalog);
  return ProductionRuntime.create({ ColorCore, RecipeSearch, FamilySpectra, paintCatalog: catalog });
}

const base = makeRuntime();
const targets = new Map(PaintCatalog.buildTargets(qtcCatalogue).map(t => [t.code, t]));
const samples = ['1023', '3020', '5015', '7035'];

console.log('=== 实验2：单一色浆强度 ±5% 批次波动 → 同一配方的 dE 变化 ===');
console.log('（物理含义：实际色浆批次强度与假设差5%时，调好的一锅会偏多少）');
for (const code of samples) {
  const target = targets.get(code);
  if (!target) continue;
  const best = base.generateCandidates(target)[0];
  const recipePct = base.recipeGplToPercent(best.recipeGpl);
  const baseEval = base.evaluateRecipe(recipePct, target, { totalGramsPerLiter: 106 });
  let worst = 0, worstDesc = '';
  for (const pig of Object.keys(best.recipeGpl)) {
    for (const dir of [0.95, 1.05]) {
      const rt = makeRuntime(cat => { if (cat[pig]) cat[pig].strength = (cat[pig].strength || 1) * dir; });
      const ev = rt.evaluateRecipe(recipePct, target, { totalGramsPerLiter: 106 });
      const drift = Math.abs(ev.dE - baseEval.dE);
      if (drift > worst) { worst = drift; worstDesc = `${pig}×${dir}`; }
    }
  }
  console.log(`${code.padEnd(10)} 基准dE=${baseEval.dE.toFixed(2).padStart(5)}  最大漂移=${worst.toFixed(2).padStart(5)}  (最敏感:${worstDesc})  配方=${JSON.stringify(best.recipeGpl)}`);
}

console.log('\n=== 实验3：色浆 manualLab ±1 单位误差 → dE 变化 ===');
console.log('（物理含义：色浆参考色值本身有1个Lab单位误差时的影响）');
for (const code of samples.slice(0, 3)) {
  const target = targets.get(code);
  if (!target) continue;
  const best = base.generateCandidates(target)[0];
  const recipePct = base.recipeGplToPercent(best.recipeGpl);
  const baseEval = base.evaluateRecipe(recipePct, target, { totalGramsPerLiter: 106 });
  let worst = 0, worstDesc = '';
  for (const pig of Object.keys(best.recipeGpl)) {
    for (const ch of [0, 1, 2]) {
      for (const d of [-1, 1]) {
        const rt = makeRuntime(cat => { if (cat[pig]?.manualLab) cat[pig].manualLab[ch] += d; });
        const ev = rt.evaluateRecipe(recipePct, target, { totalGramsPerLiter: 106 });
        const drift = Math.abs(ev.dE - baseEval.dE);
        if (drift > worst) { worst = drift; worstDesc = `${pig} Lab[${'Lab'[ch]}]${d > 0 ? '+' : '-'}1`; }
      }
    }
  }
  console.log(`${code.padEnd(10)} 基准dE=${baseEval.dE.toFixed(2).padStart(5)}  最大漂移=${worst.toFixed(2).padStart(5)}  (${worstDesc})`);
}

console.log('\n=== 实验4：目标 Lab 扰动后首选配方是否改变 ===');
console.log('（物理含义：QTC电子参考值与实体色卡若有1-2 dE偏差，配方会怎么变）');
for (const code of samples) {
  const target = targets.get(code);
  if (!target) continue;
  const best = base.generateCandidates(target)[0];
  const perturbed = { ...target, targetLab: [target.targetLab[0] + 1, target.targetLab[1] - 0.7, target.targetLab[2] + 0.7] };
  const best2 = base.generateCandidates(perturbed)[0];
  const same = JSON.stringify(best.recipeGpl) === JSON.stringify(best2.recipeGpl);
  console.log(`${code.padEnd(10)} 首选${same ? '不变' : '改变'}: ${JSON.stringify(best.recipeGpl)}${same ? '' : ' -> ' + JSON.stringify(best2.recipeGpl)}`);
}
console.log('\n完成。');
