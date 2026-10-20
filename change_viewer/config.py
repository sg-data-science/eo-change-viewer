"""Settings shared across the app."""

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"
# Saved reviews and exported chips. Unlike the cache, these are the user's
# work, so they're kept apart from it.
OUTPUT_DIR = PROJECT_ROOT / "output"
REVIEWS_PATH = OUTPUT_DIR / "reviews.gpkg"
EXPORT_DIR = OUTPUT_DIR / "chips"

# Name of the variable in .env holding the full WFS URL, auth key included.
WFS_URL_ENV = "PLANNING_POLY_WFS"

APP_PORT = 8050

# Earliest year the planning layer has data for.
FIRST_YEAR = 2010
DEFAULT_YEAR = 2022
DEFAULT_DEV_TYPE = "housing"

# Harmonised `dev_desc_is` categories offered in the filter, most useful first.
DEV_TYPES = [
    "housing",
    "business and industry",
    "householder development",
    "other development",
    "electricity generation",
    "transport and infrastructure",
    "minerals",
    "waste management",
    "agricultural work",
    "forestry work",
    "telecommunication",
    "listed building and conservation area consent",
    "other",
]

# Native CRS of the planning layer and the S2 ARD images.
NATIVE_CRS = "EPSG:27700"
DISPLAY_CRS = "EPSG:4326"

# Placeholder polygons are circles of this radius around the point location.
PLACEHOLDER_RADIUS_M = 10.0

# Sentinel-2 search. An image passes if under S2CLOUDLESS_MAX % of the site,
# plus CHECK_BUFFER_M around it, is cloud. The library's default whole-tile
# cloud filter is turned off (100): in Scotland most tiles are partly cloudy,
# and it rejected images that were clear over the site.
S2CLOUDLESS_MAX = 10
CHECK_BUFFER_M = 100
TILE_CLOUD_MAX = 100
WINDOW_NAMES = ("before", "after", "later")
N_WINDOWS = len(WINDOW_NAMES)
# First year of the CEDA Sentinel-2 ARD archive.
S2_FIRST_YEAR = 2015
# Month used for the default before/after windows, so images match in season.
DEFAULT_WINDOW_MONTH = 6

# Image chips: the area read is the site's bounds plus this buffer, made
# square, which leaves room to pan around in the panel maps. Each band is
# clipped to RGB_CLIP and scaled to 0-255.
CHIP_BUFFER_M = 1000
RGB_CLIP = (0, 150)
# Panel chips are enlarged to about this many pixels across, so the burnt-in
# polygon outline stays thin relative to the 10 m pixels.
PANEL_SIZE_PX = 400

# GDAL settings for faster remote COG reads, as the ceda_sentinel README
# recommends.
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
}

# Initial map view: Scotland.
MAP_CENTER = [56.8, -4.2]
MAP_ZOOM = 7


def years():
    """Years offered in the year selector, newest first."""
    return list(range(date.today().year, FIRST_YEAR - 1, -1))


def wfs_url():
    """Read the planning WFS URL from the environment or `.env`."""
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.environ.get(WFS_URL_ENV)
    if not url:
        raise RuntimeError(f"{WFS_URL_ENV} is not set; add it to .env")
    return url
