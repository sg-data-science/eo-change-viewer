"""Saving reviews to a GeoPackage and exporting chips."""

from datetime import datetime

import pytest
from rasterio.errors import RasterioIOError
from shapely.geometry import box

from change_viewer import config, reviews
from change_viewer.cache import Cache
from change_viewer.imagery import Candidate
from change_viewer.render import Chip
from change_viewer.reviews import ReviewStore, WindowChoice

LINK = "https://dap.ceda.ac.uk/x/S2B_20230604_T30VVH_utm30n_osgb_vmsk.tif"
OTHER = "https://dap.ceda.ac.uk/x/S2A_20230611_T30VVH_utm30n_osgb_vmsk.tif"
PROPS = {
    "keyval": "K1",
    "year": 2022,
    "reference": "22/00083/DPP",
    "local_auth": "Midlothian",
    "dev_desc_is": "housing",
    "is_placeholder": False,
    "address": "not copied",
}
GEOM = box(330600, 665200, 330650, 665260)


def choices(before=LINK):
    return [
        WindowChoice("before", "2021-06", Candidate(before, "2021-06-04", 1.5)),
        WindowChoice("after", "2023-06", None),
        WindowChoice("later", None, None),
    ]


def record(verdict="change", note=" new houses ", **kwargs):
    return reviews.review_record(
        PROPS,
        verdict,
        note,
        10,
        choices(**kwargs),
        {"before": "chips/K1/before.tif"},
        saved_at=datetime(2026, 10, 20, 14, 3),
    )


def test_review_record_is_flat():
    rec = record()
    assert list(rec) == reviews.COLUMNS
    assert rec["verdict"] == "change" and rec["note"] == "new houses"
    assert rec["before_image_id"] == "S2B_20230604_T30VVH"
    assert rec["before_cloud"] == 1.5 and rec["before_chip"] == "chips/K1/before.tif"
    assert rec["after_month"] == "2023-06" and rec["after_image_link"] is None
    assert rec["saved_at"] == "2026-10-20T14:03:00"
    assert "address" not in rec


def test_review_record_rejects_unknown_verdict():
    with pytest.raises(ValueError):
        record(verdict="maybe")


def test_store_round_trip_and_replace(tmp_path):
    store = ReviewStore(tmp_path / "out" / "reviews.gpkg")
    assert store.get("K1") is None and store.reviewed() == set()
    store.save(record(), GEOM)
    saved = store.get("K1")
    assert saved["verdict"] == "change" and saved["after_image_link"] is None
    assert saved["before_cloud"] == 1.5
    assert reviews.saved_windows(saved) == [
        ("2021-06", LINK),
        ("2023-06", None),
        (None, None),
    ]
    # Saving again replaces the row rather than adding one.
    store.save(record(verdict="unsure", before=OTHER), GEOM)
    gdf = store.all()
    assert len(gdf) == 1 and gdf.crs.to_epsg() == 27700
    assert store.get("K1")["verdict"] == "unsure"
    assert store.reviewed() == {"K1"}
    assert gdf.geometry.iloc[0].equals(GEOM)


def test_store_keeps_other_sites(tmp_path):
    store = ReviewStore(tmp_path / "reviews.gpkg")
    store.save(record(), GEOM)
    store.save({**record(), "keyval": "K2"}, GEOM)
    assert store.reviewed() == {"K1", "K2"}
    assert not list(tmp_path.glob(".tmp-*"))


@pytest.fixture
def fake_reads(monkeypatch):
    """Stand-ins for the remote reads, recording which images were read."""
    reads = []

    def fake_export(link, geom, path):
        reads.append(link)
        with open(path, "wb") as f:
            f.write(b"tif")

    def fake_render(link, geom):
        return Chip(b"panel", b"overlay", [[1, 2], [3, 4]])

    monkeypatch.setattr(reviews.render, "export_bands", fake_export)
    monkeypatch.setattr(reviews.render, "render_chip", fake_render)
    return reads


def test_export_site_writes_tif_and_panel(tmp_path, fake_reads):
    export_dir = tmp_path / "output" / "chips"
    chips, failed = reviews.export_site(
        Cache(tmp_path / "cache"), "K1", choices(), GEOM, export_dir
    )
    stem = f"before_S2B_20230604_T30VVH_b{config.CHIP_BUFFER_M}"
    assert chips == {"before": f"chips/K1/{stem}.tif"} and failed == []
    assert (export_dir / "K1" / f"{stem}.png").read_bytes() == b"panel"
    # Exporting again reuses the files rather than reading CEDA again.
    reviews.export_site(Cache(tmp_path / "cache"), "K1", choices(), GEOM, export_dir)
    assert fake_reads == [LINK]


def test_export_site_removes_earlier_choice(tmp_path, fake_reads):
    export_dir = tmp_path / "chips"
    cache = Cache(tmp_path / "cache")
    reviews.export_site(cache, "K1", choices(), GEOM, export_dir)
    reviews.export_site(cache, "K1", choices(before=OTHER), GEOM, export_dir)
    names = sorted(p.name for p in (export_dir / "K1").iterdir())
    assert len(names) == 2 and all("S2A_20230611" in n for n in names)


def test_export_site_reports_unreadable_image(tmp_path, monkeypatch):
    def failing_export(link, geom, path):
        raise RasterioIOError("CEDA down")

    monkeypatch.setattr(reviews.render, "export_bands", failing_export)
    chips, failed = reviews.export_site(
        Cache(tmp_path / "cache"), "K1", choices(), GEOM, tmp_path / "chips"
    )
    assert chips == {} and failed == ["before"]
    assert not list((tmp_path / "chips" / "K1").iterdir())


def test_export_site_rejects_unsafe_keyval(tmp_path):
    with pytest.raises(ValueError):
        reviews.export_site(Cache(tmp_path), "../x", choices(), GEOM, tmp_path)
