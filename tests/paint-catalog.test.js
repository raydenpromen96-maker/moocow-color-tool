const assert = require('node:assert/strict');
const test = require('node:test');

const PaintCatalog = require('../src/paint-catalog.js');
const qtcCatalogue = require('../src/qtc-ral-classic.js');

test('paint catalog exports immutable raw pigment data and the six exact presets', () => {
  assert.deepEqual(Object.keys(PaintCatalog).sort(), ['PAINT_DATA', 'RAL_BASE_RECIPES', 'buildTargets']);
  // v5: 15 支（新增第 15 支 G36 / PG36，待采购）
  assert.equal(Object.keys(PaintCatalog.PAINT_DATA).length, 15);
  assert.ok(Object.isFrozen(PaintCatalog.PAINT_DATA));
  assert.ok(Object.isFrozen(PaintCatalog.PAINT_DATA.R254D));
  assert.ok(Object.isFrozen(PaintCatalog.PAINT_DATA.R254D.aliases));
  assert.ok(Object.isFrozen(PaintCatalog.PAINT_DATA.Y83S.manualLab));
  // v5: 每支色浆必须有官方/估计颜料含量%（有效颜料质量混合权重）与湿密度（mL 换算）
  Object.entries(PaintCatalog.PAINT_DATA).forEach(([code, pigment]) => {
    assert.ok(Number.isFinite(pigment.pigmentContent) && pigment.pigmentContent > 0, `${code} pigmentContent`);
    assert.ok(Number.isFinite(pigment.density) && pigment.density > 0, `${code} density`);
  });
  // 估计值必须机器可读地标注
  for (const code of ['073', 'W064', 'G36']) {
    assert.equal(PaintCatalog.PAINT_DATA[code].pigmentContentStatus, 'estimated_unpublished', `${code} estimated pigment content`);
  }
  const g36 = PaintCatalog.PAINT_DATA.G36;
  assert.equal(g36.ci, 'PG36');
  assert.equal(g36.purchaseStatus, 'pending_purchase');
  assert.equal(g36.densityUnitStatus, 'estimated_not_measured');
  assert.deepEqual(g36.manualLab, [27.82, -11.83, -0.17]);
  assert.deepEqual(PaintCatalog.RAL_BASE_RECIPES, {
    'RAL 1021': { Y74S: 85, W064: 15 },
    'RAL 3020': { R254D: 95, BK7H: 5 },
    'RAL 5005': { B150S: 80, W064: 20 },
    'RAL 7035': { W064: 90, BK7H: 10 },
    'RAL 9005': { BK7H: 100 },
    'RAL 9010': { W064: 100 }
  });
  assert.ok(Object.isFrozen(PaintCatalog.RAL_BASE_RECIPES));
  assert.ok(Object.isFrozen(PaintCatalog.RAL_BASE_RECIPES['RAL 1021']));
});

test('buildTargets preserves QTC target data and attaches only the exact six presets', () => {
  const targets = PaintCatalog.buildTargets(qtcCatalogue);
  assert.equal(targets.length, 216);
  assert.ok(Object.isFrozen(targets));
  assert.ok(targets.every(Object.isFrozen));
  assert.ok(targets.every(target => Object.isFrozen(target.rgb) && Object.isFrozen(target.targetLab)));
  assert.deepEqual(targets.filter(target => target.baseRecipe).map(target => target.ral), Object.keys(PaintCatalog.RAL_BASE_RECIPES));
  assert.deepEqual(targets.find(target => target.code === '1021').targetLab, qtcCatalogue.colors.find(target => target.code === '1021').targetLab);
  assert.throws(() => PaintCatalog.buildTargets({ count: 215, colors: [] }), /missing or incomplete/);
});
