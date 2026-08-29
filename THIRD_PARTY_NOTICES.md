# Third-party notices

## CPython 3.14.5

The macOS application bundles the CPython 3.14.5 runtime. Python is distributed under the
Python Software Foundation License Version 2 together with the historical component licenses.
The exact runtime license is distributed in `licenses/python/LICENSE.txt`.

## cryptography 50.0.0

The portable backup feature uses `cryptography` for AES-256-GCM.
The installed package declares `Apache-2.0 OR BSD-3-Clause`.
The corresponding notices are distributed in `licenses/cryptography/`:

- `LICENSE`
- `LICENSE.APACHE`
- `LICENSE.BSD`

Release builds must include these files together with the bundled library.

## cffi 2.0.0

`cryptography` declares `cffi` as a runtime dependency on CPython. It is MIT licensed.
The package license and author notice are distributed in `licenses/cffi/`.

## pycparser 3.0

`cffi` uses `pycparser`. It is BSD-3-Clause licensed.
The package license is distributed in `licenses/pycparser/`.

## openpyxl 3.1.5

Excel (`.xlsx`) output uses `openpyxl`, licensed under the MIT License.
The package license is distributed in `licenses/openpyxl/`.

## Pillow 12.1.1

Excel image embedding uses Pillow. Pillow and its bundled codec notices are distributed
under the terms reproduced in `licenses/Pillow/LICENSE`.

## python-docx 1.2.0

Word (`.docx`) output uses `python-docx`, licensed under the MIT License.
The package license is distributed in `licenses/python-docx/`.

## lxml 6.1.1

`python-docx` uses `lxml`. The installed distribution declares BSD-3-Clause and includes
the applicable license texts in `licenses/lxml/`.

## et-xmlfile 2.0.0

`openpyxl` uses `et-xmlfile`. Its MIT license, Python-derived component license, and author
notice are distributed in `licenses/et-xmlfile/`.

## typing-extensions 4.15.0

`python-docx` uses `typing-extensions`. The installed distribution declares PSF-2.0 and its
license is distributed in `licenses/typing-extensions/`.

## PyInstaller 6.21.0

This notice belongs only to the retained legacy native-app build tooling. PyInstaller is not
part of the canonical local-Web source ZIP. When that legacy tool is used, it is GPL-2.0-or-later with its
bootloader exception for distributing programs, including commercial programs. The exact
installed distribution's `COPYING.txt` is collected into `licenses/pyinstaller/` at build time.
PyInstaller is a build-only dependency pinned in `requirements-build.txt`.
