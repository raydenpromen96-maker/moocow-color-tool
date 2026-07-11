(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.MooCowFamilySpectra = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const WAVELENGTHS = Object.freeze([400, 430, 460, 490, 520, 550, 580, 610, 640, 670, 700]);
  const D65 = Object.freeze([82.7549, 86.6823, 117.812, 108.811, 104.79, 104.046, 95.788, 89.5991, 83.6992, 82.2778, 71.6091]);
  const CIE_1931_2DEG = Object.freeze({
    x: Object.freeze([0.01431, 0.2839, 0.2908, 0.03201, 0.06327, 0.4334499, 0.9163, 1.0026, 0.4479, 0.0874, 0.01135916]),
    y: Object.freeze([0.000396, 0.0116, 0.06, 0.20802, 0.71, 0.9949501, 0.87, 0.503, 0.175, 0.032, 0.004102]),
    z: Object.freeze([0.06785001, 1.3856, 1.6692, 0.46518, 0.07824999, 0.00875, 0.00165, 0.00034, 0.00002, 0, 0])
  });

  const SOURCE = Object.freeze({
    id: 'multipigment-phantoms',
    title: 'MultipigmentPhantoms normalized pigment optical properties',
    authors: 'Alec Walter and E. D. Jansen',
    repository: 'https://github.com/AlecWalter/MultipigmentPhantoms',
    paper: 'https://doi.org/10.1117/1.JBO.28.2.025002',
    license: 'MIT',
    copyright: 'Copyright (c) 2022 AlecWalter',
    absorptionSha256: '8424BBFC20AE534D0ED295E82A022F3E4A617AAA5E5A4F9D16A9D8324F653014',
    reducedScatteringSha256: '6F13699B07CACB43605913F0C92F8E3D855DC8FD20466ED4FD4E7328EFDCF354',
    units: 'mm^-1 per (mg/g)',
    measurement: '370-950 nm pigment-in-epoxy phantoms; inverse adding-doubling; absorption mu_a and reduced scattering mu_s_prime; assumed anisotropy g=0.8',
    usage: 'shadow_shape_evidence_only'
  });

  const RESEARCH_REFERENCES = Object.freeze({
    ritArtistPaint: Object.freeze({
      title: 'Artist Paint Spectral Database',
      url: 'https://www.rit.edu/science/sites/rit.edu.science/files/2019-03/ArtistSpectralDatabase.pdf',
      dataBundled: false,
      reason: 'Upstream redistribution terms are not explicit on the current RIT download page.'
    }),
    colanylGreenGg: Object.freeze({
      title: 'Colanyl Green GG 131-TH / PG7 reflectance study',
      url: 'https://www.scienceasia.org/2020.46S.n1/scias46S_110.pdf',
      dataBundled: false,
      reason: 'The paper publishes a plotted curve, not a raw numeric table.'
    })
  });

  function profile(ci, absorption, reducedScattering) {
    return Object.freeze({
      ci,
      status: 'exact_ci_optical_prior',
      sourceId: SOURCE.id,
      wavelengths: WAVELENGTHS,
      absorption: Object.freeze(absorption),
      reducedScattering: Object.freeze(reducedScattering)
    });
  }

  const PROFILES = Object.freeze({
    PY74: profile('PY74',
      [0.532006043, 0.756384587, 0.313011212, 0.000306931, 0, 0, 0, 0, 0, 0, 0],
      [0.042481238, 0.039669854, 0.037389937, 0.035481207, 0.033841249, 0.032401529, 0.031114566, 0.029948686, 0.028878718, 0.027889434, 0.026964798]),
    PR122: profile('PR122',
      [0.067338678, 0.055726711, 0.083945045, 0.16329563, 0.32246694, 0.409501778, 0.244040701, 0.024675, 0.003797975, 0.000524413, 0],
      [0.05102372, 0.0452513, 0.04077691, 0.037193562, 0.034240865, 0.031743273, 0.029585814, 0.027687826, 0.025992253, 0.024458502, 0.02305506]),
    PV23: profile('PV23',
      [0.048841222, 0.019647927, 0.025451717, 0.091626902, 0.209613747, 0.246756577, 0.252164196, 0.167153306, 0.167213435, 0.039867597, 0],
      [0.032353112, 0.033896842, 0.034868288, 0.035446935, 0.035736654, 0.035824381, 0.035746103, 0.035534937, 0.035207756, 0.034779374, 0.034258616]),
    PG7: profile('PG7',
      [0.312774289, 0.190449378, 0.073280349, 0.008866558, 0.005202698, 0.028625295, 0.132464573, 0.430351414, 0.503074193, 0.398370596, 0.237001795],
      [0.032322656, 0.030833839, 0.029620993, 0.028628701, 0.027811219, 0.027134331, 0.026564244, 0.02608534, 0.025674527, 0.025322518, 0.025015306]),
    'PB15:3': profile('PB15:3',
      [0.194336517, 0.084220465, 0.028006133, 0.017262222, 0.0469275, 0.189299265, 0.467210133, 0.63616266, 0.652436627, 0.527776399, 0.615727007],
      [0.033678833, 0.030464909, 0.028345398, 0.026921186, 0.02593898, 0.025252263, 0.024766078, 0.024414778, 0.024159988, 0.023972682, 0.02383409]),
    PW6: profile('PW6',
      [0.010062901, 0.00000254, 0.00000232, 0.00000231, 0.00000247, 0.0000027, 0.00000275, 0.00000286, 0.00000269, 0.00000242, 0.00000234],
      [0.474521137, 0.471401627, 0.46303636, 0.451605292, 0.438249606, 0.423859261, 0.408839663, 0.393556879, 0.378274294, 0.363049669, 0.347988119])
  });

  function validateProfile(value) {
    const errors = [];
    if (!value || typeof value !== 'object') return { valid: false, errors: ['profile_missing'] };
    if (value.status !== 'exact_ci_optical_prior') errors.push('invalid_status');
    for (const key of ['wavelengths', 'absorption', 'reducedScattering']) {
      if (!Array.isArray(value[key]) || value[key].length !== WAVELENGTHS.length) errors.push(`${key}_length`);
    }
    if (Array.isArray(value.wavelengths) && value.wavelengths.some((v, i) => v !== WAVELENGTHS[i])) errors.push('wavelength_grid');
    if (Array.isArray(value.absorption) && value.absorption.some(v => !Number.isFinite(v) || v < 0)) errors.push('invalid_absorption');
    if (Array.isArray(value.reducedScattering) && value.reducedScattering.some(v => !Number.isFinite(v) || v < 0)) errors.push('invalid_reduced_scattering');
    return { valid: errors.length === 0, errors };
  }

  function normalizeShape(values) {
    if (!Array.isArray(values) || values.length === 0 || values.some(v => !Number.isFinite(v) || v < 0)) return null;
    const max = Math.max(...values);
    return max > 0 ? values.map(value => value / max) : values.map(() => 0);
  }

  function opticalShape(value) {
    if (!validateProfile(value).valid) return null;
    return Object.freeze({
      wavelengths: WAVELENGTHS,
      absorption: Object.freeze(normalizeShape(value.absorption)),
      reducedScattering: Object.freeze(normalizeShape(value.reducedScattering))
    });
  }

  function summarizeCoverage(entries) {
    const normalized = (Array.isArray(entries) ? entries : [])
      .map(item => ({ ci: item?.ci || null, fraction: Math.max(0, Number(item?.fraction) || 0) }))
      .filter(item => item.fraction > 0);
    const total = normalized.reduce((sum, item) => sum + item.fraction, 0);
    let exact = 0, missing = 0;
    const missingCi = new Set();
    normalized.forEach(item => {
      const weight = total > 0 ? item.fraction / total : 0;
      if (item.ci && PROFILES[item.ci]) exact += weight;
      else { missing += weight; missingCi.add(item.ci || 'CI-unverified'); }
    });
    return Object.freeze({
      exactFraction: exact,
      proxyFraction: 0,
      missingFraction: missing,
      proxyCi: Object.freeze([]),
      missingCi: Object.freeze([...missingCi]),
      predictiveEligible: false,
      mode: SOURCE.usage
    });
  }

  return Object.freeze({ WAVELENGTHS, D65, CIE_1931_2DEG, SOURCE, RESEARCH_REFERENCES, PROFILES, validateProfile, normalizeShape, opticalShape, summarizeCoverage });
});
