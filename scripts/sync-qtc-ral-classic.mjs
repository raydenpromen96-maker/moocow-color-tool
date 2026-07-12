import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIRECTORY_URL = 'https://m.qtccolor.com/webapi/color/GetDirJSONByArticile?id=70';
const DETAIL_URL = id => `https://m.qtccolor.com/webapi/color/getColor?colorId=${id}&isUpdateVol=0`;
const PAGE_URL = 'https://m.qtccolor.com/mshop/#/pages/color/dir?articleId=70';
const EXPECTED_COUNT = 216;
const CONCURRENCY = 8;
const EXPECTED_CODE_SET_SHA256 = '50cbb1bd23b9510fa5c8d7d561717166c0a87b7439b93f7b8de5d12ccae688fa';

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

async function getJson(url) {
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json',
      Referer: 'https://m.qtccolor.com/mshop/',
      'User-Agent': 'MOOCOW-QTC-RAL-Snapshot/1.0'
    }
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}: ${url}`);
  const raw = await response.text();
  return { payload: JSON.parse(raw), responseSha256: sha256(raw) };
}

function assertFiniteTriplet(value, label) {
  if (!Array.isArray(value) || value.length !== 3 || !value.every(Number.isFinite)) {
    throw new Error(`${label} must be a finite 3-value array`);
  }
}

function normalizeDetail(directoryItem, payload) {
  const color = payload?.data?.color;
  const item = color?.ColorlibraryItems?.[0];
  if (!payload?.success || !item) throw new Error(`Missing detail for ${directoryItem.ColorCode}`);
  if (String(item.Code) !== String(directoryItem.ColorCode)) {
    throw new Error(`Code mismatch: directory ${directoryItem.ColorCode}, detail ${item.Code}`);
  }

  const rgb = [item.RGB_R, item.RGB_G, item.RGB_B].map(Number);
  const targetLab = [item.LAB_L, item.LAB_A, item.LAB_B].map(Number);
  assertFiniteTriplet(rgb, `${item.Code} RGB`);
  assertFiniteTriplet(targetLab, `${item.Code} Lab`);
  if (!rgb.every(value => Number.isInteger(value) && value >= 0 && value <= 255)) {
    throw new Error(`${item.Code} has invalid RGB values`);
  }

  const rgbHex = `#${rgb.map(value => value.toString(16).padStart(2, '0')).join('')}`.toUpperCase();
  const hex = String(item.Hex || '').toUpperCase();
  if (!/^#[0-9A-F]{6}$/.test(hex) || hex !== rgbHex) {
    throw new Error(`${item.Code} HEX/RGB mismatch: ${hex} vs ${rgbHex}`);
  }

  return {
    ral: `RAL ${item.Code}`,
    code: String(item.Code),
    name_en: String(item.EnglishName || '').trim(),
    name_zh: String(item.Alias || directoryItem.ColorName || '').trim(),
    hex,
    rgb,
    targetLab,
    qtcColorId: Number(item.ColorId || item.Id || directoryItem.Id),
    qtcIndex: Number(directoryItem.Index)
  };
}

async function mapConcurrent(items, worker) {
  const results = new Array(items.length);
  let nextIndex = 0;
  await Promise.all(Array.from({ length: CONCURRENCY }, async () => {
    while (nextIndex < items.length) {
      const index = nextIndex++;
      results[index] = await worker(items[index], index);
    }
  }));
  return results;
}

const directoryResponse = await getJson(DIRECTORY_URL);
const directoryPayload = directoryResponse.payload;
const directory = JSON.parse(directoryPayload?.data?.colorJSON || '{}');
const directoryItems = directory.List || [];
if (directoryItems.length !== EXPECTED_COUNT) {
  throw new Error(`Expected ${EXPECTED_COUNT} directory records, received ${directoryItems.length}`);
}

const detailReceipts = new Array(directoryItems.length);
const colors = await mapConcurrent(directoryItems, async (item, index) => {
  const detailResponse = await getJson(DETAIL_URL(item.Id));
  detailReceipts[index] = { colorId: Number(item.Id), responseSha256: detailResponse.responseSha256 };
  return normalizeDetail(item, detailResponse.payload);
});

const codes = new Set(colors.map(color => color.code));
if (codes.size !== EXPECTED_COUNT) throw new Error('Duplicate QTC colour codes detected');
const codeSetSha256 = sha256(colors.map(color => color.code).join('\n'));
if (codeSetSha256 !== EXPECTED_CODE_SET_SHA256) {
  throw new Error(`QTC code-set drift detected: ${codeSetSha256}`);
}
if (colors[0]?.code !== '1000' || colors.at(-1)?.code !== '9023') {
  throw new Error('Unexpected QTC catalogue order or boundary codes');
}

const snapshot = {
  schemaVersion: 1,
  source: {
    provider: 'QTC Color',
    catalogue: 'RAL Classic',
    pageUrl: PAGE_URL,
    directoryUrl: DIRECTORY_URL,
    detailUrlTemplate: 'https://m.qtccolor.com/webapi/color/getColor?colorId={id}&isUpdateVol=0',
    retrievedAt: new Date().toISOString(),
    valueType: 'computer-simulated screen reference',
    disclaimer: 'Displayed colours and values are computer simulations. Confirm production work against a current physical colour card.',
    authorizationBasis: 'User-reported telephone confirmation from the RAL Asia-Pacific business manager; QTC identified by the user as an authorised presentation source.',
    directoryResponseSha256: directoryResponse.responseSha256,
    detailResponsesSha256: sha256(JSON.stringify(detailReceipts)),
    approvedCodeSetSha256: EXPECTED_CODE_SET_SHA256
  },
  count: colors.length,
  colors
};

const canonicalColors = JSON.stringify(colors);
snapshot.colorsSha256 = sha256(canonicalColors);

await mkdir(path.join(ROOT, 'data'), { recursive: true });
await mkdir(path.join(ROOT, 'src'), { recursive: true });

const jsonOutput = `${JSON.stringify(snapshot, null, 2)}\n`;
const jsOutput = `(function (root, factory) {\n` +
  `  const value = factory();\n` +
  `  if (typeof module === 'object' && module.exports) module.exports = value;\n` +
  `  if (root) root.MooCowRalClassic = value;\n` +
  `})(typeof globalThis !== 'undefined' ? globalThis : this, function () {\n` +
  `  return ${JSON.stringify(snapshot, null, 2)};\n` +
  `});\n`;

await writeFile(path.join(ROOT, 'data', 'qtc-ral-classic.json'), jsonOutput, 'utf8');
await writeFile(path.join(ROOT, 'src', 'qtc-ral-classic.js'), jsOutput, 'utf8');

console.log(`Saved ${colors.length} QTC RAL Classic colours`);
console.log(`SHA-256 ${snapshot.colorsSha256}`);
