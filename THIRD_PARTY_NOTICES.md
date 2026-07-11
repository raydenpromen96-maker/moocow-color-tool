# Third-party data notices

## Runtime spectral-data status

This public release does not bundle third-party measured pigment-family
spectral arrays. The application's existing `REFERENCE_SPECTRA` values are
screen-model approximations, not measured reflectance factors or production
Kubelka-Munk coefficients.

## Removed epoxy dataset

The v4.2.0 release briefly bundled 30 nm samples from
`MultipigmentPhantoms`. Those pigment-in-epoxy absorption (`mu_a`) and reduced
scattering (`mu_s'`) arrays were removed in v4.2.1 because their matrix is not
compatible with the waterborne acrylic clear-base system used by MooCow.

- Historical source: https://github.com/AlecWalter/MultipigmentPhantoms
- Runtime data bundled in v4.2.1: no
- Eligible as waterborne acrylic evidence: no

## Waterborne acrylic research candidates

### GOLDEN Paint Spectra

The strongest accessible technical near-match is a spreadsheet of GOLDEN
Heavy Body water-based acrylic drawdowns:

- Data page: https://www.realtimerendering.com/golden.html
- Contents: reflectance and single-constant K/S, 400-700 nm at 10 nm
- Conditions: 10 mil wet / approximately 6 mil dry over white Leneta card,
  D65, 10-degree observer
- Important limitation: the white backing affects transparent colors and the
  films are not all truly opaque

The page says Golden allowed the hosts to share the data with others, but it
does not publish a named licence or an explicit commercial redistribution and
derivative-use grant. The public MooCow project therefore does not copy or
transform its numeric arrays. Written permission is required before ingestion.

### RIT / IS&T artist acrylic dataset

The RIT/IS&T work is a useful measurement and two-constant Kubelka-Munk method
reference, but its downloadable numeric dataset has no explicit commercial
redistribution licence in the materials reviewed for this release. No values
from it are bundled.

- Paper: https://doi.org/10.2352/issn.2168-3204.2022.19.1.10
- Runtime data bundled in v4.2.1: no

### Colanyl Green GG 131-TH / PG7

This paper is retained as a product-family research reference. It publishes a
plotted curve rather than a raw numeric table, so no values are digitized or
bundled:

https://www.scienceasia.org/2020.46S.n1/scias46S_110.pdf

## Admission gate for future data

A measured family dataset may enter runtime only when all of these are known:

1. explicit commercial redistribution and derivative-use permission;
2. waterborne acrylic binder/matrix and exact C.I. identity;
3. pigment concentration, substrate, wet/dry thickness, and opacity state;
4. instrument geometry, illuminant, observer, wavelength grid, and specular
   condition; and
5. separation from candidate ranking until local drawdown validation proves
   that the source transfers to the current CN batches.
