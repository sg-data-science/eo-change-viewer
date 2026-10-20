"""Shared test helpers: a fake HTTP session and the recorded GeoServer fixtures."""

import json
from pathlib import Path

import pytest
import requests

from change_viewer.planning import WfsConfig

FIXTURES = Path(__file__).parent / "fixtures"

# The recorded exception report echoes the auth key; it was replaced with this.
FAKE_KEY = "FAKEKEY"
FAKE_URL = (
    "https://geo.example.org/geoserver/sh_plnapp_premium/wfs?service=WFS"
    f"&authkey={FAKE_KEY}&request=GetCapabilities"
    "&typeName=sh_plnapp_premium:pub_plnapppol"
)


class FakeResponse:
    def __init__(self, body=b"", status=200, content_type="application/json"):
        self.content = body if isinstance(body, bytes) else body.encode()
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    @property
    def ok(self):
        return self.status_code < 400

    @property
    def text(self):
        return self.content.decode()

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Records each GET and returns a fixed response, or raises an exception."""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.response


def fixture_text(name):
    return (FIXTURES / name).read_text()


@pytest.fixture
def wfs_config():
    return WfsConfig.from_url(FAKE_URL)
