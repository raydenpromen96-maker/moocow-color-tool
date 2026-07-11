const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ColorCore = require('..');

const CIEDE2000_VECTORS = [
  [[50.0000, 2.6772, -79.7751], [50.0000, 0.0000, -82.7485], 2.0425],
  [[50.0000, 3.1571, -77.2803], [50.0000, 0.0000, -82.7485], 2.8615],
  [[50.0000, 2.8361, -74.0200], [50.0000, 0.0000, -82.7485], 3.4412],
  [[50.0000, -1.3802, -84.2814], [50.0000, 0.0000, -82.7485], 1.0000],
  [[50.0000, -1.1848, -84.8006], [50.0000, 0.0000, -82.7485], 1.0000],
  [[50.0000, -0.9009, -85.5211], [50.0000, 0.0000, -82.7485], 1.0000],
  [[50.0000, 0.0000, 0.0000], [50.0000, -1.0000, 2.0000], 2.3669]
];

function approximatelyEqual(actual, expected, tolerance = 1e-10) {
  assert.ok(Math.abs(actual - expected) <= tolerance, `expected ${actual} to be within ${tolerance} of ${expected}`);
}

test('exports the documented ColorCore API', () => {
  assert.deepEqual(Object.keys(ColorCore).sort(), [
    'activateRalPreset',
    'clampR',
    'deltaE2000',
    'deltaE76',
    'deriveModelColor',
    'getKS',
    'getRfromKS',
    'hexToLab',
    'hexToRgb',
    'labToRgb',
    'linearToSrgb',
    'resolveActiveRecipe',
    'rgbToHex',
    'rgbToLab',
    'srgbToLinear'
  ]);
  Object.values(ColorCore).forEach(value => assert.equal(typeof value, 'function'));
});

test('publishes the same API on window in a browser context', () => {
  const browserContext = { window: {} };
  const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'color-core.js'), 'utf8');

  vm.runInNewContext(source, browserContext);

  assert.deepEqual(Object.keys(browserContext.window.MooCowColorCore).sort(), Object.keys(ColorCore).sort());
});

test('matches published CIEDE2000 reference vectors', () => {
  CIEDE2000_VECTORS.forEach(([lab1, lab2, expected], index) => {
    approximatelyEqual(ColorCore.deltaE2000(lab1, lab2), expected, 5e-5);
  });
});

test('delta E functions are symmetric and have zero identity distance', () => {
  const lab1 = [36.2, -15.4, 42.8];
  const lab2 = [68.7, 31.1, -27.4];

  [ColorCore.deltaE76, ColorCore.deltaE2000].forEach(deltaE => {
    approximatelyEqual(deltaE(lab1, lab2), deltaE(lab2, lab1));
    approximatelyEqual(deltaE(lab1, lab1), 0);
  });
});

test('RGB-Lab-RGB preserves primary and neutral RGB values', () => {
  [
    [255, 0, 0],
    [0, 255, 0],
    [0, 0, 255],
    [0, 0, 0],
    [128, 128, 128],
    [255, 255, 255]
  ].forEach(rgb => {
    assert.deepEqual(ColorCore.labToRgb(...ColorCore.rgbToLab(...rgb)), rgb);
  });
});

test('K/S conversion round-trips reflectance across its supported domain', () => {
  [0.001, 0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 0.999].forEach(reflectance => {
    approximatelyEqual(ColorCore.getRfromKS(ColorCore.getKS(reflectance)), reflectance, 1e-12);
  });
});

test('color calculations are deterministic across repeated calls', () => {
  const inputs = ['#123ABC', '#FFCC00', '#808080'];
  const expected = inputs.map(hex => ({
    rgb: ColorCore.hexToRgb(hex),
    lab: ColorCore.hexToLab(hex),
    hex: ColorCore.rgbToHex(ColorCore.labToRgb(...ColorCore.hexToLab(hex)))
  }));

  for (let i = 0; i < 100; i += 1) {
    assert.deepEqual(inputs.map(hex => ({
      rgb: ColorCore.hexToRgb(hex),
      lab: ColorCore.hexToLab(hex),
      hex: ColorCore.rgbToHex(ColorCore.labToRgb(...ColorCore.hexToLab(hex)))
    })), expected);
  }
});

test('manualLab takes precedence over display HEX for model input', () => {
  const manualLab = [57.18, 49.67, 13.26];
  const derived = ColorCore.deriveModelColor({ hex: '#D8001D', manualLab });

  assert.equal(derived.modelLabSource, 'manualLab');
  assert.deepEqual(derived.modelLab, manualLab);
  assert.deepEqual(derived.modelRgb, ColorCore.labToRgb(...manualLab));
  assert.ok(derived.displayConflictDE > 10);
});

test('display HEX is used only as a documented fallback when manualLab is absent', () => {
  const derived = ColorCore.deriveModelColor({ hex: '#123ABC' });

  assert.equal(derived.modelLabSource, 'hexFallback');
  assert.deepEqual(derived.modelLab, ColorCore.hexToLab('#123ABC'));
  assert.deepEqual(derived.modelRgb, ColorCore.hexToRgb('#123ABC'));
  approximatelyEqual(derived.displayConflictDE, 0);
});

test('reloading the same RAL preset clears a generated recipe before volume scaling', () => {
  const preset = { ral: 'RAL 1021', baseRecipe: { Y74S: 85, W064: 15 } };
  const generated = { Y83S: 40.71, Y74S: 50.23, W064: 14.86 };
  const state = { ral: null, generatedRecipe: null };

  ColorCore.activateRalPreset(state, preset);
  state.generatedRecipe = generated;
  assert.equal(ColorCore.resolveActiveRecipe(state), generated);

  ColorCore.activateRalPreset(state, preset);
  assert.equal(state.generatedRecipe, null);
  assert.equal(ColorCore.resolveActiveRecipe(state), preset.baseRecipe);
  assert.deepEqual(
    Object.fromEntries(Object.entries(ColorCore.resolveActiveRecipe(state)).map(([code, percent]) => [code, (percent / 100) * 106 * 5])),
    { Y74S: 450.5, W064: 79.5 }
  );
});

test('active recipe resolution is deterministic and does not mutate presets', () => {
  const presetRecipe = Object.freeze({ Y74S: 85, W064: 15 });
  const preset = Object.freeze({ ral: 'RAL 1021', baseRecipe: presetRecipe });
  const state = { ral: null, generatedRecipe: null };

  ColorCore.activateRalPreset(state, preset);
  const snapshots = Array.from({ length: 100 }, () => JSON.stringify(ColorCore.resolveActiveRecipe(state)));

  assert.equal(new Set(snapshots).size, 1);
  assert.deepEqual(preset.baseRecipe, { Y74S: 85, W064: 15 });
});
