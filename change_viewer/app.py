"""Dash app: planning polygons on a map, with search and click lookup.

Run with `python -m change_viewer.app` and open http://localhost:8050.
"""

import argparse
import functools
import json
import math

import dash_bootstrap_components as dbc
import dash_leaflet as dl
from dash import ALL, MATCH, Dash, Input, Output, State, ctx, dcc, html, no_update
from shapely.geometry import shape

from change_viewer import basemap, config
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
        style={"height": "calc(100vh - 1rem)", "marginTop": "0.5rem"},
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


def create_app(client=None):
    """Build the Dash app. `client` defaults to one configured from `.env`."""
    client = client or PlanningClient.from_env()
    app = Dash(
        __name__,
        title="change-viewer",
        external_stylesheets=[dbc.themes.BOOTSTRAP],
    )
    register_wms_proxy(app.server, client.config)

    app.layout = dbc.Container(
        [
            dbc.Row(
                [
                    dbc.Col(_map(), md=8, lg=9),
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
