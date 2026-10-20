"""WFS client: request building, CQL quoting, parsing and key redaction."""

import json

import pytest
import requests
from shapely.geometry import Point, box

from change_viewer.planning import (
    PlanningClient,
    PlanningError,
    WfsConfig,
    bbox_filter,
    cql_string,
    is_placeholder,
    keyval_filter,
    parse_features,
    search_filter,
)
from tests.conftest import FAKE_KEY, FAKE_URL, FakeResponse, FakeSession, fixture_text

PLACEHOLDER_KEYVAL = "R4GPX3HF0O100"
SITE_KEYVAL = "R6M0KPKVIRF00"


def client_with(response=None, exc=None):
    session = FakeSession(response, exc)
    return PlanningClient(WfsConfig.from_url(FAKE_URL), session=session), session


# --- config -----------------------------------------------------------------


def test_config_splits_url():
    cfg = WfsConfig.from_url(FAKE_URL)
    assert cfg.endpoint == "https://geo.example.org/geoserver/sh_plnapp_premium/wfs"
    assert cfg.type_name == "sh_plnapp_premium:pub_plnapppol"
    assert cfg.layer_name == "pub_plnapppol"
    assert cfg.authkey == FAKE_KEY
    assert cfg.wms_endpoint.endswith("/sh_plnapp_premium/wms")


def test_config_repr_hides_key():
    assert FAKE_KEY not in repr(WfsConfig.from_url(FAKE_URL))


def test_config_requires_key_and_layer():
    with pytest.raises(ValueError, match="authkey"):
        WfsConfig.from_url("https://x/wfs?typeName=a:b")


# --- CQL --------------------------------------------------------------------


def test_cql_string_doubles_quotes():
    assert cql_string("Dobbie's Road") == "'Dobbie''s Road'"


def test_cql_string_neutralises_injection():
    # The whole input stays inside one string literal.
    quoted = cql_string("x' OR 1=1 OR 'a'='a")
    assert quoted == "'x'' OR 1=1 OR ''a''=''a'"


def test_search_filter_starts_with_year():
    cql = search_filter(2022, "  dobbie's  ")
    assert cql.startswith("year=2022 AND ")
    assert "reference ILIKE '%dobbie''s%'" in cql
    assert "address ILIKE '%dobbie''s%'" in cql
    assert "keyval='DOBBIE''S'" in cql


def test_search_filter_rejects_empty_text():
    with pytest.raises(ValueError):
        search_filter(2022, "   ")


def test_search_filter_coerces_year():
    with pytest.raises(ValueError):
        search_filter("2022 OR 1=1", "x")


def test_bbox_filter():
    cql = bbox_filter(2022, (1, 2, 3, 4), dev_type="housing")
    assert cql == "year=2022 AND BBOX(geom,1.0,2.0,3.0,4.0) AND dev_desc_is='housing'"


def test_bbox_filter_without_dev_type():
    assert bbox_filter(2022, (1, 2, 3, 4)) == "year=2022 AND BBOX(geom,1.0,2.0,3.0,4.0)"


def test_keyval_filter():
    assert keyval_filter(2022, "R6M0") == "year=2022 AND keyval='R6M0'"


# --- parsing ----------------------------------------------------------------


def test_parse_features_from_fixture():
    gdf = parse_features(json.loads(fixture_text("wfs_two_features.json")))
    assert len(gdf) == 2
    assert gdf.crs.to_epsg() == 27700
    flags = dict(zip(gdf["keyval"], gdf["is_placeholder"], strict=True))
    assert flags == {SITE_KEYVAL: False, PLACEHOLDER_KEYVAL: True}


def test_parse_features_empty():
    gdf = parse_features(json.loads(fixture_text("wfs_empty.json")))
    assert gdf.empty
    assert gdf.crs.to_epsg() == 27700
    assert "is_placeholder" in gdf.columns


def test_is_placeholder_shapes():
    circle = Point(300000, 700000).buffer(10, quad_segs=8)
    assert is_placeholder(circle)
    assert not is_placeholder(Point(300000, 700000).buffer(25, quad_segs=8))
    assert not is_placeholder(box(0, 0, 14, 14))


# --- client -----------------------------------------------------------------


def test_get_features_sends_expected_params():
    client, session = client_with(FakeResponse(fixture_text("wfs_empty.json")))
    client.get_features("year=2022", count=7)
    (call,) = session.calls
    assert call["url"] == client.config.endpoint
    params = call["params"]
    assert params["authkey"] == FAKE_KEY
    assert params["typeNames"] == "sh_plnapp_premium:pub_plnapppol"
    assert params["CQL_FILTER"] == "year=2022"
    assert params["count"] == 7
    assert params["outputFormat"] == "application/json"


def test_search_sorts_by_reference():
    client, session = client_with(FakeResponse(fixture_text("wfs_two_features.json")))
    gdf = client.search(2022, "x")
    assert list(gdf["reference"]) == sorted(gdf["reference"])
    assert session.calls[0]["params"]["CQL_FILTER"].startswith("year=2022 AND ")


def test_at_point_builds_bbox_in_27700_and_sorts_smallest_first():
    client, session = client_with(FakeResponse(fixture_text("wfs_two_features.json")))
    # Roughly the centre of the site polygon in the fixture.
    gdf = client.at_point(2022, -3.1046, 55.8667, tolerance_m=5, dev_type="housing")
    cql = session.calls[0]["params"]["CQL_FILTER"]
    assert cql.startswith("year=2022 AND BBOX(geom,")
    assert cql.endswith("AND dev_desc_is='housing'")
    minx, miny, maxx, maxy = (
        float(v) for v in cql.split("BBOX(geom,")[1].split(")")[0].split(",")
    )
    assert maxx - minx == pytest.approx(10)
    assert 300000 < minx < 400000 and 600000 < miny < 700000
    areas = list(gdf.geometry.area)
    assert areas == sorted(areas)


def test_by_keyval_returns_none_when_missing():
    client, _ = client_with(FakeResponse(fixture_text("wfs_empty.json")))
    assert client.by_keyval(2022, "NOSUCHKEY") is None


def test_by_keyval_returns_geodataframe():
    client, session = client_with(FakeResponse(fixture_text("wfs_two_features.json")))
    gdf = client.by_keyval(2022, SITE_KEYVAL)
    assert len(gdf) >= 1 and gdf.crs.to_epsg() == 27700
    assert session.calls[0]["params"]["CQL_FILTER"] == (
        f"year=2022 AND keyval='{SITE_KEYVAL}'"
    )


# --- errors never contain the key -------------------------------------------


def test_server_exception_is_redacted():
    # The recorded report echoes the auth key in its schemaLocation.
    body = fixture_text("wfs_exception.xml")
    assert FAKE_KEY in body
    client, _ = client_with(FakeResponse(body, 400, "application/xml"))
    with pytest.raises(PlanningError) as info:
        client.get_features("year=2022 AND nonsense((")
    assert "HTTP 400" in str(info.value)
    assert "Could not parse CQL" in str(info.value)
    assert FAKE_KEY not in str(info.value)


def test_non_json_ok_response_is_redacted():
    body = fixture_text("wfs_exception.xml")
    client, _ = client_with(FakeResponse(body, 200, "application/xml"))
    with pytest.raises(PlanningError) as info:
        client.get_features("year=2022")
    assert FAKE_KEY not in str(info.value)


def test_forbidden_html_response():
    client, _ = client_with(FakeResponse("<html>403</html>", 403, "text/html"))
    with pytest.raises(PlanningError, match="HTTP 403"):
        client.get_features("year=2022")


@pytest.mark.parametrize(
    "exc",
    [
        requests.ConnectionError(
            f"Max retries exceeded with url: /wfs?authkey={FAKE_KEY}"
        ),
        requests.Timeout(f"Read timed out: {FAKE_URL}"),
    ],
)
def test_network_errors_hide_url_and_key(exc):
    client, _ = client_with(exc=exc)
    with pytest.raises(PlanningError) as info:
        client.get_features("year=2022")
    message = str(info.value)
    assert FAKE_KEY not in message
    assert "geo.example.org" not in message
    # No chained exception carrying the original text into a traceback.
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__
