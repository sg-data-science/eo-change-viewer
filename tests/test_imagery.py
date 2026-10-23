"""Window maths, default windows, the selection rule and the search flow."""

from datetime import date

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio as rio
import requests
from ceda_s2 import ImageReadError
from rasterio.transform import from_origin
from shapely.geometry import box

from change_viewer import imagery, render
from change_viewer.imagery import (
    Candidate,
    CedaUnavailable,
    ImageSearch,
    SearchResult,
    Window,
    choose,
    default_windows,
)

LINK = "https://dap.ceda.ac.uk/neodc/sentinel_ard/data/sentinel_2/{}"
NEW_NAME = (
    "2023/06/04/S2B_20230604_latn563lonw0037_T30VVH_ORB123_20230604121616"
    "_utm30n_osgb_vmsk_sharp_rad_srefdem_stdsref.tif"
)
OLD_NAME = (
    "2021/06/29/S2A_20210629_lat56lon373_T30VVH_ORB123"
    "_utm30n_osgb_vmsk_sharp_rad_srefdem_stdsref.tif"
)


# --- windows ----------------------------------------------------------------


def test_window_dates():
    w = Window.parse("2024-02")
    assert (w.start, w.end) == (date(2024, 2, 1), date(2024, 2, 29))
    assert Window(2023, 2).end == date(2023, 2, 28)
    assert Window(2021, 12).end == date(2021, 12, 31)
    assert (w.key, w.label) == ("2024-02", "Feb 2024")


def test_window_rejects_bad_month():
    with pytest.raises(ValueError):
        Window(2021, 13)


def test_default_windows():
    windows = default_windows("2021-12-21", "2022-04-13", today=date(2026, 10, 20))
    assert windows == [Window(2020, 6), Window(2023, 6), Window(2024, 6)]


def test_default_windows_use_received_without_decision():
    windows = default_windows("2021-12-21", None, today=date(2026, 10, 20))
    assert windows == [Window(2020, 6), Window(2022, 6), Window(2023, 6)]


def test_default_windows_leave_future_blank():
    windows = default_windows("2025-03-01", "2025-08-01", today=date(2026, 10, 20))
    assert windows == [Window(2024, 6), Window(2026, 6), None]


def test_default_windows_without_dates():
    assert default_windows(None, None) == [None, None, None]


# --- selection rule ---------------------------------------------------------


def test_choose_least_cloud():
    a = Candidate("a", "2021-06-01", 4.0)
    b = Candidate("b", "2021-06-20", 1.5)
    assert choose([a, b]) == b


def test_choose_keeps_zero_cloud():
    zero = Candidate("a", "2021-06-20", 0.0)
    assert choose([Candidate("b", "2021-06-01", 0.5), zero]) == zero


def test_choose_tie_goes_to_earliest():
    early = Candidate("z", "2021-06-01", 2.0)
    assert choose([Candidate("a", "2021-06-09", 2.0), early]) == early


def test_choose_same_day_duplicates_break_on_link():
    s2a = Candidate(LINK.format("S2A.tif"), "2021-06-09", 0.0)
    s2b = Candidate(LINK.format("S2B.tif"), "2021-06-09", 0.0)
    assert choose([s2b, s2a]) == s2a
    assert choose([s2a, s2b]) == s2a


def test_choose_empty():
    assert choose([]) is None


def test_image_id_handles_both_naming_styles():
    new = Candidate(LINK.format(NEW_NAME), "2023-06-04", 0.0)
    old = Candidate(LINK.format(OLD_NAME), "2021-06-29", 0.0)
    assert new.image_id == "S2B_20230604_latn563lonw0037_T30VVH_ORB123_20230604121616"
    assert old.image_id == "S2A_20210629_lat56lon373_T30VVH_ORB123"


def test_search_result_round_trip():
    result = SearchResult(
        Window(2023, 6), [Candidate("a", "2023-06-04", 0.0)], "html", 2
    )
    assert SearchResult.from_dict(result.to_dict()) == result


# --- search flow with fake finders ------------------------------------------


@pytest.fixture
def aoi():
    return gpd.GeoDataFrame(
        {"keyval": ["K1"], "reference": ["21/1"]},
        geometry=[box(285000, 721000, 285300, 721300)],
        crs="EPSG:27700",
    )


def finder_output(aoi, rows):
    """A finder's GeoDataFrame result, or None when nothing passed."""
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["image_link", "image_date", "s2cloudles"])
    df["keyval"] = "K1"
    return aoi[["keyval", "geometry"]].merge(df, on="keyval")


class FakeFinder:
    """Stands in for FindS2STAC / FindS2. Behaviours are consumed in order."""

    calls = []
    behaviours = []

    def __init__(self, aoi, start, end, **kwargs):
        self.aoi, self.start, self.end, self.kwargs = aoi, start, end, kwargs
        self.read_errors = []
        type(self).calls.append(self)

    def find_image_links(self):
        behaviour = type(self).behaviours.pop(0)
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour


class FakeStac(FakeFinder):
    calls, behaviours = [], []


class FakeHtml(FakeFinder):
    calls, behaviours = [], []


class FakeItems:
    def __init__(self, n):
        self.n = n

    def items(self):
        return iter(range(self.n))


class FakeClient:
    def __init__(self, n_items):
        self.n_items = n_items
        self.searches = []

    def search(self, **kwargs):
        self.searches.append(kwargs)
        return FakeItems(self.n_items)


def make_search(n_items, stac=(), html=(), bright=None):
    """A search with fake finders. `bright` maps image links to the bright
    percentage, or to an exception to raise; links not in it are 0."""
    FakeStac.calls, FakeStac.behaviours = [], list(stac)
    FakeHtml.calls, FakeHtml.behaviours = [], list(html)
    client = FakeClient(n_items)
    bright = bright or {}

    def bright_reader(link, bounds, blue_min):
        bright_reader.calls.append((link, bounds, blue_min))
        value = bright.get(link, 0.0)
        if isinstance(value, Exception):
            raise value
        return value

    bright_reader.calls = []
    search = ImageSearch(
        stac_finder=FakeStac,
        html_finder=FakeHtml,
        client_factory=lambda: client,
        bright_reader=bright_reader,
    )
    search.bright_calls = bright_reader.calls
    return search, client


def test_search_uses_stac_when_it_has_items(aoi):
    rows = [(LINK.format("b.tif"), "2023-06-16", 0.0), ("a", "2023-06-04", 0.0)]
    search, client = make_search(2, stac=[finder_output(aoi, rows)])
    result = search.search(aoi, Window(2023, 6), s2cloudless_max=5)
    assert result.source == "stac"
    assert [c.image_date for c in result.candidates] == ["2023-06-04", "2023-06-16"]
    (finder,) = FakeStac.calls
    assert (finder.start, finder.end) == ("2023-06-01", "2023-06-30")
    assert finder.kwargs["client"] is client
    assert finder.kwargs["s2cloudless_max"] == 5
    assert finder.kwargs["tile_cloud_max"] == imagery.config.TILE_CLOUD_MAX
    assert finder.kwargs["check_buffer"] == imagery.config.CHECK_BUFFER_M
    assert list(finder.aoi.columns) == ["keyval", "geometry"]
    assert FakeHtml.calls == []


def test_search_falls_back_to_html_without_stac_items(aoi):
    search, _ = make_search(0, html=[finder_output(aoi, [("a", "2018-06-02", 1)])])
    result = search.search(aoi, Window(2018, 6))
    assert result.source == "html"
    assert len(result.candidates) == 1
    assert "client" not in FakeHtml.calls[0].kwargs


def test_search_with_no_clear_image_does_not_fall_back(aoi):
    search, _ = make_search(3, stac=[None])
    result = search.search(aoi, Window(2021, 6))
    assert result.candidates == [] and result.best is None
    assert FakeHtml.calls == []


def test_read_errors_mean_unavailable_not_no_images(aoi):
    error = ImageReadError([{"error": "HTTP 503", "image_link": "x"}])
    search, _ = make_search(3, stac=[error])
    with pytest.raises(CedaUnavailable):
        search.search(aoi, Window(2021, 6))
    assert FakeHtml.calls == []


def test_connection_error_is_retried_once(aoi):
    ok = finder_output(aoi, [("a", "2020-06-01", 0.0)])
    search, _ = make_search(0, html=[requests.ConnectionError("DNS"), ok])
    assert len(search.search(aoi, Window(2020, 6)).candidates) == 1
    assert len(FakeHtml.calls) == 2


def test_repeated_connection_errors_mean_unavailable(aoi):
    errors = [requests.ConnectionError("DNS"), requests.ConnectionError("DNS")]
    search, _ = make_search(0, html=errors)
    with pytest.raises(CedaUnavailable):
        search.search(aoi, Window(2020, 6))


def test_stac_client_failure_means_unavailable(aoi):
    def broken():
        raise OSError("no route")

    search = ImageSearch(client_factory=broken)
    with pytest.raises(CedaUnavailable):
        search.search(aoi, Window(2023, 6))


def test_settings_key():
    search = ImageSearch()
    assert search.settings_key() == search.settings_key(imagery.config.S2CLOUDLESS_MAX)
    assert search.settings_key(5) != search.settings_key(10)
    assert ImageSearch(bright_max_percent=50).settings_key() != search.settings_key()
    assert ImageSearch(bright_blue_min=400).settings_key() != search.settings_key()


# --- brightness check -------------------------------------------------------


def test_bright_images_are_dropped_after_the_mask(aoi):
    rows = [("fog", "2020-06-14", 0.0), ("clear", "2020-06-29", 3.0)]
    search, _ = make_search(2, stac=[finder_output(aoi, rows)], bright={"fog": 100})
    result = search.search(aoi, Window(2020, 6))
    assert [c.image_link for c in result.candidates] == ["clear"]
    assert result.read_errors == 0
    # Measured over the chip area, not the site plus the mask's buffer.
    bounds = render.chip_bounds(aoi.geometry.iloc[0])
    assert search.bright_calls == [
        ("fog", bounds, imagery.config.BRIGHT_BLUE_MIN),
        ("clear", bounds, imagery.config.BRIGHT_BLUE_MIN),
    ]


def test_bright_check_threshold_is_inclusive(aoi):
    limit = imagery.config.BRIGHT_MAX_PERCENT
    rows = [("at", "2020-06-01", 0.0), ("over", "2020-06-02", 0.0)]
    bright = {"at": limit, "over": limit + 0.1}
    search, _ = make_search(2, stac=[finder_output(aoi, rows)], bright=bright)
    result = search.search(aoi, Window(2020, 6))
    assert [c.image_link for c in result.candidates] == ["at"]


def test_bright_check_skipped_when_mask_passes_nothing(aoi):
    search, _ = make_search(2, stac=[None])
    assert search.search(aoi, Window(2020, 6)).candidates == []
    assert search.bright_calls == []


def test_bright_read_error_drops_image_and_counts(aoi):
    rows = [("bad", "2020-06-01", 0.0), ("ok", "2020-06-02", 0.0)]
    bright = {"bad": OSError("HTTP 503")}
    search, _ = make_search(2, stac=[finder_output(aoi, rows)], bright=bright)
    result = search.search(aoi, Window(2020, 6))
    assert [c.image_link for c in result.candidates] == ["ok"]
    assert result.read_errors == 1


def test_every_bright_read_failing_means_unavailable(aoi):
    rows = [("a", "2020-06-01", 0.0)]
    bright = {"a": OSError("HTTP 503")}
    search, _ = make_search(2, stac=[finder_output(aoi, rows)], bright=bright)
    with pytest.raises(CedaUnavailable):
        search.search(aoi, Window(2020, 6))


@pytest.fixture
def blue_tif(tmp_path):
    """A 10x10 image at 10 m with blue 0 (nodata) in the first row, 500
    (bright) in the next two and 50 elsewhere."""
    blue = np.full((10, 10), 50, dtype=np.uint16)
    blue[0] = 0
    blue[1:3] = 500
    path = tmp_path / "image.tif"
    profile = {
        "driver": "GTiff",
        "width": 10,
        "height": 10,
        "count": 3,
        "dtype": "uint16",
        "crs": "EPSG:27700",
        "transform": from_origin(1000, 2000, 10, 10),
        "nodata": 0,
    }
    with rio.open(path, "w", **profile) as dst:
        dst.write(np.stack([blue, blue // 2, blue // 2]))
    return path


def test_bright_percent_ignores_nodata(blue_tif):
    # 90 valid pixels, 20 of them bright.
    percent = imagery.bright_percent(blue_tif, (1000, 1900, 1100, 2000), 300)
    assert percent == pytest.approx(20 / 90 * 100)


def test_bright_percent_outside_image_is_nodata(blue_tif):
    # The left half is off the image, so only the image's valid pixels count.
    percent = imagery.bright_percent(blue_tif, (900, 1900, 1100, 2000), 300)
    assert percent == pytest.approx(20 / 90 * 100)
    assert imagery.bright_percent(blue_tif, (0, 0, 100, 100), 300) == 0.0
