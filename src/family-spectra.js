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

  // CHSOS Pigments Checker 实测反射率（chsopensource.org，丙烯粘合剂涂布样本，
  // 光纤反射光谱仪实测）。原始 CSV 波长范围覆盖 400-700nm，按线性插值重采样到
  // 上方 11 波长点并裁剪到 [0.001, 0.999]；重采样逻辑与
  // experiments/whatif-real-spectra.mjs 的 loadChsos 一致，数值由
  // experiments/derive-hardcoded-spectra.mjs 从原始 CSV 算出后硬编码在此，
  // 运行时不依赖仓库外文件。
  // 注意：这是"代理数据，待 45 卡实测校准替换"——样本是通用颜料涂布而非
  // 科莱恩 Colanyl CN 色浆批次，且 CHSOS PB15 未区分 15:1/15:3 晶型。
  const CHSOS_SOURCE = Object.freeze({
    id: 'chsos-pigments-checker-acrylic',
    title: 'Pigments Checker measured reflectance, acrylic binder',
    provider: 'Cultural Heritage Science Open Source (CHSOS)',
    page: 'https://chsopensource.org/pigments-checker/',
    measurement: 'measured reflectance of acrylic-binder drawdown samples; resampled to 400-700 nm at 30 nm intervals',
    usage: 'measured_proxy_pending_45card_calibration'
  });

  // PG36 光谱的来源说明：CHSOS 的 PG36 实测样本不具代表性（涂布过浅，
  // L=66.8 vs GOLDEN 官方 PG36 本色 L=27.8，且任何 K/S 强度缩放下都无法
  // 拟合官方本色 Lab），已弃用。最终采用 GOLDEN 实测 PG7（同族颜料）曲线
  // 向长波平移 23nm（溴代使吸收边红移）、增益 1.0，锚定 GOLDEN 官方公布
  // PG36 本色 Lab (27.82, -11.83, -0.17)，拟合误差 0.32。
  // 形状 = 同族实测外推，强度/色相 = 官方公布锚点；属"有锚点的近似"，非纯实测。
  const PG36_SOURCE = Object.freeze({
    id: 'pg7-shift-23nm-official-masstone-anchored',
    title: 'PG7 measured spectrum shifted +23 nm, anchored to GOLDEN official PG36 masstone Lab',
    anchorLab: Object.freeze([27.82, -11.83, -0.17]),
    fitError: 0.32,
    usage: 'anchored_extrapolation_pending_measurement'
  });

  const PROFILE_STATUSES = Object.freeze([
    'exact_ci_waterborne_acrylic_reference',
    'chsos_measured_acrylic_proxy_reference',
    'pg7_shift_masstone_anchored_proxy_reference'
  ]);

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

  function proxyProfile(ci, status, source, reflectance, kOverS) {
    return Object.freeze({
      ci,
      status,
      sourceId: source.id,
      source,
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
      [7.4051, 5.9518, 4.2997, 3.6921, 2.0758, 0.6716, 0.3029, 0.3351, 0.391, 0.3912, 0.3365]),

    // ---- CHSOS Pigments Checker 实测（丙烯粘合剂），重采样到 11 点 ----
    // 来源：chsopensource.org Pigments Checker；数值由 experiments/derive-hardcoded-spectra.mjs
    // 从原始 CSV 重采样算出。代理数据，待 45 卡实测校准替换。
    PY74: proxyProfile('PY74', 'chsos_measured_acrylic_proxy_reference', CHSOS_SOURCE,
      // PY74_arylide_yellow_5GX.csv（对应 Y74S / Yellow 2GXD 130-CN）
      [0.0688, 0.0704, 0.0664, 0.0819, 0.5174, 0.8326, 0.846, 0.8405, 0.8332, 0.821, 0.8103],
      [6.3017, 6.1326, 6.5629, 5.1441, 0.2251, 0.0168, 0.014, 0.0151, 0.0167, 0.0195, 0.0222]),
    'PB15:1': proxyProfile('PB15:1', 'chsos_measured_acrylic_proxy_reference', CHSOS_SOURCE,
      // PB15_phthalo_blue.csv（对应 B150S / Blue A2R 131-CN；CHSOS 未分晶型，代用 PB15:1）
      [0.0638, 0.1064, 0.2119, 0.1438, 0.0536, 0.0331, 0.0327, 0.035, 0.0367, 0.0378, 0.0429],
      [6.872, 3.7514, 1.4658, 2.5478, 8.3487, 14.1441, 14.3153, 13.3006, 12.6246, 12.2379, 10.6764]),
    PO73: proxyProfile('PO73', 'chsos_measured_acrylic_proxy_reference', CHSOS_SOURCE,
      // PO73_pyrrole_orange.csv（对应 073 / Orange D2R 100-CN，供应商确认 CI 56117）
      [0.0766, 0.0439, 0.0346, 0.0358, 0.0339, 0.0502, 0.3216, 0.6968, 0.7381, 0.7309, 0.7251],
      [5.5637, 10.4033, 13.4697, 12.977, 13.7707, 8.9834, 0.7155, 0.066, 0.0465, 0.0495, 0.0521]),
    PW6: proxyProfile('PW6', 'chsos_measured_acrylic_proxy_reference', CHSOS_SOURCE,
      // PW6_titanium_white.csv（金红石型，对应 W064 / White TQ 100-CN）
      [0.4848, 0.9053, 0.8988, 0.8865, 0.8831, 0.8746, 0.859, 0.8467, 0.8368, 0.8224, 0.81],
      [0.2737, 0.005, 0.0057, 0.0073, 0.0077, 0.009, 0.0116, 0.0139, 0.0159, 0.0192, 0.0223]),

    // ---- PG36：PG7 实测平移 +23nm、增益 1.0，锚定 GOLDEN 官方 PG36 本色 Lab ----
    // （CHSOS PG36 样本不具代表性已弃用；拟合误差 0.32。代理数据，待实测替换。）
    PG36: proxyProfile('PG36', 'pg7_shift_masstone_anchored_proxy_reference', PG36_SOURCE,
      [0.0511, 0.0515, 0.0543, 0.0663, 0.0814, 0.0551, 0.0401, 0.0388, 0.0415, 0.046, 0.0498],
      [8.8103, 8.7395, 8.2336, 6.5803, 5.1812, 8.1036, 11.5003, 11.9016, 11.0612, 9.8879, 9.0664])
  });

  function validateProfile(value) {
    const errors = [];
    if (!value || typeof value !== 'object') return { valid: false, errors: ['profile_missing'] };
    if (!PROFILE_STATUSES.includes(value.status)) errors.push('invalid_status');
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
    let proxy = 0;
    const proxyCi = new Set();
    const missingCi = new Set();
    normalized.forEach(item => {
      const weight = total > 0 ? item.fraction / total : 0;
      const profile = item.ci ? PROFILES[item.ci] : null;
      if (profile?.status === 'exact_ci_waterborne_acrylic_reference') exact += weight;
      else if (profile) { proxy += weight; proxyCi.add(item.ci); }
      else missingCi.add(item.ci || 'CI-unverified');
    });
    return Object.freeze({
      exactFraction: exact,
      proxyFraction: proxy,
      missingFraction: Math.max(0, 1 - exact - proxy),
      proxyCi: Object.freeze([...proxyCi]),
      missingCi: Object.freeze([...missingCi]),
      predictiveEligible: false,
      mode: SOURCE.usage
    });
  }

  return Object.freeze({ WAVELENGTHS, SOURCE, CHSOS_SOURCE, PG36_SOURCE, PROFILE_STATUSES, PROFILES, validateProfile, summarizeCoverage });
});
