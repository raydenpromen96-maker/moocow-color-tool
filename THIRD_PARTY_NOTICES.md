# Third-party data notices

## QTC RAL Classic screen-reference data

`data/qtc-ral-classic.json` and `src/qtc-ral-classic.js` contain a local
snapshot of 216 computer-simulated RAL Classic screen references published by
QTC Color (Shenzhen Qiantong Color Management Co., Ltd.).

- Catalogue page: https://m.qtccolor.com/mshop/#/pages/color/dir?articleId=70
- Directory endpoint: https://m.qtccolor.com/webapi/color/GetDirJSONByArticile?id=70
- Detail endpoint pattern: https://m.qtccolor.com/webapi/color/getColor?colorId={id}&isUpdateVol=0
- Bundled values: code, Chinese/English name, HEX, RGB, Lab, QTC colour ID and index
- Rebuild command: `npm run sync:qtc-ral`

The user reports direct telephone confirmation from the RAL Asia-Pacific
business manager that this customer colour-selection use is allowed and
identifies QTC as an authorised presentation source. That user-provided
authorization is the basis for this integration.

QTC states that the displayed colours and values are computer simulations,
that different devices may display them differently, and that production work
must be confirmed against the latest physical colour card. These records are
therefore target/display references only. They are not measured reflectance
spectra, do not replace a physical colour card, and are not relicensed as
measurements made by MooCow.

## GOLDEN water-based acrylic spectral data

`src/family-spectra.js` contains 30 nm samples selected from the spreadsheet
**Reflectance Data for Golden HB 10 mil Drawdowns over White**, supplied by
Golden Artist Colors, Inc. and shared by Andrew Glassner and Eric Haines.

- Data page and sharing statement: https://www.realtimerendering.com/golden.html
- Provider: Golden Artist Colors, Inc.
- Source ZIP SHA-256: `AF3F8B0C327DD4DCFF52BAC83A5ED7E9C80D82D2E56164518DDF1C9AA57D3835`
- Extracted XLSX SHA-256: `584A38368C4AF637A1253B6465B9F71493E38C65340092A0CFE9F73B3ED227CF`
- Source grid: 400-700 nm at 10 nm intervals
- Bundled grid: 400-700 nm at 30 nm intervals
- Bundled values: reflectance fraction and dimensionless single-constant K/S

The reproducible row/column mapping, every bundled sample, and every full-profile
SHA-256 are recorded in `data/golden-family-spectra-manifest.json`. Given a
lawfully obtained copy of the source workbook, verify the complete extraction
with:

```text
python scripts/verify-golden-family-spectra.py "path/to/Reflectance Data for Golden HB 10 mil Drawdowns over White.xlsx"
```

The data page states that Golden supplied spectral data for its acrylic paints
and allowed the hosts to share the data with others. It does not publish a
named data licence or an explicit commercial redistribution grant. These
numeric data remain attributed to their provider, are not relicensed under the
project's MIT code licence, and are not claimed as measurements made by
MooCow. Users who need broader rights should obtain permission from Golden or
the data rightsholder.

### Measurement conditions and limits

The source samples are water-based Golden Heavy Body acrylic paints drawn down
at 10 mil wet thickness and measured at approximately 6 mil dry thickness over
a white Leneta card. The source reports D65 illumination, a 10-degree observer,
and 10 nm measurements from 400 to 700 nm.

Golden warns that the films are not all truly opaque and that the white backing
affects transparent colors. The supplied K/S values are single-constant values
derived from measured reflectance over white, not a fully calibrated
two-constant K and S characterization over black and white.

The application uses only exact C.I. matches and treats these values as a
waterborne-acrylic family reference. They do not represent the current
Clariant/Heubach CN batches, do not prove hiding over black, and do not affect
candidate scoring or ranking. Unsupported or unverified C.I. identities fail
closed and never inherit a visually similar curve.

## Removed epoxy dataset

The v4.2.0 release briefly bundled samples from `MultipigmentPhantoms`. Those
pigment-in-epoxy `mu_a` and `mu_s'` arrays were removed in v4.2.1 because the
matrix is incompatible with this application's waterborne acrylic reference
layer. The current project retains no runtime numeric data from that source.

Historical source: https://github.com/AlecWalter/MultipigmentPhantoms

## Research-only external references

The RIT/IS&T paper remains an external method reference; no values from its
separate dataset are redistributed here:
https://doi.org/10.2352/issn.2168-3204.2022.19.1.10

The Colanyl Green GG 131-TH / PG7 paper is also referenced externally, but no
values are digitized because it publishes a plotted curve rather than a raw
numeric table:
https://www.scienceasia.org/2020.46S.n1/scias46S_110.pdf
