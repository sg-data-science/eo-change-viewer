/*
 * Keep the Sentinel-2 panel maps on the same view, so panning or zooming one
 * moves them all, live. With the "Link to main map" switch on, the main map
 * joins in too.
 *
 * Dash serves files in assets/ automatically. dash-leaflet exposes Leaflet as
 * window.L, so an init hook can collect each map as it's created; maps are
 * found by the id Dash gives their container.
 */
(function () {
    "use strict";

    const MAIN_MAP_ID = "map";
    const PANEL_ID_PART = "window-panel";
    const LINK_SWITCH_ID = "link-main-map";
    const maps = new Map();
    let syncing = false;
    // Exposed for debugging in the browser console and for browser tests.
    window.changeViewerMaps = maps;

    function mainMapLinked() {
        const input = document.getElementById(LINK_SWITCH_ID);
        return Boolean(input && input.checked);
    }

    function linkedMaps() {
        const linked = [];
        for (const [id, map] of maps) {
            if (id.includes(PANEL_ID_PART) || (id === MAIN_MAP_ID && mainMapLinked())) {
                linked.push(map);
            }
        }
        return linked;
    }

    function follow(leader) {
        const others = linkedMaps().filter((m) => m !== leader);
        if (syncing || !others.length || !linkedMaps().includes(leader)) {
            return;
        }
        // Moving the others fires their own move events; the flag stops
        // them moving the leader back.
        syncing = true;
        try {
            const center = leader.getCenter();
            const zoom = leader.getZoom();
            for (const map of others) {
                map.setView(center, zoom, { animate: false });
            }
        } finally {
            syncing = false;
        }
    }

    function firstPanel() {
        for (const [id, map] of maps) {
            if (id.includes(PANEL_ID_PART)) {
                return map;
            }
        }
        return null;
    }

    if (!window.L || !window.L.Map) {
        console.warn("sync_maps.js: Leaflet not found; maps won't be linked.");
        return;
    }

    window.L.Map.addInitHook(function () {
        const map = this;
        const id = map.getContainer().id;
        if (!id) {
            return;
        }
        maps.set(id, map);
        // "move" fires throughout a drag and a zoom, so the others follow live.
        // "moveend" catches the final position, e.g. after a drag's glide.
        map.on("move moveend", () => follow(map));
        map.on("unload", () => maps.delete(id));
    });

    // Switching the link on brings the main map to the panels' view.
    document.addEventListener("change", (event) => {
        if (event.target && event.target.id === LINK_SWITCH_ID && event.target.checked) {
            const panel = firstPanel();
            if (panel) {
                follow(panel);
            }
        }
    });
})();
