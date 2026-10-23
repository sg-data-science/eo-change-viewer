"""Smoke tests against the real GeoServer and CEDA. Run with `pytest -m integration`."""

import os

import pytest

from change_viewer import config

pytestmark = pytest.mark.integration

# A major housing site in Crieff, used for the ceda_sentinel smoke test.
CRIEFF_YEAR = 2022
CRIEFF_KEYVAL = "R4H1W6MKFSS00"


@pytest.fixture(scope="module")
def client():
    from change_viewer.planning import PlanningClient

    try:
        config.wfs_url()
    except RuntimeError:
        pytest.skip(f"{config.WFS_URL_ENV} not set")
    return PlanningClient.from_env()


def test_wfs_lookup_by_keyval(client):
    gdf = client.by_keyval(CRIEFF_YEAR, CRIEFF_KEYVAL)
    assert gdf is not None and len(gdf) == 1
    assert gdf.crs.to_epsg() == 27700


def test_wfs_site_list(client):
    sites = client.site_list(CRIEFF_YEAR, "housing")
    assert CRIEFF_KEYVAL in {s["keyval"] for s in sites}
    order = [(s["local_auth"], s["reference"]) for s in sites]
    assert order == sorted(order)


def test_wfs_search(client):
    gdf = client.search(CRIEFF_YEAR, "crieff", limit=5)
    assert not gdf.empty


def test_find_s2_stac_returns_images(client):
    from ceda_s2 import FindS2STAC

    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
    aoi = client.by_keyval(CRIEFF_YEAR, CRIEFF_KEYVAL)[["keyval", "geometry"]]
    finder = FindS2STAC(aoi, "2025-04-01", "2025-06-30", id_col="keyval")
    links = finder.find_image_links()
    assert len(links) > 0
    assert {"image_link", "image_date", "s2cloudles"} <= set(links.columns)


S2_ARD = "https://dap.ceda.ac.uk/neodc/sentinel_ard/data/sentinel_2/"
SUFFIX = "_utm30n_osgb_vmsk_sharp_rad_srefdem_stdsref.tif"
# Chip area of an Orkney site (QVVK33MDKAU00) whose June 2020 and 2022 images
# are fog the cloud mask scores as 0% cloud; its June 2023 image is clear.
ORKNEY_CHIP = (353110, 1002060, 355280, 1004230)


@pytest.mark.parametrize(
    ("name", "bright"),
    [
        ("2020/06/14/S2A_20200614_lat59lon204_T30VWL_ORB123", True),
        ("2022/06/09/S2B_20220609_lat59lon204_T30VWL_ORB123", True),
        (
            "2023/06/14/S2B_20230614_latn590lonw0020_T30VWL_ORB123_20230614121545",
            False,
        ),
    ],
)
def test_bright_check_catches_fog_the_mask_misses(name, bright):
    from change_viewer import imagery

    percent = imagery.bright_percent(S2_ARD + name + SUFFIX, ORKNEY_CHIP)
    assert (percent > config.BRIGHT_MAX_PERCENT) is bright


def test_basemap_capture_date():
    from change_viewer.basemap import image_info

    info = image_info(-3.84, 56.373)
    assert info is not None and info.date.year >= 2014


def test_image_search_and_render(client):
    from change_viewer import imagery, render

    aoi = client.by_keyval(CRIEFF_YEAR, CRIEFF_KEYVAL)
    result = imagery.ImageSearch().search(aoi, imagery.Window(2023, 6))
    assert result.source == "stac" and result.best is not None
    chip = render.render_chip(result.best.image_link, aoi.geometry.iloc[0])
    assert chip.panel_png.startswith(b"\x89PNG")
