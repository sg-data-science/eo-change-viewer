"""Dash app: planning polygons on a map, and Sentinel-2 chips of one site.

Pick a site by clicking, searching or stepping with Next and Previous, set up
to three month windows and press Find. Each window is searched in its own
callback, so the three run in parallel and each shows its own progress. A
verdict saved for a site is stored with its chosen images, which are
exported; reopening the site brings both back.

Run with `python -m change_viewer.app` and open http://localhost:8050.
"""

import argparse
import functools
import hashlib
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

from change_viewer import basemap, config, imagery, reviews, workers
from change_viewer.cache import CHIP_ROUTE, Cache, cached_chip, cached_search
from change_viewer.planning import PlanningClient, PlanningError
from change_viewer.proxy import ROUTE as WMS_ROUTE
from change_viewer.proxy import register_wms_proxy
from change_viewer.reviews import ReviewStore, WindowChoice

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
ALL_COUNCILS = "__all__"
BASEMAP_DATE_LABEL = "Basemap image date"

WINDOW_NAMES = [name.title() for name in config.WINDOW_NAMES]
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


MONTH_OPTIONS = [
    {"label": date(2000, m, 1).strftime("%b"), "value": f"{m:02d}"}
    for m in range(1, 13)
]


def year_options(today=None):
    last = (today or date.today()).year
    return [str(y) for y in range(last, config.S2_FIRST_YEAR - 1, -1)]


def split_month(value):
    """(month, year) select values for a `YYYY-MM` window, or Nones."""
    if not value:
        return None, None
    return value[5:7], value[:4]


def join_month(month, year):
    """The `YYYY-MM` window for the month and year selects, None if either
    is blank."""
    return f"{year}-{month}" if month and year else None


def default_window_values(feature, review=None):
    """`YYYY-MM` values for the window inputs, or None where left blank.

    A saved review's windows are used when there is one.
    """
    if not feature:
        return [None] * config.N_WINDOWS
    if review_has_windows(review):
        return [month for month, _ in reviews.saved_windows(review)]
    props = feature["properties"]
    windows = imagery.default_windows(props.get("date_recei"), props.get("date_decis"))
    return [w.key if w else None for w in windows]


def review_has_windows(review):
    return bool(review) and any(month for month, _ in reviews.saved_windows(review))


def window_requests(feature, values, max_cloud, n_clicks, prefer=None):
    """One search request per window, for the window callbacks.

    `prefer` gives an image link per window to show instead of the clearest,
    such as the one chosen in a saved review.
    """
    prefer = prefer or [None] * len(values)
    return [
        {
            "feature": feature,
            "window": value or None,
            "max_cloud": float(max_cloud or config.S2CLOUDLESS_MAX),
            "prefer": link,
            # Makes each press a new request, even with the same windows.
            "n": n_clicks,
        }
        for value, link in zip(values, prefer, strict=True)
    ]


def aoi_from_feature(feature):
    """A one-row GeoDataFrame in EPSG:27700 for a selected feature."""
    return gpd.GeoDataFrame.from_features([feature], crs=config.DISPLAY_CRS).to_crs(
        config.NATIVE_CRS
    )


def candidate_label(candidate, result):
    """Date and cloud of an image, with its tile when the date isn't unique.

    One acquisition can be published on two overlapping tiles, giving two
    images with the same date.
    """
    label = f"{candidate.image_date} · {candidate.cloud:.1f}% cloud"
    same_date = [c for c in result.candidates if c.image_date == candidate.image_date]
    if len(same_date) > 1 and candidate.tile:
        label = f"{label} · {candidate.tile}"
    return f"{label} (clearest)" if candidate == result.best else label


def choice_options(result):
    """Dropdown options for every image that passed, in date order."""
    return [
        {"label": candidate_label(c, result), "value": c.image_link}
        for c in result.candidates
    ]


def site_size_m(feature):
    """Longest side of the feature's bounding box in metres (EPSG:27700)."""
    minx, miny, maxx, maxy = aoi_from_feature(feature).total_bounds
    return max(maxx - minx, maxy - miny)


def site_problem(feature, max_size_m=config.MAX_SITE_SIZE_M):
    """Why a site can't be searched for images, or None if it can."""
    size = site_size_m(feature)
    if size > max_size_m:
        return (
            f"This site is {size / 1000:,.1f} km across, too large to check "
            f"with Sentinel-2 chips (the limit is {max_size_m / 1000:g} km)."
        )
    return None


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


def council_options(sites):
    """Dropdown options for the councils in `sites`, (keyval, council) pairs."""
    councils = sorted({c for _, c in sites if c})
    return [{"label": "All councils", "value": ALL_COUNCILS}] + [
        {"label": c, "value": c} for c in councils
    ]


def _shuffle_key(keyval):
    # A hash rather than `random`, so the order is the same every run and a
    # site keeps its place relative to the others when filtering by council.
    return hashlib.sha256(str(keyval).encode()).hexdigest()


def step_keyvals(sites, council=ALL_COUNCILS, shuffle=False):
    """The keyvals Next and Previous step through.

    `sites` are (keyval, council) pairs in council then reference order.
    Keeps one council's sites unless `council` is all, and with `shuffle`
    puts them in a fixed pseudo-random order, so councils are mixed.
    """
    keyvals = [
        k for k, c in sites if council in (None, "", ALL_COUNCILS) or c == council
    ]
    return sorted(keyvals, key=_shuffle_key) if shuffle else keyvals


def step_target(keyvals, current, direction, skip=frozenset()):
    """Index of the next site after `current` in `direction` (1 or -1).

    Sites in `skip` are passed over. If `current` isn't in the list, Next
    starts at the first site and Previous at the last. None at either end.
    """
    try:
        i = keyvals.index(current)
    except ValueError:
        i = -1 if direction > 0 else len(keyvals)
    i += direction
    while 0 <= i < len(keyvals):
        if keyvals[i] not in skip:
            return i
        i += direction
    return None


def position_text(keyvals, current, reviewed):
    """Where the selected site is in the step list, and how many are reviewed."""
    done = len(reviewed.intersection(keyvals))
    count = f"{len(keyvals):,} sites · {done:,} reviewed"
    if current in keyvals:
        return f"Site {keyvals.index(current) + 1:,} of {count}"
    return count


def window_choices(results, choice_values, months, keyval):
    """A `WindowChoice` per window from the panels' current state.

    A window with a result records its searched month and the image shown;
    one without records the month typed in, and no image.
    """
    choices = []
    for name, data, link, month in zip(
        config.WINDOW_NAMES, results, choice_values, months, strict=True
    ):
        candidate = None
        if data and data["feature"]["properties"].get("keyval") == keyval:
            result = imagery.SearchResult.from_dict(data)
            month = result.window.key
            candidate = next(
                (c for c in result.candidates if c.image_link == link), None
            )
        choices.append(WindowChoice(name, month or None, candidate))
    return choices


def review_status(record, failed=()):
    """What's saved for the selected site."""
    if not record:
        return _muted("Not reviewed yet.")
    exported = sum(1 for name in config.WINDOW_NAMES if record.get(f"{name}_chip"))
    saved_at = record["saved_at"].replace("T", " ")[:16]
    children = [
        html.P(
            [
                html.Span(
                    # .get: reviews saved before the verdicts were split
                    # still show.
                    "Saved: "
                    + reviews.VERDICTS.get(record["verdict"], record["verdict"]),
                    className="fw-bold",
                ),
                f" · {saved_at} · {exported} image{'s' * (exported != 1)} exported",
            ],
            className="text-success small mb-1",
        )
    ]
    if failed:
        children.append(
            _warning(
                f"Couldn't read the {' and '.join(failed)} image from CEDA, so it "
                "wasn't exported. Save again to retry."
            )
        )
    return children


def review_edited(review, verdict, note):
    """True if the verdict or note differ from what's saved for the site."""
    saved_verdict = review["verdict"] if review else None
    saved_note = (review["note"] if review else None) or ""
    return verdict != saved_verdict or (note or "").strip() != saved_note.strip()


UNSAVED_MESSAGE = "Not saved yet: press Save before moving on."


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
                    # Native selects rather than <input type="month">, which
                    # Firefox shows as a plain text box with no picker.
                    dbc.Select(
                        id=window_id("month", index),
                        options=MONTH_OPTIONS,
                        placeholder="Month",
                        size="sm",
                        style={"width": "6rem"},
                    ),
                    dbc.Select(
                        id=window_id("year", index),
                        options=year_options(),
                        placeholder="Year",
                        size="sm",
                        style={"width": "6rem"},
                    ),
                ],
                className="d-flex align-items-center gap-1",
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
        html.H4("eo-change-viewer", className="mt-2"),
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
        html.Div(
            [
                dbc.ButtonGroup(
                    [
                        dbc.Button(
                            "◀ Previous",
                            id="prev-button",
                            n_clicks=0,
                            outline=True,
                            color="secondary",
                        ),
                        dbc.Button(
                            "Next ▶",
                            id="next-button",
                            n_clicks=0,
                            outline=True,
                            color="secondary",
                        ),
                    ],
                    size="sm",
                ),
                dbc.Switch(
                    id="skip-reviewed",
                    label="Skip reviewed",
                    value=True,
                    persistence=True,
                    className="small mb-0",
                ),
            ],
            className="d-flex align-items-center gap-3",
        ),
        html.Div(
            [
                html.Div(
                    dcc.Dropdown(
                        id="council",
                        options=[{"label": "All councils", "value": ALL_COUNCILS}],
                        value=ALL_COUNCILS,
                        clearable=False,
                    ),
                    className="flex-grow-1 small",
                ),
                dbc.Switch(
                    id="shuffle-sites",
                    label="Shuffle",
                    value=False,
                    persistence=True,
                    className="small mb-0",
                ),
            ],
            className="d-flex align-items-center gap-3 mt-2",
            title="The council to step through, and whether to step in a fixed "
            "shuffled order that mixes councils.",
        ),
        html.Div(
            id="step-status",
            className="small text-muted mb-2",
            title="Next and Previous step through the year and development type "
            "shown on the map, in council and reference order unless shuffled.",
        ),
        dbc.Label("Verdict", html_for="verdict", className="mb-0"),
        dbc.RadioItems(
            id="verdict",
            options=[{"label": v, "value": k} for k, v in reviews.VERDICTS.items()],
            inline=True,
            className="small",
        ),
        dbc.Textarea(id="note", placeholder="Note (optional)", size="sm", rows=2),
        html.Div(
            [
                dbc.Button("Save", id="save-button", n_clicks=0, size="sm"),
                dbc.Button(
                    "Save and next",
                    id="save-next-button",
                    n_clicks=0,
                    size="sm",
                    outline=True,
                    color="primary",
                ),
            ],
            className="d-flex gap-2 mt-2",
            title="Saves the verdict and the images shown, and exports them.",
        ),
        dcc.Loading(
            html.Div(
                id="review-status", className="mt-2", style={"minHeight": "1.5rem"}
            ),
            custom_spinner=html.Div(
                [dbc.Spinner(size="sm"), " Saving and exporting…"],
                className="small text-muted mt-2",
            ),
        ),
        dbc.Switch(
            id="show-basemap-date",
            label="Show basemap image date (slower)",
            value=False,
            persistence=True,
            className="small",
        ),
        html.Div(id="attributes", children=attributes_panel(None)),
        dcc.Store(id="step-request"),
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


def create_app(
    client=None,
    image_search=None,
    chip_cache=None,
    review_store=None,
    export_dir=config.EXPORT_DIR,
):
    """Build the Dash app.

    `client` defaults to one configured from `.env`; `image_search`,
    `chip_cache` and `review_store` to the real CEDA search, the on-disk
    cache and the reviews GeoPackage.
    """
    client = client or PlanningClient.from_env()
    image_search = image_search or imagery.ImageSearch()
    chip_cache = chip_cache or Cache()
    review_store = review_store or ReviewStore()

    @functools.lru_cache(maxsize=8)
    def site_list(year, dev_type):
        """(keyval, council) pairs. Errors aren't cached, so are retried."""
        return tuple(
            (s["keyval"], s["local_auth"]) for s in client.site_list(year, dev_type)
        )

    def stepped_keyvals(year, dev_type, council, shuffle):
        sites = site_list(int(year), dev_type_value(dev_type))
        return step_keyvals(sites, council, shuffle)

    def selected_review(feature):
        return review_store.get(feature["properties"]["keyval"]) if feature else None

    app = Dash(
        __name__,
        title="eo-change-viewer",
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
        Input("prev-button", "n_clicks"),
        Input("next-button", "n_clicks"),
        Input("step-request", "data"),
        State("search-store", "data"),
        State("year", "value"),
        State("dev-type", "value"),
        State("map", "zoom"),
        State("selected", "data"),
        State("skip-reviewed", "value"),
        State("council", "value"),
        State("shuffle-sites", "value"),
        prevent_initial_call=True,
    )
    def select(
        click,
        hit_clicks,
        _prev,
        _next,
        _step,
        search_features,
        year,
        dev_type,
        zoom,
        current,
        skip_reviewed,
        council,
        shuffle,
    ):
        trigger = ctx.triggered_id
        if trigger in ("prev-button", "next-button", "step-request"):
            direction = -1 if trigger == "prev-button" else 1
            return step_to(
                current, direction, year, dev_type, council, shuffle, skip_reviewed
            )
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

    def step_to(current, direction, year, dev_type, council, shuffle, skip_reviewed):
        """Select the next or previous site in the step list."""
        current_keyval = current["properties"].get("keyval") if current else None
        try:
            keyvals = stepped_keyvals(year, dev_type, council, shuffle)
            skip = review_store.reviewed() if skip_reviewed else set()
            index = step_target(keyvals, current_keyval, direction, skip)
            if index is None:
                unreviewed = " unreviewed" if skip_reviewed else ""
                way = "after" if direction > 0 else "before"
                return no_update, no_update, _info(f"No{unreviewed} sites {way} this.")
            gdf = client.by_keyval(year, keyvals[index])
        except PlanningError as e:
            return no_update, no_update, _error(str(e))
        if gdf is None:
            return no_update, no_update, _error("That site is no longer available.")
        feature = to_features(gdf)[0]
        viewport = {"bounds": view_bounds(feature), "transition": "flyToBounds"}
        return feature, viewport, None

    @app.callback(
        Output("council", "options"),
        Output("council", "value"),
        Input("year", "value"),
        Input("dev-type", "value"),
        State("council", "value"),
    )
    def update_councils(year, dev_type, council):
        try:
            options = council_options(site_list(int(year), dev_type_value(dev_type)))
        except PlanningError:
            return no_update, no_update
        values = {o["value"] for o in options}
        return options, council if council in values else ALL_COUNCILS

    @app.callback(
        Output("step-status", "children"),
        Input("selected", "data"),
        Input("year", "value"),
        Input("dev-type", "value"),
        Input("council", "value"),
        Input("shuffle-sites", "value"),
        Input("review-status", "children"),
    )
    def show_position(feature, year, dev_type, council, shuffle, _review_status):
        try:
            keyvals = stepped_keyvals(year, dev_type, council, shuffle)
        except PlanningError:
            return "Couldn't load the site list."
        current = feature["properties"].get("keyval") if feature else None
        return position_text(keyvals, current, review_store.reviewed())

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
        Output(window_id("year", ALL), "value"),
        Output("max-cloud", "value"),
        Input("selected", "data"),
    )
    def prefill_windows(feature):
        review = selected_review(feature)
        max_cloud = review["max_cloud"] if review_has_windows(review) else no_update
        values = default_window_values(feature, review)
        months, years = zip(*map(split_month, values), strict=True)
        return list(months), list(years), max_cloud

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
        State(window_id("year", ALL), "value"),
        State("max-cloud", "value"),
        prevent_initial_call=True,
    )
    def request_windows(n_clicks, feature, months, years, max_cloud):
        problem = site_problem(feature) if feature else None
        if problem:
            # Only say why when asked; a new selection just clears the panels.
            message = _warning(problem) if ctx.triggered_id == "find-button" else None
            return [None] * config.N_WINDOWS, message
        if ctx.triggered_id != "find-button":
            # A new site clears the old site's images, or brings back the
            # images saved in its review.
            review = selected_review(feature)
            if not review_has_windows(review):
                return [None] * config.N_WINDOWS, None
            months, links = zip(*reviews.saved_windows(review), strict=True)
            return (
                window_requests(feature, months, review["max_cloud"], 0, links),
                None,
            )
        if not feature:
            return [no_update] * config.N_WINDOWS, _muted("Select a site first.")
        values = list(map(join_month, months, years))
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
            # Remote image reads run on workers, never on the server's
            # per-connection threads; see workers.py.
            result = workers.run(
                cached_search, chip_cache, image_search, aoi, window, max_cloud
            )
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
        data = {
            "feature": request["feature"],
            "max_cloud": max_cloud,
            **result.to_dict(),
        }
        links = [c.image_link for c in result.candidates]
        prefer = request.get("prefer")
        value = prefer if prefer in links else result.best.image_link
        return data, status, choice_options(result), value

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
            _, overlay_url, bounds = workers.run(
                cached_chip,
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
            html.Span(candidate_label(candidate, result), className="fw-bold"),
            html.Span(f" · {source}", className="text-muted"),
        ]
        choice_style = {} if len(result.candidates) > 1 else hidden
        # The panel map and the main map's overlay show the same PNG.
        return overlay_url, bounds, caption, choice_style, overlay_url, bounds

    # --- reviews ------------------------------------------------------------

    @app.callback(
        Output("verdict", "value"),
        Output("note", "value"),
        Output("review-status", "children"),
        Input("selected", "data"),
    )
    def load_review(feature):
        if not feature:
            return None, "", None
        review = selected_review(feature)
        if not review:
            return None, "", review_status(None)
        return review["verdict"], review["note"] or "", review_status(review)

    # Warns while the verdict or note differ from what's saved, since Next
    # and Previous don't save.
    @app.callback(
        Output("review-status", "children", allow_duplicate=True),
        Input("verdict", "value"),
        Input("note", "value"),
        State("selected", "data"),
        prevent_initial_call=True,
    )
    def flag_unsaved(verdict, note, feature):
        if not feature:
            return no_update
        review = selected_review(feature)
        if review_edited(review, verdict, note):
            return _warning(UNSAVED_MESSAGE)
        return review_status(review)

    @app.callback(
        Output("review-status", "children", allow_duplicate=True),
        Output("step-request", "data"),
        Input("save-button", "n_clicks"),
        Input("save-next-button", "n_clicks"),
        State("selected", "data"),
        State("verdict", "value"),
        State("note", "value"),
        State(window_id("result", ALL), "data"),
        State(window_id("choice", ALL), "value"),
        State(window_id("month", ALL), "value"),
        State(window_id("year", ALL), "value"),
        State("max-cloud", "value"),
        prevent_initial_call=True,
    )
    def save_review(
        save_clicks,
        next_clicks,
        feature,
        verdict,
        note,
        results,
        links,
        months,
        years,
        cloud,
    ):
        if not feature:
            return _muted("Select a site first."), no_update
        if verdict not in reviews.VERDICTS:
            return _warning("Choose a verdict first."), no_update
        keyval = feature["properties"]["keyval"]
        values = list(map(join_month, months, years))
        choices = window_choices(results, links, values, keyval)
        problem = reviews.verdict_problem(verdict, choices)
        if problem:
            return _warning(problem), no_update
        # The threshold the shown images were found with, if any were.
        max_cloud = next(
            (r["max_cloud"] for r in results if r and r.get("max_cloud")),
            float(cloud or config.S2CLOUDLESS_MAX),
        )
        geom = aoi_from_feature(feature).geometry.iloc[0]
        chips, failed = workers.run(
            reviews.export_site, chip_cache, keyval, choices, geom, export_dir
        )
        record = reviews.review_record(
            feature["properties"], verdict, note, max_cloud, choices, chips
        )
        try:
            review_store.save(record, geom)
        except OSError as e:
            return _error(f"Couldn't save the review: {e.strerror}"), no_update
        step = no_update
        if ctx.triggered_id == "save-next-button" and not failed:
            # Picked up by `select`, which steps to the next site.
            step = {"n": next_clicks}
        return review_status(record, failed), step

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
