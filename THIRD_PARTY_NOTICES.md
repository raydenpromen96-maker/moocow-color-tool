# Third-party data notices

## MultipigmentPhantoms

`src/family-spectra.js` contains 30 nm samples derived from the normalized
absorption (`mu_a`) and reduced-scattering (`mu_s'`) files published in
**MultipigmentPhantoms**.

- Repository: https://github.com/AlecWalter/MultipigmentPhantoms
- Paper and measurement method: https://doi.org/10.1117/1.JBO.28.2.025002
- License: MIT
- Copyright: Copyright (c) 2022 AlecWalter
- `Absorption.csv` SHA-256: `8424BBFC20AE534D0ED295E82A022F3E4A617AAA5E5A4F9D16A9D8324F653014`
- `ReducedScattering.csv` SHA-256: `6F13699B07CACB43605913F0C92F8E3D855DC8FD20466ED4FD4E7328EFDCF354`

The source values were measured in pigment-in-epoxy phantoms and normalized by
mass fraction. They are not paint reflectance factors, Kubelka-Munk K/S values,
or measurements of the Clariant/Heubach CN batches used by this application.
The application therefore uses them only as attributed, shadow-mode optical
shape evidence. They do not affect candidate ranking.

MIT license text:

```text
MIT License

Copyright (c) 2022 AlecWalter

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Research-only external references

No RIT Artist Paint Spectral Database values are redistributed in this
project because separate upstream redistribution terms were not explicit on
the current RIT page. The paper remains an external method and family-data
reference: https://www.rit.edu/science/sites/rit.edu.science/files/2019-03/ArtistSpectralDatabase.pdf

The Colanyl Green GG 131-TH / PG7 paper is also referenced externally, but no
values are digitized because it publishes a plotted curve rather than a raw
numeric table: https://www.scienceasia.org/2020.46S.n1/scias46S_110.pdf
