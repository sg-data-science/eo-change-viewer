"""Capture dates for the Esri World Imagery basemap.

The basemap is a mosaic of images taken on different dates. Esri publishes
the source of each patch in metadata layers on the same MapServer, one per
resolution. Layers 5 to 16 describe the mosaic drawn at zoom 12 and above,
which is what's on screen when looking at a site; the coarser layers describe
the low-zoom overview and carry no date.
"""

from dataclasses import dataclass
from datetime import date, datetime

import requests

IDENTIFY_URL = (
    "https://services.arcgisonline.com/arcgis/rest/services/"
    "World_Imagery/MapServer/identify"
)
DETAILED_LAYERS = range(5, 17)


class BasemapError(Exception):
    """The imagery metadata could not be fetched."""


@dataclass(frozen=True)
class ImageInfo:
    """Source of the basemap image at a point."""

    date: date
    source: str | None = None
    resolution_m: float | None = None


def _parse_date(value):
    try:
        return datetime.strptime(str(value), "%Y%m%d").date()
    except ValueError:
        return None


def _parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_identify(data):
    """The finest-resolution dated result from an identify response, or None."""
    results = sorted(data.get("results", []), key=lambda r: r.get("layerId", 0))
    for result in results:
        attrs = result.get("attributes", {})
        captured = _parse_date(attrs.get("SRC_DATE"))
        if captured:
            source = attrs.get("NICE_DESC")
            return ImageInfo(
                date=captured,
                source=None if source in (None, "", "Null") else source,
                resolution_m=_parse_float(attrs.get("SRC_RES")),
            )
    return None


def image_info(lon, lat, session=None, timeout=5):
    """Basemap image source at a lon/lat, or None if Esri has no date for it."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "sr": 4326,
        "layers": "all:" + ",".join(str(i) for i in DETAILED_LAYERS),
        "tolerance": 0,
        # identify needs a map extent and size; with zero tolerance any will do.
        "mapExtent": "-180,-90,180,90",
        "imageDisplay": "800,800,96",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        response = (session or requests).get(
            IDENTIFY_URL, params=params, timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as e:
        raise BasemapError(f"Basemap metadata unavailable ({type(e).__name__})") from e
    if "error" in data:
        raise BasemapError("Basemap metadata service returned an error")
    return parse_identify(data)


def describe(info):
    """Short text for the attributes panel, e.g. `2021-09-01 (Vantor, 0.31 m)`."""
    details = [
        d for d in (info.source, info.resolution_m and f"{info.resolution_m:g} m") if d
    ]
    text = info.date.isoformat()
    return f"{text} ({', '.join(details)})" if details else text
