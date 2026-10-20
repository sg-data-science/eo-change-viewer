"""Turn a Sentinel-2 ARD image into RGB chips of one site.

Two PNGs are made from the same read: a panel chip in the image's own grid
(EPSG:27700) with the site outline drawn on, for comparing dates side by side,
and a map overlay warped to EPSG:3857 for Leaflet.
"""

import inspect
import io
import math
from dataclasses import dataclass

import numpy as np
import rasterio as rio
from ceda_s2 import ImagePlotter
from PIL import Image, ImageDraw
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.warp import transform_bounds as warp_bounds
from rasterio.windows import from_bounds
from shapely.geometry import MultiPolygon

from change_viewer import config

# ceda_sentinel's ImagePlotter reads these bands (blue, green and red in the
# ARD files) and reverses them for display. We do the same rather than define
# a band order of our own.
_PLOTTER_BANDS = (
    inspect.signature(ImagePlotter.__init__).parameters["band_indices"].default
)
RGB_BANDS = tuple(reversed(_PLOTTER_BANDS))

PIXEL_SIZE_M = 10
OUTLINE_COLOUR = (0, 229, 255, 255)
WEB_MERCATOR = "EPSG:3857"


@dataclass(frozen=True)
class Chip:
    panel_png: bytes
    overlay_png: bytes
    # Leaflet bounds of the overlay: [[south, west], [north, east]].
    overlay_bounds: list


def chip_bounds(geom, buffer_m=config.CHIP_BUFFER_M, pixel=PIXEL_SIZE_M):
    """A square around `geom` plus `buffer_m`, snapped out to the pixel grid.

    Every image of a site uses these bounds, so the chips line up.
    """
    minx, miny, maxx, maxy = geom.bounds
    half = max(maxx - minx, maxy - miny) / 2 + buffer_m
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    # Snap each edge outward, then widen the narrower side to make a square.
    left, right = (
        math.floor((cx - half) / pixel) * pixel,
        math.ceil((cx + half) / pixel) * pixel,
    )
    bottom, top = (
        math.floor((cy - half) / pixel) * pixel,
        math.ceil((cy + half) / pixel) * pixel,
    )
    size = max(right - left, top - bottom)
    return left, bottom, left + size, bottom + size


def read_rgb(image_link, bounds):
    """Red, green and blue for `bounds`; outside the image is nodata.

    Returns (array of shape (3, rows, cols), transform, crs, nodata).
    """
    with rio.open(image_link) as src:
        nodata = 0 if src.nodata is None else src.nodata
        window = from_bounds(*bounds, src.transform).round_offsets().round_lengths()
        data = src.read(
            list(RGB_BANDS), window=window, boundless=True, fill_value=nodata
        )
        transform = rio.windows.transform(window, src.transform)
        crs = src.crs
    return data, transform, crs, nodata


def to_rgba(data, nodata, clip=config.RGB_CLIP):
    """Clip each band to `clip`, scale to 0-255, and make nodata transparent."""
    lo, hi = clip
    scaled = (np.clip(data.astype(np.float32), lo, hi) - lo) / (hi - lo) * 255
    rgb = np.round(scaled).astype(np.uint8)
    alpha = np.where(np.all(data == nodata, axis=0), 0, 255).astype(np.uint8)
    return np.dstack([rgb[0], rgb[1], rgb[2], alpha])


def encode_png(rgba):
    buffer = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buffer, format="PNG")
    return buffer.getvalue()


def _rings(geom):
    polygons = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    for polygon in polygons:
        yield polygon.exterior
        yield from polygon.interiors


def panel_png(rgba, transform, geom, size_px=config.PANEL_SIZE_PX):
    """The chip enlarged by a whole factor, with the site outline drawn on."""
    rows, cols = rgba.shape[:2]
    scale = max(1, round(size_px / max(rows, cols)))
    image = Image.fromarray(rgba, "RGBA").resize(
        (cols * scale, rows * scale), Image.Resampling.NEAREST
    )
    draw = ImageDraw.Draw(image)
    for ring in _rings(geom):
        points = [
            (
                (x - transform.c) / transform.a * scale,
                (y - transform.f) / transform.e * scale,
            )
            for x, y in ring.coords
        ]
        draw.line(points, fill=OUTLINE_COLOUR, width=2, joint="curve")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def warp_to_web_mercator(rgba, transform, crs):
    """Reproject an RGBA chip to EPSG:3857 (nearest neighbour).

    Returns the warped array and its Leaflet bounds.
    """
    rows, cols = rgba.shape[:2]
    dst_transform, dst_cols, dst_rows = calculate_default_transform(
        crs, WEB_MERCATOR, cols, rows, *array_bounds(rows, cols, transform)
    )
    bands = []
    for band in range(4):
        # Outside the source is left at 0, so it's transparent.
        destination = np.zeros((dst_rows, dst_cols), dtype=np.uint8)
        reproject(
            source=np.ascontiguousarray(rgba[:, :, band]),
            destination=destination,
            src_transform=transform,
            src_crs=crs,
            dst_transform=dst_transform,
            dst_crs=WEB_MERCATOR,
            resampling=Resampling.nearest,
        )
        bands.append(destination)
    west, south, east, north = warp_bounds(
        WEB_MERCATOR,
        "EPSG:4326",
        *array_bounds(dst_rows, dst_cols, dst_transform),
    )
    return np.dstack(bands), [[south, west], [north, east]]


def render_chip(image_link, geom):
    """Panel and overlay PNGs of `geom` (EPSG:27700) from one image."""
    data, transform, crs, nodata = read_rgb(image_link, chip_bounds(geom))
    rgba = to_rgba(data, nodata)
    overlay, bounds = warp_to_web_mercator(rgba, transform, crs)
    return Chip(
        panel_png=panel_png(rgba, transform, geom),
        overlay_png=encode_png(overlay),
        overlay_bounds=bounds,
    )
