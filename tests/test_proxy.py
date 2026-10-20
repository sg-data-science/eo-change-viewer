"""WMS tile proxy: parameter whitelisting, CQL building and key handling."""

from datetime import date

import pytest
import requests
from flask import Flask

from change_viewer import config
from change_viewer.proxy import (
    BadTileRequest,
    parse_tile_request,
    register_wms_proxy,
    tile_filter,
    upstream_params,
)
from tests.conftest import FAKE_KEY, FakeResponse, FakeSession

GOOD = {
    "bbox": "-360000,7540000,-350000,7550000",
    "width": "256",
    "height": "256",
    "year": "2022",
}
PNG = b"\x89PNG\r\n\x1a\nfake"


def proxy_client(wfs_config, response=None, exc=None):
    session = FakeSession(response, exc)
    server = Flask(__name__)
    register_wms_proxy(server, wfs_config, session=session)
    return server.test_client(), session


# --- parameter checks -------------------------------------------------------


def test_parse_good_request():
    tile = parse_tile_request({**GOOD, "dev_type": "housing"})
    assert tile == {
        "bbox": [-360000.0, 7540000.0, -350000.0, 7550000.0],
        "width": 256,
        "height": 256,
        "year": 2022,
        "dev_type": "housing",
    }


def test_parse_ignores_other_params():
    tile = parse_tile_request(
        {
            **GOOD,
            "layers": "secret:other_layer",
            "styles": "x",
            "CQL_FILTER": "1=1",
            "authkey": "attacker",
            "request": "GetFeatureInfo",
        }
    )
    assert set(tile) == {"bbox", "width", "height", "year", "dev_type"}


def test_parse_is_case_insensitive():
    tile = parse_tile_request({k.upper(): v for k, v in GOOD.items()})
    assert tile["year"] == 2022


@pytest.mark.parametrize(
    "change",
    [
        {"bbox": "1,2,3"},
        {"bbox": "a,b,c,d"},
        {"bbox": "3,2,1,4"},
        {"bbox": "1,2,nan,4"},
        {"width": "0"},
        {"height": "5000"},
        {"width": "abc"},
        {"year": "2022 OR 1=1"},
        {"year": "1999"},
        {"year": str(date.today().year + 1)},
        {"dev_type": "housing' OR '1'='1"},
        {"dev_type": "not a category"},
    ],
)
def test_parse_rejects_bad_values(change):
    with pytest.raises(BadTileRequest):
        parse_tile_request({**GOOD, **change})


@pytest.mark.parametrize("missing", ["bbox", "width", "height", "year"])
def test_parse_rejects_missing(missing):
    args = {k: v for k, v in GOOD.items() if k != missing}
    with pytest.raises(BadTileRequest):
        parse_tile_request(args)


def test_tile_filter():
    assert tile_filter(2022) == "year=2022"
    assert tile_filter(2022, "housing") == "year=2022 AND dev_desc_is='housing'"


def test_all_dev_types_quote_cleanly():
    for dev_type in config.DEV_TYPES:
        assert tile_filter(2022, dev_type).endswith(f"='{dev_type}'")


def test_upstream_params(wfs_config):
    params = upstream_params(
        parse_tile_request({**GOOD, "dev_type": "housing"}), wfs_config
    )
    assert params["authkey"] == FAKE_KEY
    assert params["layers"] == "pub_plnapppol"
    assert params["srs"] == "EPSG:3857"
    assert params["request"] == "GetMap"
    assert params["CQL_FILTER"] == "year=2022 AND dev_desc_is='housing'"
    assert "<Name>pub_plnapppol</Name>" in params["SLD_BODY"]


# --- route ------------------------------------------------------------------


def test_route_returns_tile(wfs_config):
    client, session = proxy_client(wfs_config, FakeResponse(PNG, 200, "image/png"))
    resp = client.get("/wms", query_string={**GOOD, "layers": "planning"})
    assert resp.status_code == 200
    assert resp.data == PNG
    assert resp.mimetype == "image/png"
    (call,) = session.calls
    assert call["url"] == wfs_config.wms_endpoint
    assert call["params"]["CQL_FILTER"] == "year=2022"
    assert call["params"]["layers"] == "pub_plnapppol"


def test_route_rejects_bad_params_without_upstream_call(wfs_config):
    client, session = proxy_client(wfs_config, FakeResponse(PNG, 200, "image/png"))
    resp = client.get("/wms", query_string={**GOOD, "year": "x"})
    assert resp.status_code == 400
    assert session.calls == []


def test_route_hides_upstream_error_body(wfs_config):
    body = f'<ServiceExceptionReport schemaLocation="x?authkey={FAKE_KEY}"/>'
    client, _ = proxy_client(wfs_config, FakeResponse(body, 200, "application/xml"))
    resp = client.get("/wms", query_string=GOOD)
    assert resp.status_code == 502
    assert FAKE_KEY.encode() not in resp.data


def test_route_hides_network_error(wfs_config):
    exc = requests.ConnectionError(f"url: /wms?authkey={FAKE_KEY}")
    client, _ = proxy_client(wfs_config, exc=exc)
    resp = client.get("/wms", query_string=GOOD)
    assert resp.status_code == 502
    assert FAKE_KEY.encode() not in resp.data
