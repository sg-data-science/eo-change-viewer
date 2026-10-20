"""Chip rendering against a small synthetic ARD-like GeoTIFF."""

import io

import numpy as np
import pytest
import rasterio as rio
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box

from change_viewer import render

ORIGIN_X, ORIGIN_Y = 300000, 700500
SIZE = 60  # pixels each way, 10 m each


@pytest.fixture
def ard_tif(tmp_path):
    """10 bands like the CEDA files: blue, green, red first; nodata 0.

    Blue is 30, green 75 and red 300 (above the clip range). The top-left
    10x10 pixels are nodata in every band.
    """
    path = tmp_path / "S2X_test.tif"
    data = np.full((10, SIZE, SIZE), 500, dtype=np.uint16)
    data[0], data[1], data[2] = 30, 75, 300
    data[:, :10, :10] = 0
    with rio.open(
        path,
        "w",
        driver="GTiff",
        width=SIZE,
        height=SIZE,
        count=10,
        dtype="uint16",
        crs="EPSG:27700",
        transform=from_origin(ORIGIN_X, ORIGIN_Y, 10, 10),
        nodata=0,
    ) as dst:
        dst.write(data)
    return str(path)


def test_rgb_bands_come_from_ceda_sentinel():
    # ImagePlotter reads (1, 2, 3) = blue, green, red and shows them reversed.
    assert render.RGB_BANDS == (3, 2, 1)


def test_chip_bounds_square_snapped_and_buffered():
    geom = box(300123, 700045, 300187, 700161)
    minx, miny, maxx, maxy = render.chip_bounds(geom, buffer_m=50)
    assert maxx - minx == maxy - miny
    assert all(v % 10 == 0 for v in (minx, miny, maxx, maxy))
    assert minx <= 300123 - 50 and maxx >= 300187 + 50
    assert miny <= 700045 - 50 and maxy >= 700161 + 50


def test_read_rgb_band_order(ard_tif):
    data, transform, crs, nodata = render.read_rgb(
        ard_tif, (300200, 700100, 300400, 700300)
    )
    assert data.shape == (3, 20, 20)
    assert [int(b[0, 0]) for b in data] == [300, 75, 30]  # red, green, blue
    assert (transform.c, transform.f) == (300200, 700300)
    assert crs.to_epsg() == 27700 and nodata == 0


def test_read_rgb_outside_image_is_nodata(ard_tif):
    # Half of these bounds are west of the image.
    data, *_ = render.read_rgb(ard_tif, (299900, 700000, 300100, 700200))
    assert data.shape == (3, 20, 20)
    assert (data[:, :, :10] == 0).all()
    assert (data[:, :, 10:] > 0).all()


def test_to_rgba_clips_scales_and_masks():
    data = np.array([[[0, 300]], [[0, 75]], [[0, 150]]], dtype=np.uint16)
    rgba = render.to_rgba(data, nodata=0, clip=(0, 150))
    nodata_px, px = rgba[0, 0], rgba[0, 1]
    assert nodata_px[3] == 0
    assert list(px) == [255, 128, 255, 255]


def test_panel_png_draws_outline(ard_tif):
    geom = box(300200, 700100, 300300, 700200)
    data, transform, _, nodata = render.read_rgb(ard_tif, render.chip_bounds(geom))
    png = render.panel_png(render.to_rgba(data, nodata), transform, geom, size_px=200)
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    rows, cols = data.shape[1:]
    scale = round(200 / max(rows, cols))
    assert image.size == (cols * scale, rows * scale)
    assert render.OUTLINE_COLOUR in {c for _, c in image.getcolors(1 << 20)}


def test_warp_bounds_match_chip(ard_tif):
    bounds = (300100, 700000, 300500, 700400)
    data, transform, crs, nodata = render.read_rgb(ard_tif, bounds)
    warped, leaflet_bounds = render.warp_to_web_mercator(
        render.to_rgba(data, nodata), transform, crs
    )
    to_wgs84 = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
    lon_w, lat_s = to_wgs84.transform(bounds[0], bounds[1])
    (south, west), (north, east) = leaflet_bounds
    assert south < north and west < east
    # The warped image's box contains the chip's corners, within a pixel.
    assert south <= lat_s + 1e-4 and west <= lon_w + 1e-4
    assert warped[warped.shape[0] // 2, warped.shape[1] // 2, 3] == 255


def test_render_chip(ard_tif):
    chip = render.render_chip(ard_tif, box(300250, 700150, 300300, 700200))
    assert chip.panel_png.startswith(b"\x89PNG")
    assert chip.overlay_png.startswith(b"\x89PNG")
    assert len(chip.overlay_bounds) == 2


def test_export_bands_keeps_all_bands_and_values(ard_tif, tmp_path):
    geom = box(300250, 700150, 300300, 700200)
    out = tmp_path / "chip.tif"
    render.export_bands(ard_tif, geom, out)
    with rio.open(out) as dst:
        assert dst.count == 10 and dst.dtypes[0] == "uint16"
        assert dst.nodata == 0 and dst.crs.to_epsg() == 27700
        assert dst.bounds == pytest.approx(render.chip_bounds(geom))
        data = dst.read()
    # Inside the image the source values come through unscaled.
    middle = data[:, data.shape[1] // 2, data.shape[2] // 2]
    assert list(middle[:3]) == [30, 75, 300] and middle[3] == 500
    # The chip reaches past the image, which is nodata.
    assert (data == 0).any()
