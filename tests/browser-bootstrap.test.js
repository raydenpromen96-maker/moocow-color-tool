const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const MODULE_PATHS = Object.freeze([
  'src/qtc-ral-classic.js',
  'src/paint-catalog.js',
  'src/color-core.js',
  'src/recipe-search.js',
  'src/family-spectra.js',
  'src/production-runtime.js'
]);

function read(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', relativePath), 'utf8');
}

function loadBrowserContext() {
  let domReadyCallback = null;
  const context = {
    console,
    setTimeout,
    clearTimeout,
    document: {
      addEventListener(event, callback) {
        if (event === 'DOMContentLoaded') domReadyCallback = callback;
      }
    }
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  MODULE_PATHS.forEach(relativePath => vm.runInContext(read(relativePath), context, { filename: relativePath }));

  const inlineScripts = [...read('index.html').matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1]);
  const bootstrap = inlineScripts.find(source => source.includes('MooCowProductionRuntime.create'));
  assert.ok(bootstrap, 'application bootstrap script is missing');
  vm.runInContext(bootstrap, context, { filename: 'index.html:inline-bootstrap' });

  return { context, domReadyCallback };
}

test('browser UMD bootstrap parses and matches representative CommonJS outputs', { timeout: 180000 }, () => {
  const { context, domReadyCallback } = loadBrowserContext();
  assert.equal(typeof domReadyCallback, 'function');
  assert.equal(typeof context.MooCowColorCore.rgbToHex, 'function');

  const browserRuntime = context.MooCowProductionRuntime.create({
    ColorCore: context.MooCowColorCore,
    RecipeSearch: context.MooCowRecipeSearch,
    FamilySpectra: context.MooCowFamilySpectra,
    paintCatalog: context.MooCowPaintCatalog.PAINT_DATA
  });
  const browserTargets = new Map(context.MooCowPaintCatalog.buildTargets(context.MooCowRalClassic)
    .map(color => [color.code, color]));

  const ColorCore = require('../src/color-core.js');
  const RecipeSearch = require('../src/recipe-search.js');
  const FamilySpectra = require('../src/family-spectra.js');
  const PaintCatalog = require('../src/paint-catalog.js');
  const ProductionRuntime = require('../src/production-runtime.js');
  const qtcCatalogue = require('../src/qtc-ral-classic.js');
  const nodeRuntime = ProductionRuntime.create({
    ColorCore,
    RecipeSearch,
    FamilySpectra,
    paintCatalog: PaintCatalog.PAINT_DATA
  });
  const nodeTargets = new Map(PaintCatalog.buildTargets(qtcCatalogue).map(color => [color.code, color]));

  ['1021', '5005', '9010'].forEach(code => {
    assert.equal(
      JSON.stringify(browserRuntime.generateCandidates(browserTargets.get(code))),
      JSON.stringify(nodeRuntime.generateCandidates(nodeTargets.get(code))),
      `RAL ${code}`
    );
  });
});
