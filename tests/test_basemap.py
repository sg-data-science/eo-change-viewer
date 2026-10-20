"""Esri basemap capture dates: parsing the identify response and errors."""

import json
from datetime import date

import pytest
import requests

from change_viewer.basemap import (
    BasemapError,
    ImageInfo,
    describe,
    image_info,
    parse_identify,
)
from tests.conftest import FakeResponse, FakeSession, fixture_text


def test_parse_recorded_response():
    info = parse_identify(json.loads(fixture_text("esri_identify.json")))
    assert info == ImageInfo(date(2021, 9, 1), "Vantor", 0.31)


def test_parse_picks_finest_layer_with_a_date():
    data = {
        "results": [
            {"layerId": 12, "attributes": {"SRC_DATE": "20200101", "SRC_RES": "1"}},
            {"layerId": 9, "attributes": {"SRC_DATE": "Null", "SRC_RES": "0.3"}},
            {"layerId": 10, "attributes": {"SRC_DATE": "20230327", "SRC_RES": "0.5"}},
        ]
    }
    assert parse_identify(data).date == date(2023, 3, 27)


def test_parse_accepts_numeric_values():
    data = {"results": [{"layerId": 9, "attributes": {"SRC_DATE": 20210901}}]}
    assert parse_identify(data) == ImageInfo(date(2021, 9, 1))


def test_parse_without_dates():
    data = {"results": [{"layerId": 17, "attributes": {"SRC_DATE": "Null"}}]}
    assert parse_identify(data) is None
    assert parse_identify({"results": []}) is None


def test_describe():
    assert describe(ImageInfo(date(2021, 9, 1), "Vantor", 0.31)) == (
        "2021-09-01 (Vantor, 0.31 m)"
    )
    assert describe(ImageInfo(date(2021, 9, 1))) == "2021-09-01"


def test_image_info_request():
    session = FakeSession(FakeResponse(fixture_text("esri_identify.json")))
    assert image_info(-3.84, 56.373, session=session).date == date(2021, 9, 1)
    params = session.calls[0]["params"]
    assert params["geometry"] == "-3.84,56.373"
    assert params["layers"] == "all:5,6,7,8,9,10,11,12,13,14,15,16"


@pytest.mark.parametrize(
    "session",
    [
        FakeSession(exc=requests.ConnectionError("no DNS")),
        FakeSession(FakeResponse("busy", 503, "text/plain")),
        FakeSession(FakeResponse("<html>", 200, "text/html")),
        FakeSession(FakeResponse('{"error": {"code": 400}}')),
    ],
)
def test_image_info_errors(session):
    with pytest.raises(BasemapError):
        image_info(-3.84, 56.373, session=session)
