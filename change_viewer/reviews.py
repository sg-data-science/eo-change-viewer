"""Manual verdicts per site, saved with the chosen images, and chip exports.

Reviews are kept in one GeoPackage layer, one row per site, with the site
polygon in EPSG:27700. The columns are flat (`before_month`,
`before_image_date`, ...) so the file opens directly in QGIS or pandas and can
serve as labels for later modelling.

Saving a review also exports each chosen image: every band of the chip area
as a GeoTIFF, plus the panel PNG with the site outline drawn on.
"""

import math
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
from rasterio.errors import RasterioIOError

from change_viewer import config, render
from change_viewer.cache import cached_chip, safe_name
from change_viewer.imagery import Candidate

VERDICTS = {"change": "Change", "no change": "No change", "unsure": "Unsure"}
LAYER = "reviews"
# Site attributes copied into each review.
SITE_FIELDS = ("keyval", "year", "reference", "local_auth", "dev_desc_is")
WINDOW_FIELDS = ("month", "image_date", "cloud", "image_id", "image_link", "chip")
COLUMNS = [
    *SITE_FIELDS,
    "is_placeholder",
    "verdict",
    "note",
    "max_cloud",
    "saved_at",
    *(f"{name}_{f}" for name in config.WINDOW_NAMES for f in WINDOW_FIELDS),
]


@dataclass(frozen=True)
class WindowChoice:
    """One window as reviewed: its month, and the image chosen if any."""

    name: str
    month: str | None
    candidate: Candidate | None = None


def _clean(value):
    """None for missing values read back from the GeoPackage."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def review_record(props, verdict, note, max_cloud, choices, chips, saved_at=None):
    """A review as a flat dict of `COLUMNS`.

    `props` are the site's attributes, `choices` a `WindowChoice` per window,
    and `chips` the exported GeoTIFF paths by window name.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict: {verdict!r}")
    record = {field: props.get(field) for field in SITE_FIELDS}
    record.update(
        is_placeholder=bool(props.get("is_placeholder")),
        verdict=verdict,
        note=(note or "").strip() or None,
        max_cloud=float(max_cloud),
        saved_at=(saved_at or datetime.now()).isoformat(timespec="seconds"),
    )
    for choice in choices:
        c = choice.candidate
        record.update(
            {
                f"{choice.name}_month": choice.month,
                f"{choice.name}_image_date": c.image_date if c else None,
                f"{choice.name}_cloud": c.cloud if c else None,
                f"{choice.name}_image_id": c.image_id if c else None,
                f"{choice.name}_image_link": c.image_link if c else None,
                f"{choice.name}_chip": chips.get(choice.name),
            }
        )
    return {column: record.get(column) for column in COLUMNS}


def saved_windows(record):
    """The month and image link saved for each window, in window order."""
    return [
        (record.get(f"{name}_month"), record.get(f"{name}_image_link"))
        for name in config.WINDOW_NAMES
    ]


class ReviewStore:
    """Reads and writes the reviews GeoPackage.

    Writes replace the whole file atomically, under a lock, since Dash runs
    callbacks in parallel threads. Reads are cached until the file changes.
    """

    def __init__(self, path=config.REVIEWS_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cached = (None, None)

    def all(self):
        """Every review as a GeoDataFrame in EPSG:27700."""
        if not self.path.exists():
            return gpd.GeoDataFrame(
                {c: [] for c in COLUMNS}, geometry=[], crs=config.NATIVE_CRS
            )
        mtime = self.path.stat().st_mtime_ns
        if self._cached[0] != mtime:
            self._cached = (mtime, gpd.read_file(self.path, layer=LAYER))
        return self._cached[1]

    def get(self, keyval):
        """The review of one site as a dict, or None if it hasn't one."""
        gdf = self.all()
        rows = gdf[gdf["keyval"] == keyval]
        if rows.empty:
            return None
        row = rows.iloc[0]
        return {c: _clean(row[c]) for c in COLUMNS}

    def reviewed(self):
        """Keyvals of every reviewed site."""
        return set(self.all()["keyval"])

    def save(self, record, geom):
        """Add or replace the review of `record["keyval"]`."""
        new = gpd.GeoDataFrame([record], geometry=[geom], crs=config.NATIVE_CRS)
        with self._lock:
            old = self.all()
            old = old[old["keyval"] != record["keyval"]]
            frames = [new] if old.empty else [old, new]
            gdf = gpd.GeoDataFrame(
                pd.concat(frames, ignore_index=True),
                geometry="geometry",
                crs=config.NATIVE_CRS,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self.path.parent, prefix=".tmp-", suffix=".gpkg"
            )
            os.close(fd)
            os.remove(tmp)  # the GeoPackage driver creates the file itself
            try:
                gdf[[*COLUMNS, "geometry"]].to_file(tmp, layer=LAYER, driver="GPKG")
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)


def chip_stem(choice):
    return (
        f"{choice.name}_{safe_name(choice.candidate.image_id)}"
        f"_b{config.CHIP_BUFFER_M}"
    )


def export_site(cache, keyval, choices, geom, export_dir=config.EXPORT_DIR):
    """Export the chosen image of each window to `export_dir/<keyval>/`.

    Returns (chip paths by window name, relative to the output folder; names
    of windows whose image couldn't be read). Files already exported are
    reused, and files from an earlier choice for this site are removed.
    """
    export_dir = Path(export_dir)
    site_dir = export_dir / safe_name(keyval)
    site_dir.mkdir(parents=True, exist_ok=True)
    chips, failed, keep = {}, [], set()
    for choice in choices:
        if choice.candidate is None:
            continue
        stem = chip_stem(choice)
        tif, png = site_dir / f"{stem}.tif", site_dir / f"{stem}.png"
        try:
            if not tif.exists():
                fd, tmp = tempfile.mkstemp(dir=site_dir, prefix=".tmp-", suffix=".tif")
                os.close(fd)
                try:
                    render.export_bands(choice.candidate.image_link, geom, tmp)
                    os.replace(tmp, tif)
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)
            if not png.exists():
                panel_url = cached_chip(cache, keyval, choice.candidate, geom)[0]
                shutil.copyfile(cache.path(keyval, panel_url.rsplit("/", 1)[1]), png)
        except RasterioIOError:
            failed.append(choice.name)
            continue
        keep.update({tif.name, png.name})
        chips[choice.name] = tif.relative_to(export_dir.parent).as_posix()
    for path in site_dir.iterdir():
        if path.suffix in (".tif", ".png") and path.name not in keep:
            path.unlink()
    return chips, failed
