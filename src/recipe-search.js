(function (root, factory) {
  const recipeSearch = factory();

  if (typeof module === 'object' && module.exports) {
    module.exports = recipeSearch;
  }

  if (root) {
    root.MooCowRecipeSearch = recipeSearch;
  }
}(typeof window !== 'undefined' ? window : typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const DEFAULT_POLICY = Object.freeze({
    totalGpl: 106,
    gridGpl: 0.5,
    minActiveGpl: 1.0,
    maxActive: 4,
    candidateCount: 3,
    minSupportDistance: 0.25
  });

  function compareText(left, right) {
    return left === right ? 0 : left < right ? -1 : 1;
  }

  function resolvePolicy(policy) {
    const resolved = { ...DEFAULT_POLICY, ...(policy || {}) };
    const totalCells = resolved.totalGpl / resolved.gridGpl;
    const minCells = resolved.minActiveGpl / resolved.gridGpl;
    const minSupportDistance = Number(resolved.minSupportDistance);

    if (!Number.isFinite(resolved.totalGpl) || !Number.isFinite(resolved.gridGpl)
      || !Number.isFinite(resolved.minActiveGpl) || !Number.isInteger(resolved.maxActive)
      || resolved.totalGpl <= 0 || resolved.gridGpl <= 0 || resolved.minActiveGpl <= 0
      || resolved.maxActive < 1 || !Number.isInteger(totalCells) || !Number.isInteger(minCells)
      || minCells > totalCells || !Number.isFinite(minSupportDistance)
      || minSupportDistance < 0 || minSupportDistance > 1) {
      throw new RangeError('recipe search policy must use positive whole grid cells');
    }

    return {
      ...resolved,
      totalCells,
      minCells,
      minSupportDistance,
      candidateCount: Math.max(1, Math.floor(Number(resolved.candidateCount) || DEFAULT_POLICY.candidateCount))
    };
  }

  function recipeEntries(recipe) {
    if (recipe instanceof Map) return Array.from(recipe.entries());
    if (Array.isArray(recipe)) {
      return recipe.map(entry => Array.isArray(entry)
        ? entry
        : [entry && (entry.code || entry.id || entry.key), entry && (entry.gpl ?? entry.amount ?? entry.value)]);
    }
    if (!recipe || typeof recipe !== 'object') return [];
    return Object.entries(recipe);
  }

  function positiveEntries(recipe, allowedCodes) {
    const merged = new Map();

    recipeEntries(recipe).forEach(([rawCode, rawDose]) => {
      const code = String(rawCode || '');
      const dose = Number(rawDose);
      if (!code || !Number.isFinite(dose) || dose <= 0 || (allowedCodes && !allowedCodes.has(code))) return;
      merged.set(code, (merged.get(code) || 0) + dose);
    });

    return Array.from(merged, ([code, dose]) => ({ code, dose }))
      .sort((left, right) => compareText(left.code, right.code));
  }

  function recipeKey(recipe) {
    return positiveEntries(recipe)
      .map(({ code, dose }) => `${code}:${dose}`)
      .join('|');
  }

  function canonicalizeDoseRecipe(recipe, policy) {
    const resolved = resolvePolicy(policy);
    const entries = positiveEntries(recipe)
      .sort((left, right) => right.dose - left.dose || compareText(left.code, right.code))
      .slice(0, resolved.maxActive);

    if (!entries.length) return {};

    const availableCells = resolved.totalCells - entries.length * resolved.minCells;
    const totalDose = entries.reduce((sum, entry) => sum + entry.dose, 0);
    const allocations = entries.map(entry => {
      const exact = availableCells * entry.dose / totalDose;
      const whole = Math.floor(exact);
      return { ...entry, cells: resolved.minCells + whole, remainder: exact - whole };
    });
    let unallocated = availableCells - allocations.reduce((sum, entry) => sum + entry.cells - resolved.minCells, 0);

    allocations
      .slice()
      .sort((left, right) => right.remainder - left.remainder || compareText(left.code, right.code))
      .forEach(entry => {
        if (unallocated > 0) {
          allocations.find(allocation => allocation.code === entry.code).cells += 1;
          unallocated -= 1;
        }
      });

    return Object.fromEntries(allocations
      .sort((left, right) => compareText(left.code, right.code))
      .map(({ code, cells }) => [code, cells * resolved.gridGpl]));
  }

  function recipeSupportKey(recipe) {
    return positiveEntries(recipe).map(({ code }) => code).join('|');
  }

  function supportDistance(left, right) {
    const leftCodes = new Set(recipeSupportKey(left && left.recipe ? left.recipe : left).split('|').filter(Boolean));
    const rightCodes = new Set(recipeSupportKey(right && right.recipe ? right.recipe : right).split('|').filter(Boolean));
    const union = new Set([...leftCodes, ...rightCodes]);
    let overlap = 0;

    leftCodes.forEach(code => {
      if (rightCodes.has(code)) overlap += 1;
    });

    return union.size ? 1 - overlap / union.size : 0;
  }

  function metricScore(metrics) {
    if (Number.isFinite(metrics)) return metrics;
    if (!metrics || typeof metrics !== 'object') return 0;

    for (const key of ['score', 'objective', 'loss', 'dE', 'deltaE']) {
      if (Number.isFinite(metrics[key])) return metrics[key];
    }
    return 0;
  }

  function candidateScore(candidate) {
    return Number.isFinite(candidate && candidate.score) ? candidate.score : metricScore(candidate && candidate.metrics);
  }

  function compareCandidates(left, right) {
    return candidateScore(left) - candidateScore(right)
      || compareText(String(left.supportKey || recipeSupportKey(left.recipe)), String(right.supportKey || recipeSupportKey(right.recipe)))
      || compareText(recipeKey(left.recipe), recipeKey(right.recipe));
  }

  function copyCandidate(candidate) {
    return {
      ...candidate,
      recipe: { ...(candidate.recipe || {}) },
      metrics: candidate.metrics && typeof candidate.metrics === 'object' && !Array.isArray(candidate.metrics)
        ? { ...candidate.metrics }
        : candidate.metrics
    };
  }

  function selectDiverseCandidates(candidates, policy) {
    const resolved = resolvePolicy(typeof policy === 'number' ? { candidateCount: policy } : policy);
    const bestBySupport = new Map();

    (Array.isArray(candidates) ? candidates : []).forEach(candidate => {
      if (!candidate || !candidate.recipe) return;
      const supportKey = candidate.supportKey || recipeSupportKey(candidate.recipe);
      if (!supportKey) return;
      const normalized = { ...candidate, supportKey };
      const current = bestBySupport.get(supportKey);
      if (!current || compareCandidates(normalized, current) < 0) bestBySupport.set(supportKey, normalized);
    });

    const remaining = Array.from(bestBySupport.values()).sort(compareCandidates);
    const selected = [];

    while (remaining.length && selected.length < resolved.candidateCount) {
      const preferredIndex = remaining.findIndex(candidate => selected.every(selectedCandidate =>
        supportDistance(candidate, selectedCandidate) >= resolved.minSupportDistance
      ));
      const nextIndex = preferredIndex === -1 ? 0 : preferredIndex;

      selected.push(copyCandidate(remaining.splice(nextIndex, 1)[0]));
    }

    return selected;
  }

  function catalogCodes(catalog) {
    const codes = new Set();
    const add = value => {
      const code = String(value || '');
      if (code) codes.add(code);
    };

    if (catalog instanceof Map) {
      catalog.forEach((_, code) => add(code));
    } else if (Array.isArray(catalog)) {
      catalog.forEach(item => add(typeof item === 'string' ? item : item && (item.code || item.id || item.key)));
    } else if (catalog && typeof catalog === 'object') {
      Object.keys(catalog).forEach(add);
    }

    return Array.from(codes).sort(compareText);
  }

  function combinations(codes, size, visit, start, chosen) {
    if (chosen.length === size) {
      visit(chosen);
      return;
    }
    for (let index = start; index <= codes.length - (size - chosen.length); index += 1) {
      combinations(codes, size, visit, index + 1, chosen.concat(codes[index]));
    }
  }

  function supportList(codes, seeds, resolved, maxSupports) {
    const allowedCodes = new Set(codes);
    const supports = new Map();
    const seedSupportKeys = new Set();
    const addSupport = (support, isSeed) => {
      const unique = Array.from(new Set(support)).sort(compareText);
      if (!unique.length || unique.length > resolved.maxActive) return;
      const key = unique.join('|');
      supports.set(key, unique);
      if (isSeed) seedSupportKeys.add(key);
    };

    const seedList = Array.isArray(seeds) ? seeds : seeds ? [seeds] : [];
    seedList.forEach(seed => {
      const recipe = seed && seed.recipe ? seed.recipe : seed;
      const constrained = canonicalizeDoseRecipe(
        Object.fromEntries(positiveEntries(recipe, allowedCodes).map(({ code, dose }) => [code, dose])),
        resolved
      );
      addSupport(Object.keys(constrained), true);
    });

    const limit = Math.max(resolved.candidateCount, Math.floor(Number(maxSupports) || resolved.candidateCount * 8));
    for (let size = 1; size <= Math.min(resolved.maxActive, codes.length) && supports.size < limit; size += 1) {
      combinations(codes, size, support => {
        if (supports.size < limit) addSupport(support);
      }, 0, []);
    }

    return Array.from(supports.entries())
      .sort(([leftKey], [rightKey]) => {
        const leftIsSeed = seedSupportKeys.has(leftKey);
        const rightIsSeed = seedSupportKeys.has(rightKey);
        if (leftIsSeed !== rightIsSeed) return leftIsSeed ? -1 : 1;
        return compareText(leftKey, rightKey);
      })
      .slice(0, limit)
      .map(([, support]) => support);
  }

  function canonicalizeOnSupport(recipe, support, resolved) {
    const source = new Map(positiveEntries(recipe).map(({ code, dose }) => [code, dose]));
    return canonicalizeDoseRecipe(Object.fromEntries(support.map(code => [code, source.get(code) || 1])), resolved);
  }

  function evaluateCanonical(recipe, evaluate, catalog) {
    const metrics = evaluate({ ...recipe }, catalog);
    return { recipe, metrics, score: metricScore(metrics) };
  }

  function refineWithinSupport(seed, support, evaluate, catalog, resolved, maxRefinementSteps) {
    let best = evaluateCanonical(canonicalizeOnSupport(seed, support, resolved), evaluate, catalog);
    const limit = Math.max(0, Math.floor(Number(maxRefinementSteps) || resolved.totalCells));

    for (let iteration = 0; iteration < limit; iteration += 1) {
      let next = null;
      support.forEach(from => {
        if (best.recipe[from] <= resolved.minActiveGpl) return;
        support.forEach(to => {
          if (from === to) return;
          const trial = { ...best.recipe, [from]: best.recipe[from] - resolved.gridGpl, [to]: best.recipe[to] + resolved.gridGpl };
          const evaluated = evaluateCanonical(canonicalizeOnSupport(trial, support, resolved), evaluate, catalog);
          if (!next || compareCandidates({ ...evaluated, supportKey: support.join('|') }, { ...next, supportKey: support.join('|') }) < 0) {
            next = evaluated;
          }
        });
      });

      if (!next || next.score >= best.score) break;
      best = next;
    }

    return evaluateCanonical(canonicalizeOnSupport(best.recipe, support, resolved), evaluate, catalog);
  }

  function normalizeSearchArguments(options, seeds, evaluate, policy) {
    if (options && typeof options === 'object' && !Array.isArray(options)
      && ('catalog' in options || 'seeds' in options || 'evaluate' in options)) {
      return options;
    }
    return { catalog: options, seeds, evaluate, policy };
  }

  function searchCandidates(options, seeds, evaluate, policy) {
    const input = normalizeSearchArguments(options, seeds, evaluate, policy);
    if (typeof input.evaluate !== 'function') throw new TypeError('searchCandidates requires an evaluate callback');

    const resolved = resolvePolicy(input.policy);
    const codes = catalogCodes(input.catalog);
    if (!codes.length) return [];

    const allowedCodes = new Set(codes);
    const seedList = Array.isArray(input.seeds) ? input.seeds : input.seeds ? [input.seeds] : [];
    const canonicalSeeds = seedList.map(seed => canonicalizeDoseRecipe(
      Object.fromEntries(positiveEntries(seed && seed.recipe ? seed.recipe : seed, allowedCodes)
        .map(({ code, dose }) => [code, dose])),
      resolved
    ));

    const candidates = supportList(codes, canonicalSeeds, resolved, input.maxSupports).map(support => {
      const supportKey = support.join('|');
      const matchingSeed = canonicalSeeds
        .filter(seed => recipeSupportKey(seed) === supportKey)
        .map(seed => ({ ...evaluateCanonical(seed, input.evaluate, input.catalog), supportKey }))
        .sort(compareCandidates)[0];
      const evaluated = refineWithinSupport(
        matchingSeed ? matchingSeed.recipe : Object.fromEntries(support.map(code => [code, 1])),
        support,
        input.evaluate,
        input.catalog,
        resolved,
        input.maxRefinementSteps
      );

      return {
        recipe: evaluated.recipe,
        supportKey,
        metrics: evaluated.metrics,
        score: evaluated.score,
        modelOnly: true
      };
    });

    return selectDiverseCandidates(candidates, resolved).map(candidate => ({ ...candidate, modelOnly: true }));
  }

  return {
    canonicalizeDoseRecipe,
    recipeSupportKey,
    supportDistance,
    selectDiverseCandidates,
    searchCandidates
  };
}));
