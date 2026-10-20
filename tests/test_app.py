"""Dash app: helper functions, and that the app builds without the network."""

import json
from datetime import date

import pytest
from shapely.geometry import shape

from change_viewer import app as app_module
from change_viewer import basemap
from change_viewer.planning import PlanningClient, WfsConfig, parse_features
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
    panel = str(app_module.attributes_panel(features[0]))
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
    panel = str(app_module.attributes_panel(features[0]))
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
    assert str(first_id["keyval"]) in str(app_module.attributes_panel(first))
