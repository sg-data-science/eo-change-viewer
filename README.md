# change-viewer

A local Dash web app for checking polygons for change with Sentinel-2
imagery. Polygons come from a GeoServer layer (WMS for display, WFS for
queries). For a selected polygon, the app searches the CEDA Sentinel-2 ARD
archive in up to three one-month windows, using
[`ceda_sentinel`](https://github.com/tomwilsonsco/ceda_sentinel). It shows the
clearest image in each window as an RGB chip, side by side. A manual verdict
can be saved per polygon, together with the chosen images, which are exported
as GeoTIFFs.

## Setup

Development uses the dev container in `.devcontainer/` (Python 3.14, Debian
trixie, system GDAL). Runtime dependencies are in
`.devcontainer/requirements.txt` and dev tools in
`.devcontainer/requirements-dev.txt`.

### Windows, without Docker

`environment.yml` builds a conda environment with the same packages. GDAL
and the other compiled libraries come from conda-forge, so nothing needs
building.

1. Install [Miniforge](https://conda-forge.org/download/). It uses
   conda-forge by default. Anaconda or Miniconda also work.
2. Open **Miniforge Prompt** in the project folder and run:

   ```bat
   conda env create -f environment.yml
   conda activate change-viewer
   ```

   pip installs `ceda-sentinel` from GitHub with the `git` that's in the
   environment, so Git for Windows isn't needed.
3. Create `.env` as described below. If you use Notepad, check that it
   doesn't save the file as `.env.txt`.
4. Run the app from the activated prompt:

   ```bat
   python -m change_viewer.app --host 127.0.0.1
   ```

   Open http://localhost:8050. Without `--host 127.0.0.1`, the app listens on
   every network interface, and Windows Firewall asks whether to allow it.

After `environment.yml` changes, update with
`conda env update -f environment.yml --prune`.

On Windows, a file that another program has open can't be replaced or
deleted. Close `output/reviews.gpkg` and any exported chips in QGIS, or
other programs, before saving a review. Otherwise the save fails.

The Playwright browser checks under Development use the dev container's
Chromium and aren't set up in the conda environment.

### Settings

Create `.env` in the project root with the full WFS URL, including its
`typeName` and `authkey` parameters:

```
PLANNING_POLY_WFS=https://<host>/geoserver/<workspace>/wfs?service=WFS&typeName=<workspace>:<layer>&authkey=<key>
```

`.env` is gitignored. The URL and key stay on the server: they aren't logged,
put in error messages or sent to the browser. WMS tiles go through a
server-side proxy at `/wms`, which accepts only bbox, size, year and
development type.

## Run

```bash
python -m change_viewer.app            # http://localhost:8050
python -m change_viewer.app --port 8051 --debug
```

## Using the app

- **Select a polygon:** click it on the map, search by reference, address or
  key, or step with **Next** / **Previous**. The step list is every polygon
  for the year and development type shown, sorted by council then reference.
  **Skip reviewed** passes over polygons that already have a saved review.
- **Find images:** the three windows are pre-filled from the application
  dates: June of the year before receipt, then June one and two years after
  the decision (or after receipt if there's no decision). Each window's
  month and year can be changed with its dropdowns. Each window is searched
  in its own callback, in parallel.
- **Panels:** each window's image shows in a small map. The three maps are
  linked, so panning or zooming one moves the others. **Link to main map**
  links the main map too. Each image can also be turned on as an overlay on
  the main map. When more than one image passes the checks, a dropdown
  switches between them.
- **Review:** compare each later image with **before** and choose:
  - **Change – after:** change shows by the after image.
  - **Change – later only:** no change by the after image, but change by
    the later one.
  - **No change:** no change in any image shown.
  - **Unsure.**

  Add an optional note, and press **Save** or **Save and next**. A verdict
  can't be saved without the images it compares, e.g. "Change – later
  only" needs the before, after and later images; choose Unsure if one is
  missing. Each window's month and image date are saved with the verdict. Next and Previous don't save; the
  status line warns while there are unsaved changes. Reopening a reviewed
  polygon restores its verdict, note, windows and chosen images.

## Image search

- The search uses `FindS2STAC` first. It falls back to `FindS2`
  (HTML scraping) only when STAC has no items for the window, because STAC
  doesn't index older dates. The fallback is slow, and is retried once after
  a connection error.
- An image passes when less than the max cloud % of the polygon, plus a
  buffer, is cloud (s2cloudless).
- **Selection rule:** least cloud first, then the earliest date, then the
  image link.
- **Display:** bands 4, 3 and 2 (red, green, blue) are each clipped to
  0–150, scaled to 0–255, and warped to EPSG:3857.
- If every image read fails, the app reports "CEDA unavailable" rather than
  "no image".
- Windows that start in the future, or before 2015, aren't searched.

## Limits and settings

Settings are in `change_viewer/config.py`.

| Setting | Value | Meaning |
| --- | --- | --- |
| `MAX_SITE_SIZE_M` | 10 000 | Polygons whose bounding box is longer than this (metres, EPSG:27700) on either side aren't searched. Find shows a message instead. Chips grow with the polygon, so very large polygons would mean very large reads, exports and browser images, and would cross Sentinel-2 tiles. |
| `S2CLOUDLESS_MAX` | 10 | Default max cloud % over the polygon. It can be changed in the UI. |
| `CHECK_BUFFER_M` | 100 | Buffer around the polygon included in the cloud check. |
| `TILE_CLOUD_MAX` | 100 | `ceda_sentinel`'s whole-tile cloud filter. A value of 100 turns it off; the default of 20 rejected images that were clear over the polygon. |
| `CHIP_BUFFER_M` | 1000 | Buffer around the polygon's bounds for chips and exports. The chip area is made square and snapped to the 10 m grid. |
| `RGB_CLIP` | (0, 150) | Display stretch for each band. |
| `S2_FIRST_YEAR` | 2015 | No search before this year. |
| `DEFAULT_WINDOW_MONTH` | 6 | Month used for the default windows. |
| `DEFAULT_YEAR`, `DEFAULT_DEV_TYPE` | 2022, `housing` | Initial map filter. |

The layer is expected to have a `geom` geometry field in EPSG:27700. Every
query filters on `year` first. The fields used are `keyval` (unique ID),
`reference`, `address`, `local_auth`, `dev_desc_is`, `date_recei`,
`date_decis` and `url`.

## Files written

| Path | Contents |
| --- | --- |
| `cache/<keyval>/` | Search results (JSON) and display PNGs. Safe to delete. A search is cached only once its window has ended. |
| `output/reviews.gpkg` | Layer `reviews`: one row per polygon, with geometry in EPSG:27700, `verdict` (`change after`, `change later`, `no change` or `unsure`), `note`, `max_cloud` and `saved_at`. For each window (`before`, `after`, `later`) it has `_month`, `_image_date`, `_cloud`, `_image_id`, `_image_link` and `_chip`. |
| `output/chips/<keyval>/` | For each chosen image: `<window>_<image_id>_b<buffer>.tif` (every band, values unchanged from the source) and a `.png` with the polygon outline. |

`cache/` and `output/` are gitignored. `output/` holds the saved reviews, so
back it up.

## Development

```bash
black --check .
ruff check .
pytest                  # unit tests, no network
pytest -m integration   # needs .env and network access to GeoServer and CEDA
```

Browser checks use Playwright with the system Chromium:
`p.chromium.launch(executable_path="/usr/bin/chromium")`.
