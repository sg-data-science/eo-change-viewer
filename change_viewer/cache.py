"""On-disk cache of search results and image chips, under `cache/<keyval>/`.

Searches read many remote images, so repeat views shouldn't hit CEDA again.
A search whose window hasn't ended yet isn't cached, as CEDA may still add
images for it. Files are written atomically because window searches run in
parallel.
"""

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from change_viewer import config, imagery, render

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
CHIP_ROUTE = "/chips"


def safe_name(name):
    """`name` if it's safe as a single file or folder name; else ValueError."""
    if not _SAFE_NAME.match(name or ""):
        raise ValueError(f"unsafe cache name: {name!r}")
    return name


class Cache:
    def __init__(self, root=config.CACHE_DIR):
        self.root = Path(root)

    def site_dir(self, keyval):
        return self.root / safe_name(keyval)

    def path(self, keyval, filename):
        """Path of a cached file, after checking both names are safe."""
        return self.site_dir(keyval) / safe_name(filename)

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    # --- searches -----------------------------------------------------------

    @staticmethod
    def search_name(window, settings_key):
        return f"search_{window.key}_{settings_key}.json"

    def load_search(self, keyval, window, settings_key):
        path = self.path(keyval, self.search_name(window, settings_key))
        if not path.exists():
            return None
        return imagery.SearchResult.from_dict(json.loads(path.read_text()))

    def save_search(self, keyval, result, settings_key):
        path = self.path(keyval, self.search_name(result.window, settings_key))
        self._write(path, json.dumps(result.to_dict(), indent=1).encode())

    # --- chips --------------------------------------------------------------

    @staticmethod
    def chip_names(image_id):
        stem = f"{safe_name(image_id)}_b{config.CHIP_BUFFER_M}"
        return f"{stem}_panel.png", f"{stem}_overlay.png", f"{stem}_overlay.json"

    def has_chip(self, keyval, image_id):
        return all(self.path(keyval, n).exists() for n in self.chip_names(image_id))

    def save_chip(self, keyval, image_id, chip):
        panel, overlay, bounds = self.chip_names(image_id)
        self._write(self.path(keyval, panel), chip.panel_png)
        self._write(self.path(keyval, overlay), chip.overlay_png)
        self._write(self.path(keyval, bounds), json.dumps(chip.overlay_bounds).encode())

    def chip_urls(self, keyval, image_id):
        """Browser URLs of the panel and overlay PNGs, and the overlay bounds."""
        panel, overlay, bounds = self.chip_names(image_id)
        return (
            f"{CHIP_ROUTE}/{keyval}/{panel}",
            f"{CHIP_ROUTE}/{keyval}/{overlay}",
            json.loads(self.path(keyval, bounds).read_text()),
        )


def cached_search(cache, search, aoi, window, s2cloudless_max, today=None):
    """Search one window for one site, using the cache when possible."""
    keyval = aoi["keyval"].iloc[0]
    settings_key = search.settings_key(s2cloudless_max)
    cached = cache.load_search(keyval, window, settings_key)
    if cached is not None:
        return cached
    result = search.search(aoi, window, s2cloudless_max)
    if window.end < (today or date.today()):
        cache.save_search(keyval, result, settings_key)
    return result


def cached_chip(cache, keyval, candidate, geom):
    """Render a candidate's chips if they aren't cached; return their URLs."""
    if not cache.has_chip(keyval, candidate.image_id):
        chip = render.render_chip(candidate.image_link, geom)
        cache.save_chip(keyval, candidate.image_id, chip)
    return cache.chip_urls(keyval, candidate.image_id)
