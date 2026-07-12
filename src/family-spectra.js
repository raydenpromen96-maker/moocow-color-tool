(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.MooCowFamilySpectra = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const WAVELENGTHS = Object.freeze([400, 430, 460, 490, 520, 550, 580, 610, 640, 670, 700]);

  const SOURCE = Object.freeze({
    id: 'golden-heavy-body-waterborne-acrylic',
    title: 'Reflectance Data for Golden HB 10 mil Drawdowns over White',
    provider: 'Golden Artist Colors, Inc.',
    sharedBy: 'Andrew Glassner and Eric Haines',
    page: 'https://www.realtimerendering.com/golden.html',
    permission: 'The public data page states that Golden allowed the hosts to share these acrylic-paint spectral data with others; no named data licence is published.',
    sourceZipSha256: 'AF3F8B0C327DD4DCFF52BAC83A5ED7E9C80D82D2E56164518DDF1C9AA57D3835',
    spreadsheetSha256: '584A38368C4AF637A1253B6465B9F71493E38C65340092A0CFE9F73B3ED227CF',
    matrix: 'water_based_heavy_body_acrylic',
    units: Object.freeze({ reflectance: 'fraction', kOverS: 'dimensionless single-constant K/S' }),
    measurement: '10 mil wet drawdown, approximately 6 mil dry, over white Leneta card; D65; 10-degree observer; 400-700 nm at 10 nm intervals',
    limitations: 'The white card influences transparent colors, the films are not all truly opaque, and these are Golden products rather than the current Clariant/Heubach CN batches.',
    usage: 'waterborne_acrylic_shadow_reference_only'
  });

  function profile(ci, productNumber, productName, productUrl, reflectance, kOverS) {
    return Object.freeze({
      ci,
      productNumber,
      productName,
      productUrl,
      status: 'exact_ci_waterborne_acrylic_reference',
      sourceId: SOURCE.id,
      wavelengths: WAVELENGTHS,
      reflectance: Object.freeze(reflectance),
      kOverS: Object.freeze(kOverS)
    });
  }

  const PROFILES = Object.freeze({
    PBk7: profile('PBk7', 1040, 'Carbon Black', 'https://goldenartistcolors.com/products/heavy-body-acrylic-color-carbon-black',
      [0.0464, 0.046, 0.0461, 0.0462, 0.0462, 0.0462, 0.0463, 0.0466, 0.0472, 0.0476, 0.0479],
      [9.7991, 9.8926, 9.869, 9.8456, 9.8456, 9.8456, 9.8223, 9.7529, 9.6168, 9.528, 9.4624]),
    PY83: profile('PY83', 1147, 'Diarylide Yellow', 'https://goldenartistcolors.com/products/heavy-body-acrylic-color-diarylide-yellow',
      [0.0631, 0.0583, 0.0544, 0.0507, 0.0853, 0.5531, 0.8378, 0.875, 0.8925, 0.907, 0.9102],
      [6.9555, 7.6055, 8.2184, 8.8873, 4.9043, 0.1805, 0.0157, 0.0089, 0.0065, 0.0048, 0.0044]),
    PV23: profile('PV23', 1150, 'Dioxazine Purple', 'https://goldenartistcolors.com/products/heavy-body-acrylic-color-dioxazine-purple',
      [0.046, 0.0441, 0.0424, 0.042, 0.042, 0.0439, 0.0478, 0.0462, 0.0495, 0.0692, 0.1269],
      [9.8926, 10.3599, 10.8137, 10.9258, 10.9258, 10.4115, 9.4842, 9.8456, 9.1258, 6.26, 3.0036]),
    'PB15:3': profile('PB15:3', 1255, 'Phthalo Blue (Green Shade)', 'https://goldenartistcolors.com/products/heavy-body-acrylic-color-phthalo-blue-green-shade',
      [0.0744, 0.0834, 0.1069, 0.0581, 0.0396, 0.0379, 0.0385, 0.0425, 0.0475, 0.048, 0.0491],
      [5.7576, 5.0369, 3.7307, 7.6349, 11.6461, 12.2116, 12.0063, 10.786, 9.5501, 9.4407, 9.2078]),
    PG7: profile('PG7', 1270, 'Phthalo Green (Blue Shade)', 'https://goldenartistcolors.com/products/heavy-body-acrylic-color-phthalo-green-blue-shade',
      [0.0511, 0.0527, 0.0596, 0.0881, 0.0595, 0.0406, 0.0383, 0.0405, 0.0449, 0.0497, 0.0501],
      [8.8103, 8.514, 7.4191, 4.7194, 7.4331, 11.3356, 12.074, 11.3659, 10.1583, 9.0852, 9.0051]),
    PR254: profile('PR254', 1277, 'Pyrrole Red', 'https://goldenartistcolors.com/products/heavy-body-acrylic-color-pyrrole-red',
      [0.0414, 0.0405, 0.0415, 0.0415, 0.0416, 0.0427, 0.0492, 0.452, 0.7572, 0.8384, 0.8786],
      [11.098, 11.3659, 11.0689, 11.0689, 11.04, 10.731, 9.1872, 0.3322, 0.0389, 0.0156, 0.0084]),
    PR122: profile('PR122', 1305, 'Quinacridone Magenta', 'https://goldenartistcolors.com/products/heavy-body-acrylic-color-quinacridone-magenta',
      [0.0811, 0.0685, 0.0523, 0.0455, 0.0433, 0.0452, 0.051, 0.0936, 0.332, 0.5258, 0.6066],
      [5.2058, 6.3335, 8.5864, 10.0118, 10.569, 10.0845, 8.8294, 4.3887, 0.672, 0.2138, 0.1276]),
    PR101: profile('PR101', 1360, 'Red Oxide', 'https://goldenartistcolors.com/products/heavy-body-acrylic-color-red-oxide',
      [0.0476, 0.048, 0.048, 0.0489, 0.0507, 0.0586, 0.1323, 0.2508, 0.2882, 0.3217, 0.3728],
      [9.528, 9.4407, 9.4407, 9.2494, 8.8873, 7.5617, 2.8454, 1.119, 0.879, 0.7151, 0.5276]),
    PY42: profile('PY42', 1410, 'Yellow Oxide', 'https://goldenartistcolors.com/products/heavy-body-acrylic-color-yellow-oxide',
      [0.0597, 0.0723, 0.0952, 0.1078, 0.1671, 0.3321, 0.4677, 0.4505, 0.4241, 0.424, 0.4498],
      [7.4051, 5.9518, 4.2997, 3.6921, 2.0758, 0.6716, 0.3029, 0.3351, 0.391, 0.3912, 0.3365])
  });

  function validateProfile(value) {
    const errors = [];
    if (!value || typeof value !== 'object') return { valid: false, errors: ['profile_missing'] };
    if (value.status !== 'exact_ci_waterborne_acrylic_reference') errors.push('invalid_status');
    for (const key of ['wavelengths', 'reflectance', 'kOverS']) {
      if (!Array.isArray(value[key]) || value[key].length !== WAVELENGTHS.length) errors.push(`${key}_length`);
    }
    if (Array.isArray(value.wavelengths) && value.wavelengths.some((v, i) => v !== WAVELENGTHS[i])) errors.push('wavelength_grid');
    if (Array.isArray(value.reflectance) && value.reflectance.some(v => !Number.isFinite(v) || v < 0 || v > 1)) errors.push('invalid_reflectance');
    if (Array.isArray(value.kOverS) && value.kOverS.some(v => !Number.isFinite(v) || v < 0)) errors.push('invalid_k_over_s');
    return { valid: errors.length === 0, errors };
  }

  function summarizeCoverage(entries) {
    const normalized = (Array.isArray(entries) ? entries : [])
      .map(item => ({ ci: item?.ci || null, fraction: Math.max(0, Number(item?.fraction) || 0) }))
      .filter(item => item.fraction > 0);
    const total = normalized.reduce((sum, item) => sum + item.fraction, 0);
    let exact = 0;
    const missingCi = new Set();
    normalized.forEach(item => {
      const weight = total > 0 ? item.fraction / total : 0;
      if (item.ci && PROFILES[item.ci]) exact += weight;
      else missingCi.add(item.ci || 'CI-unverified');
    });
    return Object.freeze({
      exactFraction: exact,
      proxyFraction: 0,
      missingFraction: Math.max(0, 1 - exact),
      proxyCi: Object.freeze([]),
      missingCi: Object.freeze([...missingCi]),
      predictiveEligible: false,
      mode: SOURCE.usage
    });
  }

  return Object.freeze({ WAVELENGTHS, SOURCE, PROFILES, validateProfile, summarizeCoverage });
});
