"""Settings shared across the app."""

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"

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
