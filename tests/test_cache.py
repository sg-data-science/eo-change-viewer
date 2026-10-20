"""On-disk cache of searches and chips."""

from datetime import date

import geopandas as gpd
import pytest
from shapely.geometry import box

from change_viewer import cache as cache_module
from change_viewer.cache import Cache, cached_chip, cached_search
from change_viewer.imagery import Candidate, SearchResult, Window
from change_viewer.render import Chip

LINK = "https://dap.ceda.ac.uk/x/S2B_20230604_T30VVH_utm30n_osgb_vmsk.tif"


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path)


@pytest.fixture
def aoi():
    return gpd.GeoDataFrame(
        {"keyval": ["K1"]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:27700"
    )


class CountingSearch:
    def __init__(self):
        self.calls = 0

    def settings_key(self, s2cloudless_max=None):
        return f"c{s2cloudless_max:g}"

    def search(self, aoi, window, s2cloudless_max=None):
        self.calls += 1
        return SearchResult(window, [Candidate(LINK, window.start.isoformat(), 1.0)])


def test_search_round_trip(cache):
    result = SearchResult(Window(2023, 6), [Candidate(LINK, "2023-06-04", 0.0)])
    cache.save_search("K1", result, "c10")
    assert cache.load_search("K1", Window(2023, 6), "c10") == result
    assert cache.load_search("K1", Window(2023, 6), "c5") is None


def test_cached_search_hits_cache(cache, aoi):
    search = CountingSearch()
    today = date(2026, 10, 20)
    first = cached_search(cache, search, aoi, Window(2023, 6), 10, today=today)
    second = cached_search(cache, search, aoi, Window(2023, 6), 10, today=today)
    assert first == second and search.calls == 1
    cached_search(cache, search, aoi, Window(2023, 6), 5, today=today)
    assert search.calls == 2  # different cloud threshold


def test_unfinished_window_is_not_cached(cache, aoi):
    search = CountingSearch()
    today = date(2026, 10, 20)
    cached_search(cache, search, aoi, Window(2026, 10), 10, today=today)
    cached_search(cache, search, aoi, Window(2026, 10), 10, today=today)
    assert search.calls == 2


def test_cached_chip_renders_once(cache, monkeypatch):
    calls = []

    def fake_render(link, geom):
        calls.append(link)
        return Chip(b"panel", b"overlay", [[1, 2], [3, 4]])

    monkeypatch.setattr(cache_module.render, "render_chip", fake_render)
    candidate = Candidate(LINK, "2023-06-04", 0.0)
    urls = cached_chip(cache, "K1", candidate, box(0, 0, 1, 1))
    assert cached_chip(cache, "K1", candidate, box(0, 0, 1, 1)) == urls
    assert len(calls) == 1
    panel_url, overlay_url, bounds = urls
    assert panel_url.startswith("/chips/K1/") and panel_url.endswith("_panel.png")
    assert bounds == [[1, 2], [3, 4]]
    assert cache.path("K1", panel_url.rsplit("/", 1)[1]).read_bytes() == b"panel"


@pytest.mark.parametrize("name", ["..", "../x", "a/b", "", ".hidden", "a b"])
def test_unsafe_names_rejected(cache, name):
    with pytest.raises(ValueError):
        cache.path(name, "file.png")
    with pytest.raises(ValueError):
        cache.path("K1", name)
