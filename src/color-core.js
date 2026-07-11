(function (root, factory) {
  const colorCore = factory();

  if (typeof module === 'object' && module.exports) {
    module.exports = colorCore;
  }

  if (root) {
    root.MooCowColorCore = colorCore;
  }
}(typeof window !== 'undefined' ? window : typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function hexToRgb(h) {
    const r = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(h);
    return r ? [parseInt(r[1], 16), parseInt(r[2], 16), parseInt(r[3], 16)] : null;
  }

  function rgbToHex(rgb) {
    return `#${rgb.map(c => Math.max(0, Math.min(255, Math.round(c))).toString(16).padStart(2, '0').toUpperCase()).join('')}`;
  }

  function srgbToLinear(c) {
    return c > 0.04045 ? Math.pow((c + 0.055) / 1.055, 2.4) : c / 12.92;
  }

  function linearToSrgb(c) {
    return c > 0.0031308 ? 1.055 * Math.pow(c, 1 / 2.4) - 0.055 : 12.92 * c;
  }

  function rgbToLab(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    r = r > 0.04045 ? Math.pow((r + 0.055) / 1.055, 2.4) : r / 12.92;
    g = g > 0.04045 ? Math.pow((g + 0.055) / 1.055, 2.4) : g / 12.92;
    b = b > 0.04045 ? Math.pow((b + 0.055) / 1.055, 2.4) : b / 12.92;
    let x = (r * 0.4124 + g * 0.3576 + b * 0.1805) * 100;
    let y = (r * 0.2126 + g * 0.7152 + b * 0.0722) * 100;
    let z = (r * 0.0193 + g * 0.1192 + b * 0.9505) * 100;
    x /= 95.047; y /= 100.0; z /= 108.883;
    x = x > 0.008856 ? Math.pow(x, 1 / 3) : (7.787 * x) + (16 / 116);
    y = y > 0.008856 ? Math.pow(y, 1 / 3) : (7.787 * y) + (16 / 116);
    z = z > 0.008856 ? Math.pow(z, 1 / 3) : (7.787 * z) + (16 / 116);
    return [(116 * y) - 16, 500 * (x - y), 200 * (y - z)];
  }

  function hexToLab(h) {
    const rgb = hexToRgb(h);
    return rgb ? rgbToLab(...rgb) : [0, 0, 0];
  }

  function labToRgb(L, a, b) {
    let y = (L + 16) / 116;
    let x = a / 500 + y;
    let z = y - b / 200;
    const pivot = v => {
      const v3 = v * v * v;
      return v3 > 0.008856 ? v3 : (v - 16 / 116) / 7.787;
    };
    x = 95.047 * pivot(x);
    y = 100.0 * pivot(y);
    z = 108.883 * pivot(z);
    x /= 100; y /= 100; z /= 100;
    const r = x * 3.2406 + y * -1.5372 + z * -0.4986;
    const g = x * -0.9689 + y * 1.8758 + z * 0.0415;
    const bl = x * 0.0557 + y * -0.2040 + z * 1.0570;
    const compand = v => {
      const c = v > 0.0031308 ? 1.055 * Math.pow(v, 1 / 2.4) - 0.055 : 12.92 * v;
      return Math.max(0, Math.min(255, Math.round(c * 255)));
    };
    return [compand(r), compand(g), compand(bl)];
  }

  function deltaE76(lab1, lab2) {
    const dL = lab1[0] - lab2[0], da = lab1[1] - lab2[1], db = lab1[2] - lab2[2];
    return Math.sqrt(dL * dL + da * da + db * db);
  }

  function deltaE2000(lab1, lab2) {
    const [L1, a1, b1] = lab1, [L2, a2, b2] = lab2;
    const deg2rad = Math.PI / 180, rad2deg = 180 / Math.PI;
    const C1 = Math.sqrt(a1 * a1 + b1 * b1), C2 = Math.sqrt(a2 * a2 + b2 * b2);
    const avgC = (C1 + C2) / 2;
    const G = 0.5 * (1 - Math.sqrt(Math.pow(avgC, 7) / (Math.pow(avgC, 7) + Math.pow(25, 7))));
    const a1p = (1 + G) * a1, a2p = (1 + G) * a2;
    const C1p = Math.sqrt(a1p * a1p + b1 * b1), C2p = Math.sqrt(a2p * a2p + b2 * b2);
    const h = (bb, aa) => {
      if (bb === 0 && aa === 0) return 0;
      const v = Math.atan2(bb, aa) * rad2deg;
      return v >= 0 ? v : v + 360;
    };
    const h1p = h(b1, a1p), h2p = h(b2, a2p);
    const dLp = L2 - L1, dCp = C2p - C1p;
    let dhp = 0;
    if (C1p * C2p !== 0) {
      const diff = h2p - h1p;
      if (Math.abs(diff) <= 180) dhp = diff;
      else dhp = diff > 180 ? diff - 360 : diff + 360;
    }
    const dHp = 2 * Math.sqrt(C1p * C2p) * Math.sin((dhp / 2) * deg2rad);
    const avgLp = (L1 + L2) / 2, avgCp = (C1p + C2p) / 2;
    let avghp = h1p + h2p;
    if (C1p * C2p === 0) avghp = h1p + h2p;
    else if (Math.abs(h1p - h2p) <= 180) avghp = (h1p + h2p) / 2;
    else avghp = (h1p + h2p + (h1p + h2p < 360 ? 360 : -360)) / 2;
    const T = 1 - 0.17 * Math.cos((avghp - 30) * deg2rad) + 0.24 * Math.cos(2 * avghp * deg2rad) + 0.32 * Math.cos((3 * avghp + 6) * deg2rad) - 0.20 * Math.cos((4 * avghp - 63) * deg2rad);
    const deltaTheta = 30 * Math.exp(-Math.pow((avghp - 275) / 25, 2));
    const Rc = 2 * Math.sqrt(Math.pow(avgCp, 7) / (Math.pow(avgCp, 7) + Math.pow(25, 7)));
    const Sl = 1 + (0.015 * Math.pow(avgLp - 50, 2)) / Math.sqrt(20 + Math.pow(avgLp - 50, 2));
    const Sc = 1 + 0.045 * avgCp;
    const Sh = 1 + 0.015 * avgCp * T;
    const Rt = -Math.sin(2 * deltaTheta * deg2rad) * Rc;
    return Math.sqrt(
      Math.pow(dLp / Sl, 2) +
      Math.pow(dCp / Sc, 2) +
      Math.pow(dHp / Sh, 2) +
      Rt * (dCp / Sc) * (dHp / Sh)
    );
  }

  function clampR(r) {
    return Math.max(0.001, Math.min(0.999, r));
  }

  function getKS(r) {
    const rc = clampR(r);
    return Math.pow(1 - rc, 2) / (2 * rc);
  }

  function getRfromKS(ks) {
    return 1 + ks - Math.sqrt(Math.pow(ks, 2) + 2 * ks);
  }

  function deriveModelColor(input) {
    const displayRgb = hexToRgb(input?.hex || '');
    const hasManualLab = Array.isArray(input?.manualLab)
      && input.manualLab.length === 3
      && input.manualLab.every(Number.isFinite);
    const modelLab = hasManualLab
      ? input.manualLab.map(Number)
      : displayRgb
        ? rgbToLab(...displayRgb)
        : [0, 0, 0];
    const modelRgb = labToRgb(...modelLab);

    return {
      displayRgb,
      modelLab,
      modelRgb,
      modelLabSource: hasManualLab ? 'manualLab' : 'hexFallback',
      displayConflictDE: displayRgb ? deltaE2000(modelLab, rgbToLab(...displayRgb)) : 0
    };
  }

  function activateRalPreset(state, ral) {
    if (!state || typeof state !== 'object') throw new TypeError('state must be an object');
    state.ral = ral || null;
    state.generatedRecipe = null;
    return state.ral?.baseRecipe || null;
  }

  function resolveActiveRecipe(state) {
    if (!state || typeof state !== 'object') return null;
    return state.generatedRecipe || state.ral?.baseRecipe || null;
  }

  return {
    hexToRgb,
    rgbToHex,
    srgbToLinear,
    linearToSrgb,
    rgbToLab,
    hexToLab,
    labToRgb,
    deltaE76,
    deltaE2000,
    clampR,
    getKS,
    getRfromKS,
    deriveModelColor,
    activateRalPreset,
    resolveActiveRecipe
  };
}));
