const assert = require('node:assert/strict');
const test = require('node:test');

const PaintCatalog = require('../src/paint-catalog.js');
const qtcCatalogue = require('../src/qtc-ral-classic.js');

test('paint catalog exports immutable raw pigment data and the six exact presets', () => {
  assert.deepEqual(Object.keys(PaintCatalog).sort(), ['PAINT_DATA', 'RAL_BASE_RECIPES', 'buildTargets']);
  assert.equal(Object.keys(PaintCatalog.PAINT_DATA).length, 14);
  assert.ok(Object.isFrozen(PaintCatalog.PAINT_DATA));
  assert.ok(Object.isFrozen(PaintCatalog.PAINT_DATA.R254D));
  assert.ok(Object.isFrozen(PaintCatalog.PAINT_DATA.R254D.aliases));
  assert.ok(Object.isFrozen(PaintCatalog.PAINT_DATA.Y83S.manualLab));
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
