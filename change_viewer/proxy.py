"""Server-side proxy for planning polygon WMS tiles.

The browser asks `/wms` for a tile with a small fixed set of parameters. The
proxy checks them, builds the CQL filter and adds the auth key, so the key
never reaches the browser and the browser can't send arbitrary WMS requests.
"""

import math
from datetime import date

import requests
from flask import Response, request

from change_viewer import config
from change_viewer.planning import cql_string

ROUTE = "/wms"
MAX_TILE_SIZE = 2048

# Outline with a light fill, so the imagery underneath stays visible.
_SLD = """<StyledLayerDescriptor version="1.0.0" \
xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc">\
<NamedLayer><Name>{layer}</Name><UserStyle><FeatureTypeStyle><Rule>\
<PolygonSymbolizer>\
<Fill><CssParameter name="fill">{colour}</CssParameter>\
<CssParameter name="fill-opacity">0.15</CssParameter></Fill>\
<Stroke><CssParameter name="stroke">{colour}</CssParameter>\
<CssParameter name="stroke-width">1.5</CssParameter></Stroke>\
</PolygonSymbolizer></Rule></FeatureTypeStyle></UserStyle></NamedLayer>\
</StyledLayerDescriptor>"""
POLYGON_COLOUR = "#ffd400"


class BadTileRequest(ValueError):
    """A tile request parameter is missing or invalid."""


def _param(args, name):
    """Look a parameter up case-insensitively."""
    for key, value in args.items():
        if key.lower() == name:
            return value
    return None


def parse_tile_request(args):
    """Check the browser's tile parameters and return them as clean values.

    Only `bbox`, `width`, `height`, `year` and `dev_type` are read. Anything
    else Leaflet sends (layers, styles, srs, ...) is ignored, because the
    proxy sets those itself.
    """
    try:
        bbox = [float(v) for v in _param(args, "bbox").split(",")]
    except (AttributeError, ValueError):
        raise BadTileRequest("bbox must be four numbers") from None
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise BadTileRequest("bbox must be four numbers")
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise BadTileRequest("bbox min must be less than max")

    sizes = []
    for name in ("width", "height"):
        try:
            size = int(_param(args, name))
        except (TypeError, ValueError):
            raise BadTileRequest(f"{name} must be an integer") from None
        if not 1 <= size <= MAX_TILE_SIZE:
            raise BadTileRequest(f"{name} must be 1 to {MAX_TILE_SIZE}")
        sizes.append(size)

    try:
        year = int(_param(args, "year"))
    except (TypeError, ValueError):
        raise BadTileRequest("year must be an integer") from None
    if not config.FIRST_YEAR <= year <= date.today().year:
        raise BadTileRequest("year is out of range")

    dev_type = _param(args, "dev_type") or None
    if dev_type is not None and dev_type not in config.DEV_TYPES:
        raise BadTileRequest("dev_type is not a known category")

    return {
        "bbox": bbox,
        "width": sizes[0],
        "height": sizes[1],
        "year": year,
        "dev_type": dev_type,
    }


def tile_filter(year, dev_type=None):
    """The CQL filter for one year's polygons, optionally of one type."""
    cql = f"year={int(year)}"
    if dev_type:
        cql += f" AND dev_desc_is={cql_string(dev_type)}"
    return cql


def upstream_params(tile, wfs_config):
    """GeoServer GetMap parameters for a checked tile request."""
    return {
        "authkey": wfs_config.authkey,
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetMap",
        "layers": wfs_config.layer_name,
        "srs": "EPSG:3857",
        "bbox": ",".join(repr(v) for v in tile["bbox"]),
        "width": tile["width"],
        "height": tile["height"],
        "format": "image/png",
        "transparent": "true",
        "CQL_FILTER": tile_filter(tile["year"], tile["dev_type"]),
        "SLD_BODY": _SLD.format(layer=wfs_config.layer_name, colour=POLYGON_COLOUR),
    }


def register_wms_proxy(server, wfs_config, session=None, timeout=30):
    """Add the `/wms` tile route to a Flask app."""
    session = session or requests.Session()

    def wms_tile():
        try:
            tile = parse_tile_request(request.args)
        except BadTileRequest as e:
            return Response(str(e), status=400, mimetype="text/plain")
        try:
            upstream = session.get(
                wfs_config.wms_endpoint,
                params=upstream_params(tile, wfs_config),
                timeout=timeout,
            )
        except requests.RequestException:
            # Don't pass the exception text on: it includes the URL and key.
            return Response(
                "Planning map server unavailable", status=502, mimetype="text/plain"
            )
        content_type = upstream.headers.get("Content-Type", "")
        if not upstream.ok or not content_type.startswith("image/"):
            return Response(
                "Planning map server error", status=502, mimetype="text/plain"
            )
        return Response(
            upstream.content,
            mimetype=content_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    server.add_url_rule(ROUTE, "wms_tile", wms_tile)
