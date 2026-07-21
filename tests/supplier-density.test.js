const assert = require('node:assert/strict');
const test = require('node:test');

const ColorCore = require('../src/color-core.js');
const RecipeSearch = require('../src/recipe-search.js');
const FamilySpectra = require('../src/family-spectra.js');
const PaintCatalog = require('../src/paint-catalog.js');
const ProductionRuntime = require('../src/production-runtime.js');
const supplierRecord = require('../data/supplier/colanyl-wet-density-2026-07-17.json');

function createRuntime(paintCatalog = PaintCatalog.PAINT_DATA) {
  return ProductionRuntime.create({
    ColorCore,
    RecipeSearch,
    FamilySpectra,
    paintCatalog
  });
}

test('supplier wet-density record covers the complete purchased catalog with explicit provenance', () => {
  assert.equal(supplierRecord.schema_version, 'moocow-supplier-wet-density-v1');
  assert.equal(supplierRecord.source.source_sha256, 'ABCEB5AACB97E2C9287180AA27F60D7CC233C27A229034E1E914BB892C11905C');
  assert.equal(supplierRecord.source.reported_unit, null);
  assert.equal(supplierRecord.source.operational_unit, 'g/mL');
  // 供应商记录覆盖 14 支已采购色浆；第 15 支 G36 待采购，密度为标注估计值，不在记录内。
  assert.equal(supplierRecord.colorants.length, 14);

  const purchasedCatalog = Object.fromEntries(
    Object.entries(PaintCatalog.PAINT_DATA).filter(([, pigment]) => pigment.purchaseStatus !== 'pending_purchase')
  );
  assert.deepEqual(Object.keys(purchasedCatalog).sort(), Object.keys(PaintCatalog.PAINT_DATA).filter(code => code !== 'G36').sort());
  const recordByCode = Object.fromEntries(supplierRecord.colorants.map(item => [item.catalog_code, item]));
  assert.deepEqual(Object.keys(recordByCode).sort(), Object.keys(purchasedCatalog).sort());
  Object.entries(purchasedCatalog).forEach(([code, pigment]) => {
    const record = recordByCode[code];
    assert.equal(pigment.productName, record.product_name, `${code} product name`);
    assert.equal(pigment.density, record.wet_density_g_ml, `${code} wet density`);
    assert.equal(pigment.densityUnit, 'g/mL', `${code} density unit`);
    assert.equal(pigment.densityUnitStatus, 'assumed_from_unlabeled_supplier_sheet', `${code} unit status`);
    assert.equal(pigment.densityBasis, 'wet_product', `${code} density basis`);
    assert.equal(pigment.densitySource, supplierRecord.record_id, `${code} density source`);
  });
  // G36（待采购）密度为估计值，必须显式标注，不得冒充实测/供应商数据。
  const g36 = PaintCatalog.PAINT_DATA.G36;
  assert.equal(g36.density, 1.35);
  assert.equal(g36.densityUnitStatus, 'estimated_not_measured');
  assert.equal(g36.densitySource, 'estimate-pending-purchase');
});

test('supplier-confirmed DPP red and orange identities with declared measured-proxy spectra', () => {
  const red = PaintCatalog.PAINT_DATA.R254D;
  const orange = PaintCatalog.PAINT_DATA['073'];
  assert.equal(red.ci, 'PR254');
  assert.deepEqual(red.aliases, ['R524D']);
  assert.equal(red.identitySource, 'supplier-confirmation-20260717');
  assert.equal(orange.ci, 'PO73');
  assert.equal(orange.ciSupplierNumber, '561170');
  assert.equal(orange.identitySource, 'supplier-confirmation-20260717');
  // v5: PO73 光谱不再是空白——来自 CHSOS Pigments Checker 实测（丙烯粘合剂），
  // 以代理身份显式标注，不是编造曲线，也不代表当前 CN 批次。
  assert.equal(FamilySpectra.PROFILES.PO73.status, 'chsos_measured_acrylic_proxy_reference');
  assert.equal(FamilySpectra.PROFILES.PO73.sourceId, FamilySpectra.CHSOS_SOURCE.id);
  assert.equal(PaintCatalog.PAINT_DATA.R101V.densityMappingStatus, 'provisional_brand_mismatch');
  assert.equal(PaintCatalog.PAINT_DATA.Y42S.densityMappingStatus, 'provisional_brand_mismatch');
});

test('wet mass and wet volume conversions use supplier density and preserve aliases', () => {
  const runtime = createRuntime();
  assert.ok(Math.abs(runtime.wetMassToVolumeMl('073', 10.7) - 10) < 1e-12);
  assert.ok(Math.abs(runtime.wetVolumeToMassG('W064', 10) - 18.3) < 1e-12);
  assert.ok(Math.abs(runtime.wetMassToVolumeMl('R524D', 11.9) - 10) < 1e-12);
  assert.ok(Math.abs(runtime.recipeWetMassToVolumeMl({ '073': 10.7, W064: 18.3 }) - 20) < 1e-12);
  assert.equal(runtime.recipeWetMassToVolumeMl({}), 0);
  assert.throws(() => runtime.wetMassToVolumeMl('UNKNOWN', 1), /Unknown paint code/);
  assert.throws(() => runtime.wetMassToVolumeMl('073', -1), /non-negative/);
  assert.throws(() => runtime.wetVolumeToMassG('073', Number.NaN), /non-negative/);
  assert.throws(() => runtime.recipeWetMassToVolumeMl([]), /must be an object/);
});

test('safe wet-volume helpers degrade to null when density metadata is unavailable', () => {
  const incompleteCatalog = JSON.parse(JSON.stringify(PaintCatalog.PAINT_DATA));
  delete incompleteCatalog['073'].density;
  const runtime = createRuntime(incompleteCatalog);
  assert.equal(runtime.wetMassToVolumeMlOrNull('073', 10.7), null);
  assert.equal(runtime.recipeWetMassToVolumeMlOrNull({ '073': 10.7 }), null);
  assert.equal(runtime.wetMassToVolumeMlOrNull('UNKNOWN', 1), null);
  assert.equal(runtime.wetMassToVolumeMlOrNull('W064', -1), null);
});
