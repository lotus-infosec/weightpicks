# Third-party notices

WeightPicks itself is MIT-licensed (see `LICENSE`). It ships or runs the following third-party software under their own licenses.

## Vendored front-end libraries (`app/web/static/vendor/`)

Committed unmodified so the app needs no CDN; versions and checksums in `app/web/static/vendor/VERSIONS.md`.

| Library | Version | License | Source |
| --- | --- | --- | --- |
| htmx | 2.0.11 | 0BSD | https://github.com/bigskysoftware/htmx |
| Alpine.js (CSP build) | 3.17.4 | MIT | https://github.com/alpinejs/alpine |
| Chart.js | 4.5.1 | MIT | https://github.com/chartjs/Chart.js |

## Python dependencies of the app

Installed from `uv.lock` into the image's `/opt/venv` (FastAPI, Starlette, SQLAlchemy, Alembic, Pydantic, NumPy, SciPy, Pillow, cryptography, argon2-cffi, structlog, uvicorn, httpx, Jinja2 and their dependencies). All are under permissive licenses (MIT, BSD, Apache-2.0, PSF, ISC, MPL-2.0 for `certifi`). Each package's license is in its `*.dist-info` folder inside the image.

## GarminDB (separate program, GPL-2.0)

The worker downloads Garmin data by running [GarminDB](https://github.com/tcgoetz/GarminDB) as a **separate program** in its own virtual environment (`/opt/garmindb` in the image). WeightPicks does not import or link it; it runs it as a subprocess and reads the files it writes.

GarminDB and some of its dependencies are licensed **GPL-2.0**:

| Package | License | Source |
| --- | --- | --- |
| GarminDb | GPL-2.0 | https://github.com/tcgoetz/GarminDB |
| fitfile | GPL-2.0 | https://github.com/tcgoetz/Fit |
| idbutils | GPL-2.0 | https://github.com/tcgoetz/utilities |
| tcxfile | GPL-2.0 | https://github.com/tcgoetz/Tcx |

The others in that environment (garminconnect, requests, SQLAlchemy, tqdm, curl_cffi, and so on) are MIT, BSD, Apache-2.0 or MPL-2.0. The exact versions, with hashes, are in `docker/garmindb-requirements.txt`. Their unmodified source is on PyPI and at the links above; the container images we publish include them unmodified. To get the corresponding source of any GPL component in a published image, download that exact version from PyPI, or open an issue and we'll provide it.

## Container base images

The published images are built on `python:3.12-slim-bookworm` (Debian 12). Debian packages keep their own licenses; see `/usr/share/doc/*/copyright` inside the image. The stylesheet is compiled at build time with the Tailwind CSS standalone CLI (MIT); the CLI itself is not shipped.
