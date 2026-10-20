"""Find a clear Sentinel-2 image of one site for each month window.

Wraps `ceda_sentinel`. STAC is tried first; the HTML-scraping finder is used
only when STAC has no items at all for the window, which happens for older
dates it doesn't index. If STAC has items but none is clear, the scraper
would check the same images more slowly, so it isn't tried.
"""

import calendar
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import date

import requests
from ceda_s2 import FindS2, FindS2STAC, ImageReadError
from ceda_stac.client import S2_ARD_COLLECTION, format_datetime_range, get_stac_client

from change_viewer import config

logger = logging.getLogger(__name__)

for _key, _value in config.GDAL_ENV.items():
    os.environ.setdefault(_key, _value)


class CedaUnavailable(Exception):
    """CEDA couldn't be read, so "no images" can't be concluded."""


@dataclass(frozen=True, order=True)
class Window:
    """One calendar month."""

    year: int
    month: int

    def __post_init__(self):
        if not 1 <= self.month <= 12:
            raise ValueError(f"month must be 1-12, not {self.month}")

    @classmethod
    def parse(cls, text):
        """From `YYYY-MM`, as an HTML month input gives it."""
        year, month = text.split("-")
        return cls(int(year), int(month))

    @property
    def key(self):
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def label(self):
        return f"{calendar.month_abbr[self.month]} {self.year}"

    @property
    def start(self):
        return date(self.year, self.month, 1)

    @property
    def end(self):
        return date(
            self.year, self.month, calendar.monthrange(self.year, self.month)[1]
        )


def _parse_date(value):
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def default_windows(date_recei, date_decis, today=None):
    """Before, after and later windows for an application.

    All use the same month, so seasonal change isn't mistaken for
    development: the year before the application was received, and one and
    two years after the decision (or after receipt, if there's no decision).
    A window starting after `today` is None.
    """
    today = today or date.today()
    received = _parse_date(date_recei)
    decided = _parse_date(date_decis) or received
    if received is None:
        return [None] * config.N_WINDOWS
    month = config.DEFAULT_WINDOW_MONTH
    windows = [
        Window(received.year - 1, month),
        Window(decided.year + 1, month),
        Window(decided.year + 2, month),
    ]
    return [w if w.start <= today else None for w in windows]


@dataclass(frozen=True)
class Candidate:
    """An image that passed the nodata and cloud checks."""

    image_link: str
    image_date: str
    cloud: float

    @property
    def image_id(self):
        """The file name without its long processing suffix."""
        name = self.image_link.rsplit("/", 1)[-1].removesuffix(".tif")
        return name.split("_utm")[0].split("_osgb")[0]

    @property
    def tile(self):
        """The MGRS tile code, e.g. "T30VVH", or None if the name lacks one."""
        match = re.search(r"_(T\d{2}[A-Z]{3})_", self.image_link.rsplit("/", 1)[-1])
        return match.group(1) if match else None


def choose(candidates):
    """The image to show: least cloud, then earliest date, then image link.

    The link breaks ties between same-day images (Sentinel-2A and 2B on
    different orbits), so the choice is stable.
    """
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c.cloud, c.image_date, c.image_link))


@dataclass
class SearchResult:
    window: Window
    candidates: list = field(default_factory=list)
    source: str = "stac"
    # Checks that failed with a read error, when the others still succeeded.
    read_errors: int = 0

    @property
    def best(self):
        return choose(self.candidates)

    def to_dict(self):
        return {
            "window": self.window.key,
            "candidates": [asdict(c) for c in self.candidates],
            "source": self.source,
            "read_errors": self.read_errors,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            window=Window.parse(data["window"]),
            candidates=[Candidate(**c) for c in data["candidates"]],
            source=data["source"],
            read_errors=data.get("read_errors", 0),
        )


def _candidates(gdf):
    """Candidates from a finder's output, which is None when nothing passed."""
    if gdf is None:
        return []
    rows = gdf[gdf["image_link"].notna()]
    return sorted(
        (
            Candidate(r.image_link, str(r.image_date), float(r.s2cloudles))
            for r in rows.itertuples()
        ),
        key=lambda c: (c.image_date, c.image_link),
    )


class ImageSearch:
    """Runs window searches for one site at a time, reusing one STAC client.

    `stac_finder`, `html_finder` and `client_factory` default to the
    `ceda_sentinel` ones and can be swapped in tests.
    """

    def __init__(
        self,
        s2cloudless_max=config.S2CLOUDLESS_MAX,
        check_buffer=config.CHECK_BUFFER_M,
        tile_cloud_max=config.TILE_CLOUD_MAX,
        stac_finder=FindS2STAC,
        html_finder=FindS2,
        client_factory=get_stac_client,
        connection_retries=1,
    ):
        self.s2cloudless_max = s2cloudless_max
        self.check_buffer = check_buffer
        self.tile_cloud_max = tile_cloud_max
        self.stac_finder = stac_finder
        self.html_finder = html_finder
        self.client_factory = client_factory
        self.connection_retries = connection_retries
        self._client = None
        self._client_lock = threading.Lock()

    def settings_key(self, s2cloudless_max=None):
        """Identifies the search settings, for caching results."""
        cloud = self.s2cloudless_max if s2cloudless_max is None else s2cloudless_max
        return f"c{cloud:g}_b{self.check_buffer:g}_t{self.tile_cloud_max:g}"

    @property
    def client(self):
        with self._client_lock:
            if self._client is None:
                try:
                    self._client = self.client_factory()
                except Exception as e:
                    raise CedaUnavailable(
                        "Couldn't connect to the CEDA STAC API"
                    ) from e
            return self._client

    def _stac_has_items(self, aoi, window):
        bbox = list(aoi.to_crs(epsg=4326).total_bounds)
        try:
            search = self.client.search(
                collections=[S2_ARD_COLLECTION],
                bbox=bbox,
                datetime=format_datetime_range(
                    window.start.isoformat(), window.end.isoformat()
                ),
                max_items=1,
            )
            return any(True for _ in search.items())
        except CedaUnavailable:
            raise
        except Exception as e:
            raise CedaUnavailable("The CEDA STAC search failed") from e

    def search(self, aoi, window, s2cloudless_max=None):
        """Every image of `aoi` in `window` that passes the checks.

        `aoi` is a one-row GeoDataFrame with a `keyval` column.

        Raises:
            CedaUnavailable: CEDA couldn't be searched or every image read
                failed, so the absence of images means nothing.
        """
        kwargs = {
            "id_col": "keyval",
            "s2cloudless_max": (
                self.s2cloudless_max if s2cloudless_max is None else s2cloudless_max
            ),
            "tile_cloud_max": self.tile_cloud_max,
            "check_buffer": self.check_buffer,
        }
        aoi = aoi[["keyval", "geometry"]]
        start, end = window.start.isoformat(), window.end.isoformat()
        if self._stac_has_items(aoi, window):
            source = "stac"

            def make_finder():
                return self.stac_finder(aoi, start, end, client=self.client, **kwargs)

        else:
            source = "html"

            def make_finder():
                return self.html_finder(aoi, start, end, **kwargs)

        for attempt in range(1 + self.connection_retries):
            finder = make_finder()
            try:
                gdf = finder.find_image_links()
                break
            except ImageReadError as e:
                raise CedaUnavailable("Every CEDA image read failed") from e
            except requests.ConnectionError as e:
                # One failed request ends a whole search, and name lookups
                # here sometimes fail briefly, so try again before giving up.
                if attempt < self.connection_retries:
                    logger.warning("Retrying %s search after: %s", window.key, e)
                    continue
                raise CedaUnavailable("Couldn't connect to CEDA") from e
            except Exception as e:
                # Other network and API errors. Logged in full so a bug here
                # isn't mistaken for an outage.
                logger.exception("Sentinel-2 search failed for %s", window.key)
                raise CedaUnavailable("The CEDA search failed") from e
        return SearchResult(
            window=window,
            candidates=_candidates(gdf),
            source=source,
            read_errors=len(finder.read_errors),
        )
