"""Dash app: helper functions, and that the app builds without the network."""

import json
import re
from datetime import date
from pathlib import Path

import dash_leaflet as dl
import geopandas as gpd
import pytest
from shapely.geometry import box, shape

from change_viewer import app as app_module
from change_viewer import basemap, config, imagery
from change_viewer.cache import Cache
from change_viewer.imagery import Candidate, SearchResult, Window
from change_viewer.planning import PlanningClient, WfsConfig, parse_features
from change_viewer.reviews import ReviewStore
from tests.conftest import FAKE_KEY, FAKE_URL, FakeSession, fixture_text


@pytest.fixture
def features():
    gdf = parse_features(json.loads(fixture_text("wfs_two_features.json")))
    return app_module.to_features(gdf)


def test_to_features_is_wgs84_and_keeps_flags(features):
    assert len(features) == 2
    lon, lat = shape(features[0]["geometry"]).centroid.coords[0]
    assert -8 < lon < 0 and 54 < lat < 61
    assert {f["properties"]["is_placeholder"] for f in features} == {True, False}
    json.dumps(features)  # must be storable in a dcc.Store


def test_click_tolerance_shrinks_with_zoom():
    coarse = app_module.click_tolerance_m(10, 56)
    fine = app_module.click_tolerance_m(18, 56)
    assert coarse > fine >= 1.0


def test_view_bounds_has_minimum_size(features):
    (miny, minx), (maxy, maxx) = app_module.view_bounds(features[0])
    assert maxy - miny == pytest.approx(300 / 111_320, rel=0.01)
    assert minx < maxx


def test_attributes_panel_flags_placeholder(features):
    placeholder = next(f for f in features if f["properties"]["is_placeholder"])
    site = next(f for f in features if not f["properties"]["is_placeholder"])
    assert "Approximate location only" in str(app_module.attributes_panel(placeholder))
    assert "Approximate location only" not in str(app_module.attributes_panel(site))
    assert site["properties"]["url"] in str(app_module.attributes_panel(site))


def test_wms_params():
    assert app_module._wms_params(2022, "housing") == {
        "layers": "planning",
        "year": 2022,
        "dev_type": "housing",
    }
    assert "dev_type" not in app_module._wms_params(2022, app_module.ALL_TYPES)


def test_app_builds_and_keeps_key_out_of_layout():
    client = PlanningClient(WfsConfig.from_url(FAKE_URL), session=FakeSession())
    dash_app = app_module.create_app(client)
    layout = json.dumps(dash_app.layout.to_plotly_json(), default=str)
    assert FAKE_KEY not in layout
    assert "geo.example.org" not in layout
    rules = {r.rule for r in dash_app.server.url_map.iter_rules()}
    assert "/wms" in rules


@pytest.fixture
def fake_basemap(monkeypatch):
    """Replace the Esri lookup; yields a dict to set the result or error."""
    state = {"result": None, "error": None, "calls": 0}

    def fake_image_info(lon, lat):
        state["calls"] += 1
        if state["error"]:
            raise state["error"]
        return state["result"]

    monkeypatch.setattr(app_module.basemap, "image_info", fake_image_info)
    app_module._basemap_image_info.cache_clear()
    yield state
    app_module._basemap_image_info.cache_clear()


def test_basemap_date_row_follows_application_dates(features):
    panel = str(app_module.attributes_panel(features[0], True))
    assert app_module.BASEMAP_DATE_LABEL in panel
    assert panel.index("Decided") < panel.index(app_module.BASEMAP_DATE_LABEL)
    assert panel.index(app_module.BASEMAP_DATE_LABEL) < panel.index("'Key'")


def test_basemap_date_text(features, fake_basemap):
    fake_basemap["result"] = basemap.ImageInfo(date(2021, 9, 1), "Vantor", 0.31)
    assert app_module.basemap_date_text(features[0]) == "2021-09-01 (Vantor, 0.31 m)"
    app_module.basemap_date_text(features[0])
    assert fake_basemap["calls"] == 1  # cached


def test_basemap_date_text_not_recorded(features, fake_basemap):
    assert app_module.basemap_date_text(features[0]) == "Not recorded"


def test_basemap_date_text_error_is_retried(features, fake_basemap):
    fake_basemap["error"] = basemap.BasemapError("down")
    assert "Unavailable" in app_module.basemap_date_text(features[0])
    fake_basemap["error"] = None
    fake_basemap["result"] = basemap.ImageInfo(date(2021, 9, 1))
    assert app_module.basemap_date_text(features[0]) == "2021-09-01"


def test_panel_shows_looking_up_until_date_arrives(features):
    panel = str(app_module.attributes_panel(features[0], True))
    assert app_module.BASEMAP_DATE_LABEL in panel
    assert "Looking up" in panel


def test_basemap_date_cell_id_is_per_application(features):
    first, second = features
    first_id = app_module.basemap_date_cell_id(first)
    assert first_id == {
        "type": "basemap-date",
        "keyval": first["properties"]["keyval"],
    }
    assert first_id != app_module.basemap_date_cell_id(second)
    assert str(first_id["keyval"]) in str(app_module.attributes_panel(first, True))


def test_basemap_date_row_off_by_default(features):
    # No date cell means no lookup is triggered.
    panel = str(app_module.attributes_panel(features[0]))
    assert app_module.BASEMAP_DATE_LABEL not in panel
    assert "basemap-date" not in panel


# --- Sentinel-2 windows -------------------------------------------------------


def test_default_window_values(features):
    site = next(f for f in features if f["properties"]["keyval"] == "R6M0KPKVIRF00")
    # Received 2022-02-01, decided 2022-04-22.
    assert app_module.default_window_values(site) == ["2021-06", "2023-06", "2024-06"]
    assert app_module.default_window_values(None) == [None, None, None]


def test_window_requests(features):
    requests = app_module.window_requests(
        features[0], ["2021-06", None, ""], "7", n_clicks=3
    )
    assert [r["window"] for r in requests] == ["2021-06", None, None]
    assert {r["max_cloud"] for r in requests} == {7.0}
    assert {r["n"] for r in requests} == {3}


def test_window_problem():
    today = date(2026, 10, 20)
    assert app_module.window_problem(imagery.Window(2023, 6), today) is None
    assert "future" in app_module.window_problem(imagery.Window(2026, 11), today)
    assert "2015" in app_module.window_problem(imagery.Window(2014, 6), today)


def test_choice_options_mark_clearest():
    result = imagery.SearchResult(
        imagery.Window(2023, 6),
        [
            imagery.Candidate("a", "2023-06-04", 3.0),
            imagery.Candidate("b", "2023-06-16", 0.0),
        ],
    )
    options = app_module.choice_options(result)
    assert [o["value"] for o in options] == ["a", "b"]
    assert options[1]["label"] == "2023-06-16 · 0.0% cloud (clearest)"
    assert "clearest" not in options[0]["label"]


def test_aoi_from_feature_is_native_crs(features):
    aoi = app_module.aoi_from_feature(features[0])
    assert aoi.crs.to_epsg() == 27700
    assert aoi["keyval"].iloc[0] == features[0]["properties"]["keyval"]
    assert 300000 < aoi.geometry.iloc[0].centroid.x < 500000


@pytest.fixture
def dash_app(tmp_path):
    client = PlanningClient(WfsConfig.from_url(FAKE_URL), session=FakeSession())
    chip_cache = Cache(tmp_path / "cache")
    dash = app_module.create_app(
        client,
        chip_cache=chip_cache,
        review_store=ReviewStore(tmp_path / "output" / "reviews.gpkg"),
        export_dir=tmp_path / "output" / "chips",
    )
    return dash, chip_cache


def test_chip_route_serves_cached_png(dash_app):
    dash, chip_cache = dash_app
    path = chip_cache.path("K1", "img_b200_panel.png")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x89PNGfake")
    resp = dash.server.test_client().get("/chips/K1/img_b200_panel.png")
    assert resp.status_code == 200 and resp.data == b"\x89PNGfake"
    assert resp.mimetype == "image/png"


@pytest.mark.parametrize(
    "url",
    ["/chips/K1/missing.png", "/chips/K1/search_2023-06_c10.json", "/chips/K1/.x.png"],
)
def test_chip_route_rejects_other_files(dash_app, url):
    dash, _ = dash_app
    assert dash.server.test_client().get(url).status_code == 404


@pytest.mark.parametrize("url", ["/chips/K1/..%2F..%2F.env", "/chips/..%2F/x.png"])
def test_chip_route_traversal_serves_nothing(dash_app, url):
    # These don't match the chip route, so Dash serves its usual app page.
    dash, _ = dash_app
    resp = dash.server.test_client().get(url)
    assert resp.mimetype != "image/png"
    assert b"PLANNING_POLY_WFS" not in resp.data


# --- linked panel maps --------------------------------------------------------

SYNC_JS = Path(app_module.__file__).parent / "assets" / "sync_maps.js"


def _js_constant(name):
    match = re.search(rf'const {name} = "([^"]+)";', SYNC_JS.read_text())
    assert match, f"{name} not found in sync_maps.js"
    return match.group(1)


def _layout_ids(layout):
    return [getattr(c, "id", None) for c in layout._traverse()]


def test_sync_script_ids_match_layout(dash_app):
    # sync_maps.js finds maps and the switch by these ids; renaming one in
    # Python would silently stop the linking.
    dash, _ = dash_app
    ids = _layout_ids(dash.layout)
    assert _js_constant("MAIN_MAP_ID") in ids
    assert _js_constant("LINK_SWITCH_ID") in ids
    panels = [i for i in ids if isinstance(i, dict) and i["type"] == "window-panel"]
    assert len(panels) == 3
    # Dash gives a map with a dict id a JSON container id, which the script
    # searches for this text.
    assert all(_js_constant("PANEL_ID_PART") in json.dumps(i) for i in panels)


def test_panel_maps_have_no_basemap():
    # An empty panel should read as "no image", not as a picture of the site.
    panel = app_module._panel_map(0)
    assert not any(isinstance(c, dl.TileLayer) for c in panel._traverse())


# --- reviews and stepping ----------------------------------------------------

KEYVALS = ["A", "B", "C", "D"]


@pytest.mark.parametrize(
    ("current", "direction", "skip", "expected"),
    [
        ("B", 1, set(), 2),
        ("B", -1, set(), 0),
        ("B", 1, {"C"}, 3),
        ("D", 1, set(), None),
        ("A", -1, set(), None),
        ("B", 1, {"C", "D"}, None),
        (None, 1, set(), 0),  # nothing selected: Next starts at the first
        ("X", -1, set(), 3),  # not in the list: Previous starts at the last
        (None, 1, {"A"}, 1),
    ],
)
def test_step_target(current, direction, skip, expected):
    assert app_module.step_target(KEYVALS, current, direction, skip) == expected


def test_position_text():
    assert app_module.position_text(KEYVALS, "B", {"A", "Z"}) == (
        "Site 2 of 4 sites · 1 reviewed"
    )
    assert app_module.position_text(KEYVALS, "X", set()) == "4 sites · 0 reviewed"


LINK = "https://dap.ceda.ac.uk/x/S2B_20230604_T30VVH_utm30n_osgb_vmsk.tif"
OTHER = "https://dap.ceda.ac.uk/x/S2A_20230611_T30VVH_utm30n_osgb_vmsk.tif"


def _result_data(keyval, month="2023-06"):
    result = SearchResult(
        Window.parse(month),
        [Candidate(LINK, "2023-06-04", 2.0), Candidate(OTHER, "2023-06-11", 0.5)],
    )
    return {"feature": {"properties": {"keyval": keyval}}, **result.to_dict()}


def test_window_choices_records_the_shown_image():
    choices = app_module.window_choices(
        [_result_data("K1"), None, _result_data("OLD")],
        [LINK, None, LINK],
        ["2023-07", "2024-06", "2025-06"],
        "K1",
    )
    before, after, later = choices
    # The searched month counts, not one typed in since.
    assert before.month == "2023-06" and before.candidate.image_link == LINK
    assert after.month == "2024-06" and after.candidate is None
    # A result left over from another site is ignored.
    assert later.month == "2025-06" and later.candidate is None
    assert [c.name for c in choices] == list(config.WINDOW_NAMES)


def test_review_restores_windows_and_images(features):
    review = {f"{n}_month": None for n in config.WINDOW_NAMES}
    review |= {f"{n}_image_link": None for n in config.WINDOW_NAMES}
    review |= {"before_month": "2020-06", "before_image_link": LINK}
    assert app_module.default_window_values(features[0], review) == [
        "2020-06",
        None,
        None,
    ]
    requests = app_module.window_requests(
        features[0], ["2020-06", None, None], 10, 0, [LINK, None, None]
    )
    assert [r["prefer"] for r in requests] == [LINK, None, None]
    # Without a saved month, the defaults from the application dates are used.
    review["before_month"] = None
    assert app_module.default_window_values(features[0], review) == (
        app_module.default_window_values(features[0])
    )


def test_review_status_text():
    assert "Not reviewed" in str(app_module.review_status(None))
    record = {
        "verdict": "no change",
        "saved_at": "2026-10-20T14:03:09",
        "before_chip": "chips/K1/a.tif",
    }
    text = str(app_module.review_status(record, failed=["after"]))
    assert "No change" in text and "2026-10-20 14:03" in text
    assert "1 image exported" in text and "after image" in text


def _square_feature(side_m):
    """A square site of `side_m` metres, as a selected feature (EPSG:4326)."""
    gdf = gpd.GeoDataFrame(
        {"keyval": ["K1"]},
        geometry=[box(300000, 700000, 300000 + side_m, 700000 + side_m)],
        crs=config.NATIVE_CRS,
    )
    return app_module.to_features(gdf)[0]


def test_site_size_limit():
    assert app_module.site_size_m(_square_feature(500)) == pytest.approx(500, abs=0.1)
    assert app_module.site_problem(_square_feature(9_000)) is None
    message = app_module.site_problem(_square_feature(12_000))
    assert "12.0 km across" in message and "10 km" in message
    assert app_module.site_problem(_square_feature(500), max_size_m=400)
