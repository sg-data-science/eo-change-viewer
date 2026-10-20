"""Dash app: planning polygons on a map, and Sentinel-2 chips of one site.

Pick a site by clicking or searching, set up to three month windows and press
Find. Each window is searched in its own callback, so the three run in
parallel and each shows its own progress.

Run with `python -m change_viewer.app` and open http://localhost:8050.
"""

import argparse
import functools
import json
import math
from datetime import date

import dash_bootstrap_components as dbc
import dash_leaflet as dl
import geopandas as gpd
from dash import ALL, MATCH, Dash, Input, Output, State, ctx, dcc, html, no_update
from flask import abort, send_file
from rasterio.errors import RasterioIOError
from shapely.geometry import shape

from change_viewer import basemap, config, imagery
from change_viewer.cache import CHIP_ROUTE, Cache, cached_chip, cached_search
from change_viewer.planning import PlanningClient, PlanningError
from change_viewer.proxy import ROUTE as WMS_ROUTE
from change_viewer.proxy import register_wms_proxy

ESRI_IMAGERY = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
ESRI_ATTRIBUTION = "Tiles &copy; Esri, Maxar, Earthstar Geographics"
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = "&copy; OpenStreetMap contributors"

SELECTED_STYLE = {"color": "#00e5ff", "weight": 3, "fillOpacity": 0}

# Click tolerance in screen pixels, turned into metres at the current zoom.
CLICK_TOLERANCE_PX = 4
# Smallest half-width of the view when zooming to a polygon, so small sites
# are shown with some context.
MIN_VIEW_HALF_WIDTH_M = 150

# Attributes shown in the panel, in order, with labels.
ATTRIBUTE_LABELS = {
    "reference": "Reference",
    "local_auth": "Council",
    "address": "Address",
    "proposal": "Proposal",
    "dev_desc_is": "Development type",
    "maj_min_loc_is": "Scale",
    "appl_desc_is": "Application type",
    "decis_desc_is": "Decision",
    "date_recei": "Received",
    "date_valid": "Validated",
    "date_decis": "Decided",
    "keyval": "Key",
}

ALL_TYPES = "__all__"
BASEMAP_DATE_LABEL = "Basemap image date"

WINDOW_NAMES = ["Before", "After", "Later"]
# A transparent pixel, so map overlays exist before there's an image.
BLANK_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42"
    "mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
BLANK_BOUNDS = [[0, 0], [0.0001, 0.0001]]
EMPTY_COLLECTION = {"type": "FeatureCollection", "features": []}


def to_features(gdf):
    """GeoJSON feature dicts in EPSG:4326 for a planning GeoDataFrame."""
    if gdf.empty:
        return []
    return json.loads(gdf.to_crs(config.DISPLAY_CRS).to_json(na="null"))["features"]


def click_tolerance_m(zoom, lat):
    """Ground distance covered by `CLICK_TOLERANCE_PX` pixels at a zoom level."""
    metres_per_px = 156543.03 * math.cos(math.radians(lat)) / 2 ** (zoom or 0)
    return max(1.0, CLICK_TOLERANCE_PX * metres_per_px)


def view_bounds(feature, min_half_width_m=MIN_VIEW_HALF_WIDTH_M):
    """Leaflet bounds around a feature, at least `min_half_width_m` each way."""
    minx, miny, maxx, maxy = shape(feature["geometry"]).bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    half_lat = min_half_width_m / 111_320
    half_lon = half_lat / math.cos(math.radians(cy))
    minx, maxx = min(minx, cx - half_lon), max(maxx, cx + half_lon)
    miny, maxy = min(miny, cy - half_lat), max(maxy, cy + half_lat)
    return [[miny, minx], [maxy, maxx]]


def dev_type_value(value):
    """The dropdown value as a `dev_desc_is` filter, or None for all types."""
    return None if value in (None, "", ALL_TYPES) else value


@functools.lru_cache(maxsize=256)
def _basemap_image_info(lon, lat):
    # Errors aren't cached, so a failed lookup is retried next time.
    return basemap.image_info(lon, lat)


def basemap_date_text(feature):
    """Capture date of the basemap imagery at a point inside the feature."""
    point = shape(feature["geometry"]).representative_point()
    try:
        info = _basemap_image_info(round(point.x, 5), round(point.y, 5))
    except basemap.BasemapError:
        return "Unavailable, try again later"
    return basemap.describe(info) if info else "Not recorded"


def basemap_date_cell_id(feature):
    """Id of the basemap date cell, unique to the selected application."""
    return {"type": "basemap-date", "keyval": feature["properties"].get("keyval") or ""}


def attributes_panel(feature, show_basemap_date=False):
    """The attribute table for a selected feature.

    With `show_basemap_date`, a basemap date cell is added showing
    "Looking up…"; adding it triggers its own callback, which fills it.
    Without it, no lookup is made.
    """
    if not feature:
        return html.P(
            "Click a polygon or search to see its details.", className="text-muted"
        )
    props = feature["properties"]
    rows = [
        html.Tr([html.Th(label, className="pe-2"), html.Td(props.get(key) or "–")])
        for key, label in ATTRIBUTE_LABELS.items()
    ]
    if show_basemap_date:
        after_dates = list(ATTRIBUTE_LABELS).index("date_decis") + 1
        rows.insert(
            after_dates,
            html.Tr(
                [
                    html.Th(BASEMAP_DATE_LABEL, className="pe-2"),
                    html.Td(
                        html.Span("Looking up…", className="text-muted"),
                        id=basemap_date_cell_id(feature),
                    ),
                ]
            ),
        )
    children = []
    if props.get("is_placeholder"):
        children.append(
            dbc.Alert(
                "Approximate location only: this is a circle around the "
                "application's point location, not the site boundary.",
                color="warning",
                className="py-2 small",
            )
        )
    children.append(dbc.Table(html.Tbody(rows), size="sm", className="small mb-2"))
    if props.get("url"):
        children.append(
            html.A(
                "Open on council planning portal",
                href=props["url"],
                target="_blank",
                rel="noopener noreferrer",
            )
        )
    return children


def search_hits(features):
    """Clickable list items for search results."""
    return [
        dbc.ListGroupItem(
            [
                html.Div(f["properties"].get("reference") or "–", className="fw-bold"),
                html.Div(f["properties"].get("address") or "", className="small"),
                html.Div(
                    f["properties"].get("dev_desc_is") or "",
                    className="small text-muted",
                ),
            ],
            id={"type": "search-hit", "index": i},
            action=True,
            n_clicks=0,
        )
        for i, f in enumerate(features)
    ]


def window_id(kind, index):
    return {"type": f"window-{kind}", "index": index}


def default_window_values(feature):
    """`YYYY-MM` values for the window inputs, or None where left blank."""
    if not feature:
        return [None] * config.N_WINDOWS
    props = feature["properties"]
    windows = imagery.default_windows(props.get("date_recei"), props.get("date_decis"))
    return [w.key if w else None for w in windows]


def window_requests(feature, values, max_cloud, n_clicks):
    """One search request per window, for the window callbacks."""
    return [
        {
            "feature": feature,
            "window": value or None,
            "max_cloud": float(max_cloud or config.S2CLOUDLESS_MAX),
            # Makes each press a new request, even with the same windows.
            "n": n_clicks,
        }
        for value in values
    ]


def aoi_from_feature(feature):
    """A one-row GeoDataFrame in EPSG:27700 for a selected feature."""
    return gpd.GeoDataFrame.from_features([feature], crs=config.DISPLAY_CRS).to_crs(
        config.NATIVE_CRS
    )


def candidate_label(candidate, best):
    label = f"{candidate.image_date} · {candidate.cloud:.1f}% cloud"
    return f"{label} (clearest)" if candidate == best else label


def choice_options(result):
    """Dropdown options for every image that passed, in date order."""
    best = result.best
    return [
        {"label": candidate_label(c, best), "value": c.image_link}
        for c in result.candidates
    ]


def window_problem(window, today=None):
    """Why a window can't have images, or None if it can."""
    if window.start > (today or date.today()):
        return f"{window.label} is in the future."
    if window.year < config.S2_FIRST_YEAR:
        return f"Sentinel-2 images start in {config.S2_FIRST_YEAR}."
    return None


def no_image_message(window, max_cloud):
    return (
        f"No image of this site with under {max_cloud:g}% cloud in "
        f"{window.label}. Try another month or raise the max cloud."
    )


def _warning(message):
    return dbc.Alert(message, color="warning", className="py-2 small mb-0")


def _muted(message):
    return html.P(message, className="text-muted small mb-0")


def _panel_map(index):
    """A small map for one window's image.

    `assets/sync_maps.js` keeps the panel maps on the same view, and the main
    map too when "Link to main map" is on. It finds them by the
    `window-panel` in their id.

    There's no basemap: only the Sentinel-2 image and the site outline show,
    so an empty panel plainly means no image yet.
    """
    return dl.Map(
        id=window_id("panel", index),
        center=config.MAP_CENTER,
        zoom=config.MAP_ZOOM,
        maxZoom=20,
        attributionControl=False,
        style={"height": "18rem", "background": "#e9ecef"},
        children=[
            dl.ImageOverlay(
                id=window_id("image", index), url=BLANK_PNG, bounds=BLANK_BOUNDS
            ),
            dl.GeoJSON(id=window_id("outline", index), style=SELECTED_STYLE),
        ],
    )


def _window_card(index):
    return dbc.Card(
        [
            dbc.CardHeader(
                [
                    html.Span(WINDOW_NAMES[index], className="fw-bold me-2"),
                    dcc.Input(
                        id=window_id("month", index),
                        type="month",
                        className="form-control form-control-sm d-inline-block",
                        style={"width": "11rem"},
                    ),
                ],
                className="d-flex align-items-center",
            ),
            dbc.CardBody(
                [
                    _panel_map(index),
                    dcc.Loading(
                        html.Div(
                            [
                                html.Div(id=window_id("status", index)),
                                html.Div(
                                    id=window_id("caption", index),
                                    className="small mt-1",
                                ),
                            ],
                            style={"minHeight": "1.5rem"},
                        ),
                        custom_spinner=html.Div(
                            [dbc.Spinner(size="sm"), " Searching CEDA…"],
                            className="small text-muted mt-1",
                        ),
                        overlay_style={"visibility": "visible", "opacity": 0.4},
                    ),
                    # A native select, as its options always draw on top; a
                    # Dash dropdown's menu was cut off at the bottom of the page.
                    dbc.Select(
                        id=window_id("choice", index),
                        options=[],
                        size="sm",
                        className="mt-1",
                        style={"display": "none"},
                    ),
                ],
                className="p-2",
            ),
            dcc.Store(id=window_id("request", index)),
            dcc.Store(id=window_id("result", index)),
        ],
        className="h-100",
    )


def _image_panel():
    return [
        dbc.Row(
            [
                dbc.Col(html.H5("Sentinel-2 images", className="mb-0"), width="auto"),
                dbc.Col(
                    dbc.InputGroup(
                        [
                            dbc.InputGroupText("Max cloud %"),
                            dbc.Input(
                                id="max-cloud",
                                type="number",
                                min=1,
                                max=100,
                                step=1,
                                value=config.S2CLOUDLESS_MAX,
                                style={"maxWidth": "5rem"},
                            ),
                        ],
                        size="sm",
                    ),
                    width="auto",
                ),
                dbc.Col(
                    dbc.Button("Find images", id="find-button", n_clicks=0, size="sm"),
                    width="auto",
                ),
                dbc.Col(
                    dbc.Switch(
                        id="link-main-map",
                        label="Link to main map",
                        value=False,
                        persistence=True,
                        className="small mb-0",
                    ),
                    width="auto",
                ),
                dbc.Col(html.Div(id="find-status", className="small"), width=True),
            ],
            className="g-2 align-items-center my-2",
        ),
        dbc.Row(
            [
                dbc.Col(_window_card(i), md=4, className="mb-2")
                for i in range(config.N_WINDOWS)
            ],
            className="g-2",
        ),
    ]


def _sidebar():
    return [
        html.H4("change-viewer", className="mt-2"),
        dbc.Label("Year", html_for="year"),
        dcc.Dropdown(
            id="year",
            options=config.years(),
            value=config.DEFAULT_YEAR,
            clearable=False,
        ),
        dbc.Label("Development type", html_for="dev-type", className="mt-2"),
        dcc.Dropdown(
            id="dev-type",
            options=[{"label": "All types", "value": ALL_TYPES}]
            + [{"label": t, "value": t} for t in config.DEV_TYPES],
            value=config.DEFAULT_DEV_TYPE,
            clearable=False,
        ),
        dbc.Label("Search reference, address or key", className="mt-3"),
        dbc.InputGroup(
            [
                dbc.Input(id="search-text", placeholder="e.g. 22/00083/DPP"),
                dbc.Button("Search", id="search-button", n_clicks=0),
            ]
        ),
        html.Div(id="status", className="mt-2"),
        dcc.Loading(
            dbc.ListGroup(
                id="search-results",
                className="mt-2",
                style={"maxHeight": "30vh", "overflowY": "auto"},
            ),
            type="dot",
        ),
        html.Hr(),
        html.H5("Selected application"),
        dbc.Switch(
            id="show-basemap-date",
            label="Show basemap image date (slower)",
            value=False,
            persistence=True,
            className="small",
        ),
        html.Div(id="attributes", children=attributes_panel(None)),
    ]


def _map():
    return dl.Map(
        id="map",
        center=config.MAP_CENTER,
        zoom=config.MAP_ZOOM,
        style={"height": "58vh", "marginTop": "0.5rem"},
        children=[
            dl.LayersControl(
                [
                    dl.BaseLayer(
                        dl.TileLayer(
                            url=ESRI_IMAGERY,
                            attribution=ESRI_ATTRIBUTION,
                            maxZoom=20,
                            maxNativeZoom=19,
                        ),
                        name="Esri World Imagery",
                        checked=True,
                    ),
                    dl.BaseLayer(
                        dl.TileLayer(
                            url=OSM_TILES,
                            attribution=OSM_ATTRIBUTION,
                            maxZoom=20,
                            maxNativeZoom=19,
                        ),
                        name="OpenStreetMap",
                    ),
                    dl.Overlay(
                        dl.WMSTileLayer(
                            id="planning-wms",
                            url=WMS_ROUTE,
                            layers="planning",
                            params=_wms_params(
                                config.DEFAULT_YEAR, config.DEFAULT_DEV_TYPE
                            ),
                            format="image/png",
                            transparent=True,
                            version="1.1.1",
                            maxZoom=20,
                        ),
                        name="Planning applications",
                        checked=True,
                    ),
                ]
                + [
                    dl.Overlay(
                        dl.ImageOverlay(
                            id=window_id("overlay", i),
                            url=BLANK_PNG,
                            bounds=BLANK_BOUNDS,
                        ),
                        name=f"Sentinel-2: {WINDOW_NAMES[i].lower()}",
                        checked=False,
                    )
                    for i in range(config.N_WINDOWS)
                ],
                id="layers",
            ),
            dl.GeoJSON(id="selected-layer", style=SELECTED_STYLE),
        ],
    )


def _wms_params(year, dev_type):
    # `layers` is required by Leaflet but ignored by the proxy, which sets it.
    params = {"layers": "planning", "year": int(year)}
    dev_type = dev_type_value(dev_type)
    if dev_type:
        params["dev_type"] = dev_type
    return params


def _error(message):
    return dbc.Alert(message, color="danger", className="py-2 small")


def _info(message):
    return dbc.Alert(message, color="info", className="py-2 small")


def create_app(client=None, image_search=None, chip_cache=None):
    """Build the Dash app.

    `client` defaults to one configured from `.env`; `image_search` and
    `chip_cache` to the real CEDA search and the on-disk cache.
    """
    client = client or PlanningClient.from_env()
    image_search = image_search or imagery.ImageSearch()
    chip_cache = chip_cache or Cache()
    app = Dash(
        __name__,
        title="change-viewer",
        external_stylesheets=[dbc.themes.BOOTSTRAP],
    )
    register_wms_proxy(app.server, client.config)

    @app.server.route(f"{CHIP_ROUTE}/<keyval>/<filename>")
    def chip_file(keyval, filename):
        if not filename.endswith(".png"):
            abort(404)
        try:
            path = chip_cache.path(keyval, filename)
        except ValueError:
            abort(404)
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="image/png", max_age=86400)

    app.layout = dbc.Container(
        [
            dbc.Row(
                [
                    dbc.Col(
                        [_map(), *_image_panel()],
                        md=8,
                        lg=9,
                        style={"height": "100vh", "overflowY": "auto"},
                    ),
                    dbc.Col(
                        _sidebar(),
                        md=4,
                        lg=3,
                        style={"height": "100vh", "overflowY": "auto"},
                    ),
                ],
                className="g-2",
            ),
            dcc.Store(id="search-store"),
            dcc.Store(id="selected"),
        ],
        fluid=True,
    )

    @app.callback(
        Output("planning-wms", "params"),
        Input("year", "value"),
        Input("dev-type", "value"),
    )
    def update_wms(year, dev_type):
        return _wms_params(year, dev_type)

    @app.callback(
        Output("search-results", "children"),
        Output("search-store", "data"),
        Output("status", "children"),
        Input("search-button", "n_clicks"),
        Input("search-text", "n_submit"),
        State("search-text", "value"),
        State("year", "value"),
        prevent_initial_call=True,
    )
    def run_search(_clicks, _submits, text, year):
        if not (text or "").strip():
            return [], [], _info("Type a reference, address or key to search.")
        try:
            features = to_features(client.search(year, text))
        except PlanningError as e:
            return [], [], _error(str(e))
        if not features:
            return [], [], _info(f"No {year} applications match “{text.strip()}”.")
        return search_hits(features), features, None

    @app.callback(
        Output("selected", "data"),
        Output("map", "viewport"),
        Output("status", "children", allow_duplicate=True),
        Input("map", "clickData"),
        Input({"type": "search-hit", "index": ALL}, "n_clicks"),
        State("search-store", "data"),
        State("year", "value"),
        State("dev-type", "value"),
        State("map", "zoom"),
        prevent_initial_call=True,
    )
    def select(click, hit_clicks, search_features, year, dev_type, zoom):
        trigger = ctx.triggered_id
        if isinstance(trigger, dict):
            index = trigger["index"]
            if not hit_clicks[index] or not search_features:
                return no_update, no_update, no_update
            feature = search_features[index]
            viewport = {"bounds": view_bounds(feature), "transition": "flyToBounds"}
            return feature, viewport, None

        lat, lng = click["latlng"]["lat"], click["latlng"]["lng"]
        try:
            gdf = client.at_point(
                year,
                lng,
                lat,
                tolerance_m=click_tolerance_m(zoom or config.MAP_ZOOM, lat),
                dev_type=dev_type_value(dev_type),
            )
        except PlanningError as e:
            return no_update, no_update, _error(str(e))
        features = to_features(gdf)
        if not features:
            return None, no_update, None
        note = None
        if len(features) > 1:
            note = _info(
                f"{len(features)} applications here; showing the smallest. "
                "Search by reference to pick another."
            )
        return features[0], no_update, note

    @app.callback(Output("selected-layer", "data"), Input("selected", "data"))
    def highlight_selected(feature):
        return {"type": "FeatureCollection", "features": [feature] if feature else []}

    @app.callback(
        Output("attributes", "children"),
        Input("selected", "data"),
        Input("show-basemap-date", "value"),
    )
    def show_attributes(feature, show_basemap_date):
        return attributes_panel(feature, show_basemap_date)

    # Fired when show_attributes adds a new date cell, so the slow external
    # lookup never holds up the highlight or attributes. The cell id carries
    # the keyval: a late reply for an earlier selection targets a cell that's
    # gone and is dropped.
    @app.callback(
        Output({"type": "basemap-date", "keyval": MATCH}, "children"),
        Input({"type": "basemap-date", "keyval": MATCH}, "id"),
        State("selected", "data"),
    )
    def fill_basemap_date(cell_id, feature):
        if not feature or basemap_date_cell_id(feature) != cell_id:
            return no_update
        return basemap_date_text(feature)

    # --- Sentinel-2 windows -------------------------------------------------

    @app.callback(
        Output(window_id("month", ALL), "value"),
        Input("selected", "data"),
    )
    def prefill_windows(feature):
        return default_window_values(feature)

    @app.callback(
        Output(window_id("outline", ALL), "data"),
        Output(window_id("panel", ALL), "viewport"),
        Input("selected", "data"),
        prevent_initial_call=True,
    )
    def show_site_in_panels(feature):
        n = config.N_WINDOWS
        if not feature:
            return [EMPTY_COLLECTION] * n, [no_update] * n
        outline = {"type": "FeatureCollection", "features": [feature]}
        viewport = {"bounds": view_bounds(feature), "transition": "fitBounds"}
        return [outline] * n, [viewport] * n

    @app.callback(
        Output(window_id("request", ALL), "data"),
        Output("find-status", "children"),
        Input("find-button", "n_clicks"),
        Input("selected", "data"),
        State(window_id("month", ALL), "value"),
        State("max-cloud", "value"),
        prevent_initial_call=True,
    )
    def request_windows(n_clicks, feature, values, max_cloud):
        if ctx.triggered_id != "find-button":
            # A new site clears the old site's images.
            return [None] * config.N_WINDOWS, None
        if not feature:
            return [no_update] * config.N_WINDOWS, _muted("Select a site first.")
        return window_requests(feature, values, max_cloud, n_clicks), None

    @app.callback(
        Output(window_id("result", MATCH), "data"),
        Output(window_id("status", MATCH), "children"),
        Output(window_id("choice", MATCH), "options"),
        Output(window_id("choice", MATCH), "value"),
        Input(window_id("request", MATCH), "data"),
    )
    def search_window(request):
        if not request:
            return None, None, [], None
        if not request["window"]:
            return None, _muted("No month set."), [], None
        window = imagery.Window.parse(request["window"])
        problem = window_problem(window)
        if problem:
            return None, _muted(problem), [], None
        max_cloud = request["max_cloud"]
        aoi = aoi_from_feature(request["feature"])
        try:
            result = cached_search(chip_cache, image_search, aoi, window, max_cloud)
        except imagery.CedaUnavailable:
            return (
                None,
                _error("CEDA unavailable, try again. This is a server problem."),
                [],
                None,
            )
        if not result.candidates:
            return None, _warning(no_image_message(window, max_cloud)), [], None
        status = None
        if result.read_errors:
            status = _muted("Some images couldn't be read; others may exist.")
        data = {"feature": request["feature"], **result.to_dict()}
        return data, status, choice_options(result), result.best.image_link

    @app.callback(
        Output(window_id("image", MATCH), "url"),
        Output(window_id("image", MATCH), "bounds"),
        Output(window_id("caption", MATCH), "children"),
        Output(window_id("choice", MATCH), "style"),
        Output(window_id("overlay", MATCH), "url"),
        Output(window_id("overlay", MATCH), "bounds"),
        Input(window_id("choice", MATCH), "value"),
        State(window_id("result", MATCH), "data"),
    )
    def show_window_image(image_link, data):
        hidden = {"display": "none"}
        blank = (BLANK_PNG, BLANK_BOUNDS)
        if not image_link or not data:
            return *blank, None, hidden, *blank
        result = imagery.SearchResult.from_dict(data)
        candidate = next(c for c in result.candidates if c.image_link == image_link)
        feature = data["feature"]
        aoi = aoi_from_feature(feature)
        try:
            _, overlay_url, bounds = cached_chip(
                chip_cache,
                feature["properties"]["keyval"],
                candidate,
                aoi.geometry.iloc[0],
            )
        except RasterioIOError:
            message = _error("Couldn't read the image from CEDA, try again.")
            return *blank, message, hidden, *blank
        source = "CEDA STAC" if result.source == "stac" else "CEDA archive listing"
        caption = [
            html.Span(candidate_label(candidate, result.best), className="fw-bold"),
            html.Span(f" · {source}", className="text-muted"),
        ]
        choice_style = {} if len(result.candidates) > 1 else hidden
        # The panel map and the main map's overlay show the same PNG.
        return overlay_url, bounds, caption, choice_style, overlay_url, bounds

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=config.APP_PORT)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    create_app().run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
