# MOOCOW K-M calibration tools

This Python package contains the offline, receipt-gated finite-film
Kubelka-Munk calibration and laboratory-trial recipe commands used by the
MOOCOW color tool.

It does not enable browser ranking, production formulas, sealed-holdout access,
or runtime activation. The open-selection recipe workflow remains restricted to
hash-bound laboratory trials until real current-lot measurements and an
independent measured holdout pass the documented gates.

Operator protocols live under `protocols/`. Start with
`protocols/open-selection-recipe-solver-v1/README.md` for the inverse recipe
request and verification boundary.
