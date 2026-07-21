// 一次性推导脚本：算出要硬编码进 src/family-spectra.js 的 11 点光谱数值。
// 重采样逻辑与 experiments/whatif-real-spectra.mjs 的 loadChsos 完全一致。
import { readFile } from 'node:fs/promises';
import FamilySpectra from '../src/family-spectra.js';

const WAVELENGTHS = [400, 430, 460, 490, 520, 550, 580, 610, 640, 670, 700];
const D65 = [82.7549, 86.6823, 117.812, 108.811, 104.79, 104.046, 95.788, 89.5991, 83.6992, 82.2778, 71.6091];
const CMF = {
  x: [0.01431, 0.2839, 0.2908, 0.03201, 0.06327, 0.4334499, 0.9163, 1.0026, 0.4479, 0.0874, 0.01135916],
  y: [0.000396, 0.0116, 0.06, 0.20802, 0.71, 0.9949501, 0.87, 0.503, 0.175, 0.032, 0.004102],
  z: [0.06785001, 1.3856, 1.6692, 0.46518, 0.07824999, 0.00875, 0.00165, 0.00034, 0.00002, 0, 0]
};
const getKS = r => { const rc = Math.max(0.001, Math.min(0.999, r)); return (1 - rc) ** 2 / (2 * rc); };

const SPECTRA_DIR = new URL('../../spectral_data/pigments/', import.meta.url);
async function loadChsos(file) {
  const text = await readFile(new URL(file, SPECTRA_DIR), 'utf8');
  const pts = [];
  for (const line of text.split(/\r?\n/).slice(1)) {
    const [wl, r] = line.split(',').map(Number);
    if (Number.isFinite(wl) && Number.isFinite(r)) pts.push([wl, Math.max(0.001, Math.min(0.999, r / 100))]);
  }
  pts.sort((a, b) => a[0] - b[0]);
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

function fitPG36Shift(pg7) {
  const targetLab = [27.82, -11.83, -0.17];
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

const files = {
  PY74: 'PY74_arylide_yellow_5GX.csv',
  'PB15:1': 'PB15_phthalo_blue.csv',
  PO73: 'PO73_pyrrole_orange.csv',
  PW6: 'PW6_titanium_white.csv'
};
const round = arr => arr.map(v => Number(v.toFixed(4)));
for (const [ci, file] of Object.entries(files)) {
  const spec = await loadChsos(file);
  console.log(`${ci}  (${file})`);
  console.log(`  reflectance: [${round(spec).join(', ')}]`);
  console.log(`  kOverS:      [${round(spec.map(getKS)).join(', ')}]`);
  console.log(`  masstoneLab: (${spectrumToLab(spec).map(v => v.toFixed(2)).join(', ')})`);
}

const pg7 = FamilySpectra.PROFILES.PG7.reflectance;
const fit = fitPG36Shift(pg7);
console.log(`PG36 fit: shift=${fit.shift}nm gain=${fit.gain} err=${fit.err.toFixed(3)} lab=(${fit.lab.map(v => v.toFixed(2)).join(', ')})`);
console.log(`  reflectance: [${round(fit.spec).join(', ')}]`);
console.log(`  kOverS:      [${round(fit.spec.map(getKS)).join(', ')}]`);

// G36 显示用 hex：由 PG36 拟合光谱经 D65/CIE1931 -> XYZ -> sRGB
function spectrumToRgb(reflectance) {
  let X = 0, Y = 0, Z = 0, wX = 0, wY = 0, wZ = 0;
  reflectance.forEach((r, i) => {
    X += r * D65[i] * CMF.x[i]; Y += r * D65[i] * CMF.y[i]; Z += r * D65[i] * CMF.z[i];
    wX += D65[i] * CMF.x[i]; wY += D65[i] * CMF.y[i]; wZ += D65[i] * CMF.z[i];
  });
  X = X / wX * 95.047 / 100; Y = Y / wY; Z = Z / wZ * 108.883 / 100;
  const r = X * 3.2406 + Y * -1.5372 + Z * -0.4986;
  const g = X * -0.9689 + Y * 1.8758 + Z * 0.0415;
  const b = X * 0.0557 + Y * -0.2040 + Z * 1.0570;
  const enc = v => { const c = v > 0.0031308 ? 1.055 * Math.pow(Math.max(0, v), 1 / 2.4) - 0.055 : 12.92 * Math.max(0, v); return Math.max(0, Math.min(255, Math.round(c * 255))); };
  return [enc(r), enc(g), enc(b)];
}
const rgb = spectrumToRgb(fit.spec);
console.log(`G36 display hex from fitted spectrum: #${rgb.map(v => v.toString(16).padStart(2, '0')).join('')}`);
