(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.MooCowProductionRuntime = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const SPECTRAL_WAVELENGTHS = Object.freeze([400, 430, 460, 490, 520, 550, 580, 610, 640, 670, 700]);
  const D65_30NM = Object.freeze([82.7549, 86.6823, 117.812, 108.811, 104.79, 104.046, 95.788, 89.5991, 83.6992, 82.2778, 71.6091]);
  const CIE_1931_2DEG_30NM = Object.freeze({
    x: Object.freeze([0.01431, 0.2839, 0.2908, 0.03201, 0.06327, 0.4334499, 0.9163, 1.0026, 0.4479, 0.0874, 0.01135916]),
    y: Object.freeze([0.000396, 0.0116, 0.06, 0.20802, 0.71, 0.9949501, 0.87, 0.503, 0.175, 0.032, 0.004102]),
    z: Object.freeze([0.06785001, 1.3856, 1.6692, 0.46518, 0.07824999, 0.00875, 0.00165, 0.00034, 0.00002, 0, 0])
  });
  const REFERENCE_TRUST = Object.freeze({ high: 0.82, medium: 0.62, low: 0.42 });
  const REFERENCE_SPECTRA = Object.freeze({
    PY74: Object.freeze([0.06, 0.08, 0.12, 0.35, 0.72, 0.87, 0.90, 0.88, 0.84, 0.82, 0.80]),
    PY83: Object.freeze([0.04, 0.05, 0.07, 0.18, 0.52, 0.82, 0.90, 0.88, 0.83, 0.77, 0.70]),
    'PB15:1': Object.freeze([0.12, 0.20, 0.34, 0.25, 0.08, 0.035, 0.025, 0.022, 0.025, 0.030, 0.040]),
    'PB15:3': Object.freeze([0.10, 0.22, 0.38, 0.34, 0.14, 0.050, 0.025, 0.022, 0.025, 0.030, 0.040]),
    PG7: Object.freeze([0.04, 0.06, 0.08, 0.22, 0.42, 0.32, 0.08, 0.035, 0.025, 0.025, 0.030]),
    PR254: Object.freeze([0.04, 0.04, 0.05, 0.06, 0.07, 0.12, 0.34, 0.62, 0.74, 0.72, 0.66]),
    PR101: Object.freeze([0.08, 0.07, 0.07, 0.08, 0.10, 0.16, 0.28, 0.38, 0.45, 0.49, 0.50]),
    PY42: Object.freeze([0.10, 0.11, 0.13, 0.18, 0.30, 0.45, 0.56, 0.58, 0.55, 0.50, 0.45]),
    PO13: Object.freeze([0.05, 0.05, 0.06, 0.08, 0.14, 0.30, 0.62, 0.78, 0.76, 0.70, 0.62]),
    PR122: Object.freeze([0.16, 0.22, 0.26, 0.18, 0.08, 0.06, 0.11, 0.30, 0.56, 0.64, 0.60]),
    PV23: Object.freeze([0.18, 0.24, 0.30, 0.18, 0.07, 0.04, 0.05, 0.10, 0.18, 0.26, 0.32]),
    PBk7: Object.freeze([0.020, 0.021, 0.021, 0.022, 0.023, 0.024, 0.025, 0.026, 0.027, 0.028, 0.030]),
    PW6: Object.freeze([0.92, 0.93, 0.94, 0.95, 0.96, 0.96, 0.95, 0.95, 0.94, 0.94, 0.93])
  });
  const TOTAL_PIGMENT_PER_LITER = 106;
  const CANDIDATE_SEARCH_POLICY = Object.freeze({ totalGpl: 106, gridGpl: 0.5, minActiveGpl: 1.0, maxActive: 4, candidateCount: 3 });
  const CANDIDATE_SEARCH_BOUNDS = Object.freeze({ maxSupports: 30, maxRefinementSteps: 60 });
  const BLACK_SUBSTRATE_RGB = Object.freeze([0, 0, 0]);
  const WHITE_SUBSTRATE_RGB = Object.freeze([255, 255, 255]);
  const TWO_COAT_PASS_DE = 3.0;
  const TWO_COAT_WARNING_DE = 6.0;

  function cloneCatalog(rawCatalog) {
    return Object.fromEntries(Object.entries(rawCatalog || {}).map(([code, pigment]) => [code, {
      ...pigment,
      aliases: pigment.aliases ? [...pigment.aliases] : undefined,
      manualLab: pigment.manualLab ? [...pigment.manualLab] : undefined
    }]));
  }

  function deepFreeze(value) {
    if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
    Object.values(value).forEach(deepFreeze);
    return Object.freeze(value);
  }

  const LEGACY_SCREENING_PROVENANCE = deepFreeze({
    evidence_class: 'catalog_screen_approximation',
    calibration_status: 'uncalibrated_screening_only',
    physical_accuracy_verified: false,
    measured_current_batch: false,
    runtime_activation_permitted: false
  });

  function create(dependencies) {
    const { ColorCore, RecipeSearch, FamilySpectra, paintCatalog } = dependencies || {};
    if (!ColorCore || !RecipeSearch || !paintCatalog) {
      throw new TypeError('production runtime requires ColorCore, RecipeSearch, and paintCatalog');
    }

    const {
      hexToRgb,
      rgbToHex,
      srgbToLinear,
      linearToSrgb,
      clampR,
      getKS,
      getRfromKS,
      rgbToLab,
      deltaE2000,
      deriveModelColor
    } = ColorCore;
    const catalog = cloneCatalog(paintCatalog);

    function normalizeCurve(curve) {
      if (!Array.isArray(curve) || curve.length !== SPECTRAL_WAVELENGTHS.length) return null;
      if (curve.some(value => typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1)) return null;
      return curve.map(clampR);
    }

    function linearToRgbFloat(value) {
      return Math.max(0, Math.min(255, linearToSrgb(Math.max(0, value)) * 255));
    }

    function xyzToRgb(X, Y, Z) {
      X /= 100; Y /= 100; Z /= 100;
      const r = X * 3.2406 + Y * -1.5372 + Z * -0.4986;
      const g = X * -0.9689 + Y * 1.8758 + Z * 0.0415;
      const b = X * 0.0557 + Y * -0.2040 + Z * 1.0570;
      return [linearToRgbFloat(r), linearToRgbFloat(g), linearToRgbFloat(b)];
    }

    function labToRgbFloat(L, a, b) {
      let y = (L + 16) / 116;
      let x = a / 500 + y;
      let z = y - b / 200;
      const pivot = value => {
        const cubed = value * value * value;
        return cubed > 0.008856 ? cubed : (value - 16 / 116) / 7.787;
      };
      x = 95.047 * pivot(x);
      y = 100 * pivot(y);
      z = 108.883 * pivot(z);
      return xyzToRgb(x, y, z);
    }

    function spectrumToRgb(curve) {
      const normalized = normalizeCurve(curve);
      if (!normalized) return null;
      let X = 0, Y = 0, Z = 0, whiteX = 0, whiteY = 0, whiteZ = 0;
      normalized.forEach((reflectance, index) => {
        const illuminant = D65_30NM[index];
        const cx = CIE_1931_2DEG_30NM.x[index];
        const cy = CIE_1931_2DEG_30NM.y[index];
        const cz = CIE_1931_2DEG_30NM.z[index];
        X += reflectance * illuminant * cx; Y += reflectance * illuminant * cy; Z += reflectance * illuminant * cz;
        whiteX += illuminant * cx; whiteY += illuminant * cy; whiteZ += illuminant * cz;
      });
      return xyzToRgb((X / whiteX) * 95.047, (Y / whiteY) * 100, (Z / whiteZ) * 108.883);
    }

    function getReferenceTrust(pigment) {
      return REFERENCE_TRUST[pigment.referenceConfidence] || 0.5;
    }

    function getPigmentLoadFactor(pigment) {
      const pigmentFactor = Math.sqrt(Math.max(0.20, (pigment.pigmentContent || 42) / 42));
      const solidFactor = Math.sqrt(Math.max(0.35, (pigment.solidContent || 55) / 55));
      const densityFactor = Math.sqrt(Math.max(0.45, (pigment.density || 1.25) / 1.25));
      return pigmentFactor * solidFactor * densityFactor;
    }

    function preparePigments() {
      if (Object.isFrozen(catalog)) return catalog;
      Object.values(catalog).forEach(pigment => {
        const modelColor = deriveModelColor(pigment);
        pigment.lab = modelColor.modelLab;
        pigment.displayRgb = modelColor.displayRgb;
        pigment.physicsRgb = labToRgbFloat(...modelColor.modelLab);
        pigment.hex = pigment.hex || rgbToHex(pigment.physicsRgb);
        pigment.modelInput = modelColor.modelLabSource;
        pigment.displayConflictDE = modelColor.displayConflictDE;
        pigment.effectiveStrength = (pigment.mixStrength || pigment.strength || 1.0) * (pigment.colorStrength || 1.0);
        const sourceSpectrum = Object.prototype.hasOwnProperty.call(pigment, 'referenceSpectrum')
          ? pigment.referenceSpectrum
          : REFERENCE_SPECTRA[pigment.ci];
        pigment.referenceSpectrum = normalizeCurve(sourceSpectrum);
        pigment.familySpectralProfile = FamilySpectra?.PROFILES[pigment.ci] || null;
        pigment.spectralRgb = pigment.referenceSpectrum ? spectrumToRgb(pigment.referenceSpectrum) : pigment.physicsRgb;
        pigment.referenceWeight = getReferenceTrust(pigment);
        const angle = Math.atan2(pigment.lab[2], pigment.lab[1]) * 180 / Math.PI;
        pigment.hue = angle < 0 ? angle + 360 : angle;
        pigment.chroma = Math.sqrt(pigment.lab[1] ** 2 + pigment.lab[2] ** 2);
        const rgb = pigment.physicsRgb;
        pigment.linear = [srgbToLinear(rgb[0] / 255), srgbToLinear(rgb[1] / 255), srgbToLinear(rgb[2] / 255)];
        pigment.ks = pigment.linear.map(getKS);
      });
      return catalog;
    }

    function resolvePaintCode(code) {
      if (catalog[code]) return code;
      return Object.keys(catalog).find(key => (catalog[key].aliases || []).includes(code)) || code;
    }

    function getRecipeEntries(recipe, options = {}) {
      const sum = Object.values(recipe || {}).reduce((total, value) => total + Math.max(0, Number(value) || 0), 0);
      if (sum <= 0) return [];
      const factor = options.volumeFactor || 1;
      return Object.entries(recipe).map(([rawCode, rawWeight]) => {
        const code = resolvePaintCode(rawCode);
        const weight = Math.max(0, Number(rawWeight) || 0);
        const pigment = catalog[code];
        if (!pigment || weight <= 0) return null;
        const fraction = weight / sum;
        const gramsPerLiter = options.totalGramsPerLiter ? fraction * options.totalGramsPerLiter : weight / factor;
        return { code, pigment, weight, fraction, gramsPerLiter };
      }).filter(Boolean);
    }

    function simulateMixKsRgb(entries) {
      let totalWeight = 0;
      let numR = 0, numG = 0, numB = 0;
      entries.forEach(({ weight, pigment }) => {
        const effectiveWeight = weight * (pigment.effectiveStrength || 1.0) * getPigmentLoadFactor(pigment);
        totalWeight += effectiveWeight;
        numR += effectiveWeight * pigment.ks[0];
        numG += effectiveWeight * pigment.ks[1];
        numB += effectiveWeight * pigment.ks[2];
      });
      if (totalWeight === 0) return [255, 255, 255];
      return [
        linearToRgbFloat(getRfromKS(numR / totalWeight)),
        linearToRgbFloat(getRfromKS(numG / totalWeight)),
        linearToRgbFloat(getRfromKS(numB / totalWeight))
      ];
    }

    function simulateMixReferenceSpectra(entries) {
      if (!entries.length) return null;
      const curves = entries.map(({ pigment }) => normalizeCurve(pigment.referenceSpectrum));
      if (curves.some(curve => !curve)) return null;
      const ks = SPECTRAL_WAVELENGTHS.map(() => 0);
      let totalWeight = 0;
      entries.forEach(({ weight, pigment }, entryIndex) => {
        const curve = curves[entryIndex];
        const effectiveWeight = weight * (pigment.effectiveStrength || 1.0) * getPigmentLoadFactor(pigment);
        totalWeight += effectiveWeight;
        curve.forEach((reflectance, index) => { ks[index] += effectiveWeight * getKS(reflectance); });
      });
      if (totalWeight === 0) return null;
      return spectrumToRgb(ks.map(value => getRfromKS(value / totalWeight)));
    }

    function estimateReferenceTrust(entries) {
      if (!entries.length) return 0;
      return entries.reduce((sum, { pigment, fraction }) => sum + fraction * getReferenceTrust(pigment), 0);
    }

    function blendRgbModels(primaryRgb, secondaryRgb, primaryWeight) {
      const weight = Math.max(0, Math.min(1, primaryWeight));
      return primaryRgb.map((channel, index) => {
        const primary = srgbToLinear(channel / 255);
        const secondary = srgbToLinear(secondaryRgb[index] / 255);
        return linearToRgbFloat(primary * weight + secondary * (1 - weight));
      });
    }

    function simulateMix(recipe, options = {}) {
      const entries = getRecipeEntries(recipe, options);
      if (!entries.length) return [255, 255, 255];
      const kmRgb = simulateMixKsRgb(entries);
      const referenceRgb = simulateMixReferenceSpectra(entries);
      let output = kmRgb;
      if (referenceRgb) {
        const trust = Math.max(0.35, Math.min(0.76, estimateReferenceTrust(entries)));
        output = blendRgbModels(referenceRgb, output, trust);
      }
      return output;
    }

    function calculateHidingAlpha(recipe, coats = 2, options = {}) {
      const entries = getRecipeEntries(recipe, options);
      if (!entries.length) return 0;
      let opticalLoad = 0;
      entries.forEach(({ pigment, gramsPerLiter, fraction }) => {
        const load = Math.max(0, gramsPerLiter);
        const hiding = (pigment.hidingPower || 60) / 100;
        const tint = Math.sqrt(Math.max(0.05, pigment.colorStrength || 1));
        const ciScatterBoost = { PW6: 1.55, PBk7: 1.35, PR101: 1.25, PY42: 1.18, PG7: 1.06 }[pigment.ci] || 1.0;
        const whiteBoost = pigment === catalog.W064 ? 1.28 : 1.0;
        const blackBoost = pigment === catalog.BK7H ? 1.15 : 1.0;
        opticalLoad += load * hiding * tint * getPigmentLoadFactor(pigment) * ciScatterBoost * whiteBoost * blackBoost * (0.65 + 0.35 * fraction);
      });
      const opticalDepth = opticalLoad / 52;
      return Math.max(0, Math.min(0.995, 1 - Math.exp(-opticalDepth * coats)));
    }

    function blendLinear(topRgb, substrateRgb, alpha) {
      return topRgb.map((channel, index) => {
        const top = srgbToLinear(channel / 255);
        const bottom = srgbToLinear(substrateRgb[index] / 255);
        return linearToRgbFloat(top * alpha + bottom * (1 - alpha));
      });
    }

    function simulateOverSubstrate(recipe, substrateRgb = BLACK_SUBSTRATE_RGB, coats = 2, options = {}) {
      const topRgb = simulateMix(recipe, options);
      const alpha = calculateHidingAlpha(recipe, coats, options);
      return { rgb: blendLinear(topRgb, substrateRgb, alpha), alpha, topRgb };
    }

    function resolveTargetColor(targetColor) {
      const targetHex = typeof targetColor === 'string' ? targetColor : targetColor?.hex;
      const targetRgb = hexToRgb(targetHex || '#FFFFFF') || [255, 255, 255];
      const embeddedLab = typeof targetColor === 'object' ? targetColor?.targetLab : null;
      const targetLab = Array.isArray(embeddedLab) && embeddedLab.length === 3 && embeddedLab.every(Number.isFinite)
        ? embeddedLab.map(Number)
        : rgbToLab(...targetRgb);
      return { targetRgb, targetLab, targetLabSource: embeddedLab ? 'qtcLab' : 'hexFallback' };
    }

    function evaluateRecipe(recipe, targetColor, options = {}) {
      const entries = getRecipeEntries(recipe, options);
      const { targetRgb, targetLab, targetLabSource } = resolveTargetColor(targetColor);
      const kmRgb = simulateMixKsRgb(entries);
      const referenceRgb = simulateMixReferenceSpectra(entries);
      const referenceTrust = referenceRgb ? estimateReferenceTrust(entries) : 0;
      const topRgb = simulateMix(recipe, options);
      const topLab = rgbToLab(...topRgb);
      const single = simulateOverSubstrate(recipe, BLACK_SUBSTRATE_RGB, 1, options);
      const double = simulateOverSubstrate(recipe, BLACK_SUBSTRATE_RGB, 2, options);
      const doubleWhite = simulateOverSubstrate(recipe, WHITE_SUBSTRATE_RGB, 2, options);
      const singleLab = rgbToLab(...single.rgb);
      const doubleLab = rgbToLab(...double.rgb);
      const doubleWhiteLab = rgbToLab(...doubleWhite.rgb);
      const dE = deltaE2000(targetLab, doubleLab);
      const singleDE = deltaE2000(targetLab, singleLab);
      const whiteDE = deltaE2000(targetLab, doubleWhiteLab);
      const topDE = deltaE2000(targetLab, topLab);
      const substrateShift = deltaE2000(doubleLab, doubleWhiteLab);
      const modelSpread = referenceRgb ? deltaE2000(rgbToLab(...referenceRgb), rgbToLab(...kmRgb)) : 0;
      const familySpectralCoverage = FamilySpectra
        ? FamilySpectra.summarizeCoverage(entries.map(({ pigment, fraction }) => ({ ci: pigment.ci, fraction })))
        : null;
      const colorClose = dE <= TWO_COAT_PASS_DE && double.alpha >= 0.96 && substrateShift <= 3.0;
      const colorUsable = dE <= TWO_COAT_WARNING_DE && double.alpha >= 0.92 && substrateShift <= 7.0;
      let grade = 'fail';
      if (colorClose) {
        if (singleDE <= TWO_COAT_PASS_DE && single.alpha >= 0.96 && substrateShift <= 2.0 && modelSpread <= 8.0) {
          grade = 'excellent';
        } else {
          grade = modelSpread <= 12.0 ? 'pass' : 'warning';
        }
      } else if (colorUsable) {
        grade = 'warning';
      }
      return { targetRgb, targetLab, targetLabSource, topRgb, topLab, kmRgb, referenceRgb, single, double, doubleWhite, dE, singleDE, whiteDE, topDE, substrateShift, modelSpread, referenceTrust, familySpectralCoverage, provenance: LEGACY_SCREENING_PROVENANCE, grade };
    }

    function normalizeRecipe(recipe) {
      const merged = {};
      Object.entries(recipe || {}).forEach(([rawCode, rawValue]) => {
        const code = resolvePaintCode(rawCode);
        const value = Math.max(0, Number(rawValue) || 0);
        if (!catalog[code] || value <= 0) return;
        merged[code] = (merged[code] || 0) + value;
      });
      const sum = Object.values(merged).reduce((total, value) => total + value, 0);
      if (sum <= 0) return {};
      Object.keys(merged).forEach(code => { merged[code] = merged[code] / sum * 100; });
      return merged;
    }

    function cleanRecipe(recipe, minPercent = 0.08) {
      const normalized = normalizeRecipe(recipe);
      Object.keys(normalized).forEach(code => {
        if (normalized[code] < minPercent) delete normalized[code];
      });
      return normalizeRecipe(normalized);
    }

    function recipePercentToGpl(recipePercent) {
      const normalized = normalizeRecipe(recipePercent);
      return Object.fromEntries(Object.entries(normalized).map(([code, percent]) => [
        code,
        percent / 100 * CANDIDATE_SEARCH_POLICY.totalGpl
      ]));
    }

    function recipeGplToPercent(recipeGpl) {
      return normalizeRecipe(recipeGpl);
    }

    function circularHueDistance(left, right) {
      const difference = Math.abs(left - right) % 360;
      return Math.min(difference, 360 - difference);
    }

    function objectiveForRecipe(recipe, targetColor) {
      const normalized = normalizeRecipe(recipe);
      if (!Object.keys(normalized).length) return Infinity;
      const evaluation = evaluateRecipe(normalized, targetColor, { totalGramsPerLiter: TOTAL_PIGMENT_PER_LITER });
      const activeCount = Object.values(normalized).filter(value => value > 0.25).length;
      const tinyCount = Object.values(normalized).filter(value => value > 0.05 && value <= 0.8).length;
      const hidingPenalty = Math.max(0, 0.965 - evaluation.double.alpha) * 18;
      const substratePenalty = Math.max(0, evaluation.substrateShift - 2.0) * 0.65;
      const complexityPenalty = Math.max(0, activeCount - 5) * 0.45 + Math.max(0, tinyCount - 1) * 0.12;
      const topColorPenalty = Math.max(0, evaluation.topDE - 4.5) * 0.32;
      const modelPenalty = Math.max(0, evaluation.modelSpread - 5.5) * 0.26;
      const confidencePenalty = Math.max(0, 0.58 - evaluation.referenceTrust) * 1.4;
      return evaluation.dE + hidingPenalty + substratePenalty + complexityPenalty + topColorPenalty + modelPenalty + confidencePenalty;
    }

    function boundedMetric(value, scale, maximum) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return maximum;
      return Math.min(maximum, Math.max(0, Math.round(numeric * scale)));
    }

    function candidateMetricScore(evaluation, activeCount) {
      const feasibilityPenalty = candidateFeasibilityTier({
        hidingAlpha: evaluation?.double?.alpha,
        substrateShift: evaluation?.substrateShift
      }) * 1000000000000000;
      const twoCoatDe = boundedMetric(evaluation?.dE, 100, 9999);
      const modelSpread = boundedMetric(evaluation?.modelSpread, 10, 999);
      const substrateShift = boundedMetric(evaluation?.substrateShift, 10, 999);
      const referenceTrustPenalty = Math.min(1000, Math.max(0, Math.round((1 - Math.min(1, Math.max(0, Number(evaluation?.referenceTrust) || 0))) * 1000)));
      const activeCountPenalty = Math.min(99, Math.max(0, Math.round(Number(activeCount) || 0)));
      return feasibilityPenalty
        + twoCoatDe * 100000000000
        + modelSpread * 100000000
        + substrateShift * 100000
        + referenceTrustPenalty * 100
        + activeCountPenalty;
    }

    function candidateFeasibilityTier(metrics) {
      const alpha = Number(metrics?.hidingAlpha);
      const substrateShift = Number(metrics?.substrateShift);
      if (alpha >= 0.96 && substrateShift <= 3.0) return 0;
      return 1;
    }

    function compareRecommendedCandidates(left, right) {
      return candidateFeasibilityTier(left.metrics) - candidateFeasibilityTier(right.metrics)
        || Number(left.score) - Number(right.score)
        || String(left.supportKey).localeCompare(String(right.supportKey));
    }

    function addLightnessSupport(recipe, targetL, chroma) {
      const output = { ...recipe };
      const currentSum = Object.values(output).reduce((sum, value) => sum + value, 0);
      let white = Math.max(0, (targetL - 45) * 1.05);
      let black = Math.max(0, (42 - targetL) * 0.65);
      if (targetL > 78) white += 18;
      if (targetL < 25) black += 12;
      if (chroma < 12) {
        white += Math.max(0, targetL - 35) * 0.55;
        black += Math.max(0, 55 - targetL) * 0.22;
      }
      const support = Math.min(86, white + black);
      const chromaScale = currentSum > 0 ? Math.max(12, 100 - support) / currentSum : 1;
      Object.keys(output).forEach(code => { output[code] *= chromaScale; });
      if (white > 0) output.W064 = (output.W064 || 0) + white;
      if (black > 0) output.BK7H = (output.BK7H || 0) + black;
      return normalizeRecipe(output);
    }

    function buildSeedRecipes(targetLab, existingRecipe) {
      const [targetL, targetA, targetB] = targetLab;
      const targetHue = Math.atan2(targetB, targetA) * 180 / Math.PI;
      const hue = targetHue < 0 ? targetHue + 360 : targetHue;
      const chroma = Math.sqrt(targetA * targetA + targetB * targetB);
      const chromatic = Object.entries(catalog)
        .filter(([code]) => code !== 'W064' && code !== 'BK7H')
        .map(([code, pigment]) => ({ code, score: circularHueDistance(hue, pigment.hue), pigment }))
        .sort((left, right) => left.score - right.score);
      const seeds = [];
      if (existingRecipe) seeds.push(normalizeRecipe(existingRecipe));
      seeds.push(addLightnessSupport({}, targetL, chroma));
      chromatic.slice(0, 6).forEach(({ code }) => { seeds.push(addLightnessSupport({ [code]: 100 }, targetL, chroma)); });
      for (let index = 0; index < Math.min(5, chromatic.length - 1); index += 1) {
        const first = chromatic[index];
        const second = chromatic[index + 1];
        seeds.push(addLightnessSupport({ [first.code]: 65, [second.code]: 35 }, targetL, chroma));
        seeds.push(addLightnessSupport({ [first.code]: 35, [second.code]: 65 }, targetL, chroma));
      }
      if (chroma < 14) {
        seeds.push(normalizeRecipe({ W064: Math.max(1, targetL), BK7H: Math.max(1, 100 - targetL) }));
        seeds.push(normalizeRecipe({ W064: targetL * 0.9, BK7H: (100 - targetL) * 0.6, Y42S: 4, B150S: 2 }));
      }
      seeds.push(normalizeRecipe({ W064: 78, Y74S: 16, Y83S: 6 }));
      seeds.push(normalizeRecipe({ R254D: 45, '073': 30, Y83S: 15, W064: 10 }));
      seeds.push(normalizeRecipe({ B150S: 45, B153S: 30, W064: 20, BK7H: 5 }));
      seeds.push(normalizeRecipe({ G7: 48, Y74S: 28, W064: 20, BK7H: 4 }));
      return seeds.filter(recipe => Object.keys(recipe).length);
    }

    function twoCoatProposalScore(recipe, targetColor) {
      const evaluation = evaluateRecipe(recipe, targetColor, { totalGramsPerLiter: TOTAL_PIGMENT_PER_LITER });
      return candidateMetricScore(evaluation, Object.values(recipe).filter(value => value > 0.25).length);
    }

    function refineObjectiveSeedProposal(seedRecipe, targetColor) {
      const codes = Object.keys(catalog);
      let best = normalizeRecipe(seedRecipe);
      let bestScore = objectiveForRecipe(best, targetColor);
      let step = 14;
      while (step >= 0.08) {
        let improved = false;
        for (const from of codes) {
          if ((best[from] || 0) <= 0.01) continue;
          for (const to of codes) {
            if (from === to) continue;
            const shift = Math.min(step, best[from] || 0);
            if (shift <= 0) continue;
            const candidate = { ...best };
            candidate[from] = (candidate[from] || 0) - shift;
            candidate[to] = (candidate[to] || 0) + shift;
            const normalized = normalizeRecipe(candidate);
            const score = objectiveForRecipe(normalized, targetColor);
            if (score + 0.0001 < bestScore) {
              best = normalized;
              bestScore = score;
              improved = true;
            }
          }
        }
        if (!improved) step *= 0.58;
      }
      const canonical = cleanRecipe(best, 0.05);
      Object.keys(canonical).forEach(code => { canonical[code] = Math.round(canonical[code] * 100) / 100; });
      const recipe = normalizeRecipe(canonical);
      return { recipe, score: objectiveForRecipe(recipe, targetColor) };
    }

    function refineSeedProposal(seedRecipe, targetColor, scoreRecipe) {
      const codes = Object.keys(catalog);
      let best = normalizeRecipe(seedRecipe);
      let bestScore = scoreRecipe(best, targetColor);
      let step = 14;
      let passes = 0;
      while (step >= 0.08 && passes < 48) {
        passes += 1;
        let improved = false;
        for (const from of codes) {
          if ((best[from] || 0) <= 0.01) continue;
          for (const to of codes) {
            if (from === to) continue;
            const shift = Math.min(step, best[from] || 0);
            if (shift <= 0) continue;
            const candidate = { ...best };
            candidate[from] = (candidate[from] || 0) - shift;
            candidate[to] = (candidate[to] || 0) + shift;
            const normalized = normalizeRecipe(candidate);
            const score = scoreRecipe(normalized, targetColor);
            if (score + 0.0001 < bestScore) {
              best = normalized;
              bestScore = score;
              improved = true;
            }
          }
        }
        if (!improved) step *= 0.58;
      }
      return { recipe: cleanRecipe(best), score: bestScore };
    }

    function generateCandidates(targetColor) {
      const targetLab = resolveTargetColor(targetColor).targetLab;
      const seedRecipes = buildSeedRecipes(targetLab, targetColor?.baseRecipe);
      const objectiveSeedProposal = seedRecipes.reduce((bestProposal, seedRecipe) => {
        const proposal = refineObjectiveSeedProposal(seedRecipe, targetColor);
        return proposal.score < bestProposal.score ? proposal : bestProposal;
      }, { recipe: null, score: Infinity });
      const twoCoatSeedProposal = seedRecipes.reduce((bestProposal, seedRecipe) => {
        const proposal = refineSeedProposal(seedRecipe, targetColor, twoCoatProposalScore);
        return proposal.score < bestProposal.score ? proposal : bestProposal;
      }, { recipe: null, score: Infinity });
      const seeds = seedRecipes.map(recipePercentToGpl);
      [objectiveSeedProposal, twoCoatSeedProposal].forEach(proposal => {
        if (proposal.recipe) seeds.push(recipePercentToGpl(proposal.recipe));
      });
      const evaluateCandidateGpl = recipeGpl => {
        const recipePercent = recipeGplToPercent(recipeGpl);
        const evaluation = evaluateRecipe(recipePercent, targetColor, { totalGramsPerLiter: CANDIDATE_SEARCH_POLICY.totalGpl });
        return {
          score: candidateMetricScore(evaluation, Object.keys(recipeGpl).length),
          dE: evaluation.dE,
          hidingAlpha: evaluation.double.alpha,
          modelSpread: evaluation.modelSpread,
          substrateShift: evaluation.substrateShift,
          referenceTrust: evaluation.referenceTrust,
          provenance: LEGACY_SCREENING_PROVENANCE,
          grade: evaluation.grade
        };
      };
      const searchResults = RecipeSearch.searchCandidates({
        catalog,
        seeds,
        policy: CANDIDATE_SEARCH_POLICY,
        maxSupports: CANDIDATE_SEARCH_BOUNDS.maxSupports,
        maxRefinementSteps: CANDIDATE_SEARCH_BOUNDS.maxRefinementSteps,
        evaluate: evaluateCandidateGpl
      });
      const constrainedProposalCandidates = [objectiveSeedProposal, twoCoatSeedProposal]
        .filter(proposal => proposal.recipe)
        .map(proposal => {
          const recipe = RecipeSearch.canonicalizeDoseRecipe(recipePercentToGpl(proposal.recipe), CANDIDATE_SEARCH_POLICY);
          const metrics = evaluateCandidateGpl(recipe);
          return {
            recipe,
            supportKey: RecipeSearch.recipeSupportKey(recipe),
            metrics,
            score: metrics.score,
            modelOnly: true
          };
        });
      return RecipeSearch.selectDiverseCandidates([...searchResults, ...constrainedProposalCandidates], CANDIDATE_SEARCH_POLICY)
        .sort(compareRecommendedCandidates)
        .map((candidate, index) => {
          const recipeGpl = { ...candidate.recipe };
          return {
            ...candidate,
            id: `model-candidate-${index + 1}`,
            recipeGpl,
            recipePercent: recipeGplToPercent(recipeGpl),
            activeCount: Object.keys(recipeGpl).length,
            minDoseGpl: Math.min(...Object.values(recipeGpl))
          };
        });
    }

    preparePigments();
    deepFreeze(catalog);

    return Object.freeze({
      catalog,
      provenance: LEGACY_SCREENING_PROVENANCE,
      constants: Object.freeze({ TOTAL_PIGMENT_PER_LITER, CANDIDATE_SEARCH_POLICY, CANDIDATE_SEARCH_BOUNDS, BLACK_SUBSTRATE_RGB, WHITE_SUBSTRATE_RGB }),
      preparePigments,
      resolvePaintCode,
      getRecipeEntries,
      simulateMix,
      calculateHidingAlpha,
      simulateOverSubstrate,
      resolveTargetColor,
      evaluateRecipe,
      normalizeRecipe,
      cleanRecipe,
      recipePercentToGpl,
      recipeGplToPercent,
      objectiveForRecipe,
      candidateMetricScore,
      candidateFeasibilityTier,
      compareRecommendedCandidates,
      buildSeedRecipes,
      refineObjectiveSeedProposal,
      refineSeedProposal,
      generateCandidates
    });
  }

  return Object.freeze({ create });
}));
