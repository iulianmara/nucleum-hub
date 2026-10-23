# Third-party notices

Components written by others that are used by or shipped with Wall Dashboard, and their licences.
Every entry must be updated before a release that adds, removes or upgrades a component.

| Component | Used as | Licence | Source |
|---|---|---|---|
| Bricolage Grotesque (variable, latin + latin-ext) | display typeface, bundled | SIL Open Font License 1.1 | https://github.com/ateliertriay/bricolage, via npm `@fontsource-variable/bricolage-grotesque@5.3.0` |
| Schibsted Grotesk (variable, latin + latin-ext) | UI typeface, bundled | SIL Open Font License 1.1 | https://github.com/schibsted/schibsted-grotesk, via npm `@fontsource-variable/schibsted-grotesk@5.3.0` |

The font files and their OFL texts are in `assets/fonts/`. The build embeds the fonts in the
shipped HTML, with a notice naming the licence; the OFL permits bundling and sale with software as
long as the licence text travels with the fonts. So every place that ships the fonts ships
`assets/fonts/OFL-*.txt` too:
- the Nucleum Hub package, both editions and the HACS zip: `custom_components/wall_dashboard/third-party/`
  (`third-party/` at the root of the HACS zip), with this file;
- the website: `/assets/fonts/`, next to the font files;
- the online demo: `third-party/`, next to its `index.html`.

## The Home Assistant integration (`integration/`)

The integration (domain `wall_dashboard`, shown as "Nucleum Hub") ships only our own Python code. It
names one requirement in `manifest.json`, which **Home Assistant itself installs from PyPI on the
customer's machine** when the integration is first set up; we do not ship, bundle or redistribute it.

| Component | Used as | Licence | Source |
|---|---|---|---|
| pillow-heif 1.8.0 (22.09.2026) | Python package, a requirement in `manifest.json`: decodes HEIC / HEIF covers (D-050) | BSD-3-Clause | https://pypi.org/project/pillow-heif/ , https://github.com/bigcat88/pillow_heif |
| libheif 1.23.4 (inside pillow-heif's binary wheels) | HEIF decoding, bundled by pillow-heif | LGPL-3.0 | https://github.com/strukturag/libheif |
| libde265 1.1.3 (inside pillow-heif's binary wheels) | HEVC decoding, bundled by pillow-heif | LGPL-3.0 | https://github.com/strukturag/libde265 |
| x265 (inside pillow-heif's binary wheels) | HEVC encoding, bundled by pillow-heif; our code never encodes HEIC | GPL-2.0 | https://bitbucket.org/multicoreware/x265_git |
| Pillow | imaging library: decoding, resizing, JPEG output | MIT-CMU (HPND) | ships with Home Assistant core; not listed in our requirements |

The bundled libraries and their licences are as pillow-heif lists them in its `LICENSES_bundled.txt`
(read 10.10.2026; on Windows its wheels also carry the MinGW-w64 runtime, GPL-3.0 with the GCC
exception, MIT and BSD). [AV] To confirm with the lawyer before the first sale: that naming pillow-heif
as a requirement, which the customer's Home Assistant downloads from PyPI, puts no obligation on us
beyond this notice, given that its wheels carry x265 under GPL-2.0.

## To verify before the first sale
- Origin and rights of the default images embedded in the source (car, robot vacuum, lawn mower,
  air purifier and media artwork, `WD_IMG`). Manufacturer product photos cannot be shipped in a
  paid product without permission.
