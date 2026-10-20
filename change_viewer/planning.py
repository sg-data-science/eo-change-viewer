"""Client for the planning application polygons served by GeoServer WFS.

The WFS URL in `.env` carries an auth key. It is kept inside `WfsConfig` and
never put into error messages: those are written here rather than passed on
from `requests`, and any server text is redacted first.

Every query filters on `year` first, because other fields such as `keyval`
are slow to query on their own.
"""

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

import geopandas as gpd
import requests
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon

from change_viewer import config

GEOMETRY_FIELD = "geom"
_LOOKUP_TO_NATIVE = Transformer.from_crs(
    config.DISPLAY_CRS, config.NATIVE_CRS, always_xy=True
)


class PlanningError(Exception):
    """A planning server request failed. The message is safe to show."""


@dataclass(frozen=True)
class WfsConfig:
    """The parts of the planning WFS URL.

    `repr` leaves out the key so the object is safe to log.
    """

    endpoint: str
    type_name: str
    authkey: str = field(repr=False)

    @classmethod
    def from_url(cls, url):
        parts = urlsplit(url)
        query = {k.lower(): v for k, v in parse_qsl(parts.query)}
        missing = {"authkey", "typename"} - query.keys()
        if missing:
            raise ValueError(f"WFS URL is missing {', '.join(sorted(missing))}")
        endpoint = f"{parts.scheme}://{parts.netloc}{parts.path}"
        return cls(endpoint, query["typename"], query["authkey"])

    @property
    def wms_endpoint(self):
        """The WMS endpoint on the same GeoServer workspace."""
        return re.sub(r"/wfs$", "/wms", self.endpoint)

    @property
    def layer_name(self):
        """The layer name without its workspace prefix."""
        return self.type_name.split(":")[-1]

    def redact(self, text):
        """Remove the auth key from text that came from the server."""
        return text.replace(self.authkey, "<redacted>")


def cql_string(value):
    """Quote a value as a CQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _year_clause(year):
    return f"year={int(year)}"


def _and(*clauses):
    return " AND ".join(c for c in clauses if c)


def _dev_type_clause(dev_type):
    return f"dev_desc_is={cql_string(dev_type)}" if dev_type else ""


def search_filter(year, text):
    """CQL to find polygons whose reference or address contains `text`.

    An exact `keyval` match is included too. `%` and `_` typed by the user
    act as wildcards, which is harmless for a read-only search.
    """
    text = text.strip()
    if not text:
        raise ValueError("search text is empty")
    like = cql_string(f"%{text}%")
    return _and(
        _year_clause(year),
        f"(reference ILIKE {like} OR address ILIKE {like}"
        f" OR keyval={cql_string(text.upper())})",
    )


def bbox_filter(year, bounds, dev_type=None):
    """CQL for polygons intersecting `bounds` (minx, miny, maxx, maxy, EPSG:27700)."""
    minx, miny, maxx, maxy = (float(b) for b in bounds)
    return _and(
        _year_clause(year),
        f"BBOX({GEOMETRY_FIELD},{minx},{miny},{maxx},{maxy})",
        _dev_type_clause(dev_type),
    )


def keyval_filter(year, keyval):
    """CQL for one polygon by year and `keyval`."""
    return _and(_year_clause(year), f"keyval={cql_string(keyval)}")


def is_placeholder(geom, radius=config.PLACEHOLDER_RADIUS_M, tolerance=0.5):
    """True if `geom` is a circle drawn around a point instead of a site boundary.

    These circles have a fixed radius and many vertices, all the same
    distance from the centre.
    """
    if isinstance(geom, MultiPolygon) and len(geom.geoms) == 1:
        geom = geom.geoms[0]
    if not isinstance(geom, Polygon) or geom.interiors:
        return False
    coords = geom.exterior.coords
    if len(coords) < 16:
        return False
    cx, cy = geom.centroid.x, geom.centroid.y
    return all(abs(math.dist(p, (cx, cy)) - radius) <= tolerance for p in coords)


def parse_features(data):
    """Turn a GeoJSON FeatureCollection into a GeoDataFrame in EPSG:27700.

    Adds an `is_placeholder` column.
    """
    features = data.get("features", [])
    if features:
        gdf = gpd.GeoDataFrame.from_features(features, crs=config.NATIVE_CRS)
    else:
        gdf = gpd.GeoDataFrame(geometry=[], crs=config.NATIVE_CRS)
    gdf["is_placeholder"] = [is_placeholder(g) for g in gdf.geometry]
    return gdf


def _exception_text(body):
    """Pull the message out of an OGC exception report, if it is one."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return ""
    texts = [
        el.text.strip()
        for el in root.iter()
        if el.tag.endswith(("ExceptionText", "ServiceException")) and el.text
    ]
    return " ".join(texts)


class PlanningClient:
    """Queries the planning polygons layer over WFS."""

    def __init__(self, wfs_config, session=None, timeout=30):
        self.config = wfs_config
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_env(cls, **kwargs):
        return cls(WfsConfig.from_url(config.wfs_url()), **kwargs)

    def _params(self, cql, count):
        return {
            "authkey": self.config.authkey,
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": self.config.type_name,
            "outputFormat": "application/json",
            "CQL_FILTER": cql,
            "count": int(count),
        }

    def get_features(self, cql, count=50):
        """Run a GetFeature query and return a GeoDataFrame in EPSG:27700."""
        try:
            response = self.session.get(
                self.config.endpoint,
                params=self._params(cql, count),
                timeout=self.timeout,
            )
        except requests.Timeout:
            raise PlanningError("Planning server did not respond in time") from None
        except requests.RequestException as e:
            # The exception text includes the request URL, so don't use it.
            raise PlanningError(
                f"Could not reach the planning server ({type(e).__name__})"
            ) from None

        if not response.ok:
            detail = self.config.redact(_exception_text(response.text))
            message = f"Planning server returned HTTP {response.status_code}"
            raise PlanningError(f"{message}: {detail}" if detail else message)
        try:
            data = response.json()
        except ValueError:
            detail = self.config.redact(_exception_text(response.text))
            raise PlanningError(
                f"Planning server sent an unexpected response: {detail}"
                if detail
                else "Planning server sent an unexpected response"
            ) from None
        return parse_features(data)

    def search(self, year, text, limit=20):
        """Polygons for `year` whose reference or address contains `text`."""
        gdf = self.get_features(search_filter(year, text), count=limit)
        if gdf.empty:
            return gdf
        return gdf.sort_values("reference").reset_index(drop=True)

    def at_point(self, year, lon, lat, tolerance_m=5, dev_type=None, limit=20):
        """Polygons for `year` within `tolerance_m` of a clicked lon/lat.

        Smallest first, so a site inside a larger one is picked over it.
        """
        x, y = _LOOKUP_TO_NATIVE.transform(lon, lat)
        bounds = (x - tolerance_m, y - tolerance_m, x + tolerance_m, y + tolerance_m)
        gdf = self.get_features(bbox_filter(year, bounds, dev_type), count=limit)
        if gdf.empty:
            return gdf
        order = gdf.geometry.area.sort_values(kind="stable").index
        return gdf.loc[order].reset_index(drop=True)

    def by_keyval(self, year, keyval):
        """A one-row GeoDataFrame for year and `keyval`, or None if not found."""
        gdf = self.get_features(keyval_filter(year, keyval), count=1)
        return None if gdf.empty else gdf
