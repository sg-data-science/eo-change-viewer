/*
 * Coordinates on the main map: a readout of the latitude and longitude under
 * the cursor, and a right-click popup to copy them. Left-click is left alone,
 * as it selects planning polygons.
 *
 * Like sync_maps.js, this uses an init hook to find the map by the id Dash
 * gives its container.
 */
(function () {
    "use strict";

    const MAIN_MAP_ID = "map";
    // Five decimal places is about 1 m, finer than a 10 m pixel.
    const DECIMALS = 5;
    const PLACEHOLDER = "Lat, long";

    function format(latlng) {
        const wrapped = latlng.wrap();
        return `${wrapped.lat.toFixed(DECIMALS)}, ${wrapped.lng.toFixed(DECIMALS)}`;
    }

    // Resolves to true if the text was copied. The clipboard API only works
    // on localhost or HTTPS, not e.g. http://<machine-name>:8050.
    function copyText(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text).then(
                () => true,
                () => false
            );
        }
        return Promise.resolve(false);
    }

    function popupContent(text) {
        const L = window.L;
        const box = L.DomUtil.create("div", "coords-popup");
        const input = L.DomUtil.create("input", "form-control form-control-sm", box);
        input.readOnly = true;
        input.value = text;
        input.setAttribute("aria-label", "Latitude, longitude");
        const button = L.DomUtil.create("button", "btn btn-sm btn-primary", box);
        button.type = "button";
        button.textContent = "Copy";
        const note = L.DomUtil.create("div", "coords-note", box);
        note.textContent = "Latitude, longitude (WGS84)";
        L.DomEvent.on(button, "click", () => {
            copyText(text).then((copied) => {
                if (copied) {
                    button.textContent = "Copied";
                } else {
                    // Leave the text selected so Ctrl+C copies it.
                    input.focus();
                    input.select();
                    note.textContent = "Press Ctrl+C to copy";
                }
            });
        });
        return box;
    }

    function addReadout(map) {
        const L = window.L;
        const Readout = L.Control.extend({
            options: { position: "bottomleft" },
            onAdd: function () {
                const div = L.DomUtil.create("div", "coords-readout");
                div.textContent = PLACEHOLDER;
                L.DomEvent.disableClickPropagation(div);
                return div;
            },
        });
        const readout = new Readout();
        map.addControl(readout);
        const div = readout.getContainer();
        map.on("mousemove", (e) => {
            div.textContent = format(e.latlng);
        });
        map.on("mouseout", () => {
            div.textContent = PLACEHOLDER;
        });
    }

    function addCopyPopup(map) {
        // Leaflet stops the browser's own menu when the map has a
        // contextmenu listener. On touch screens a long press fires it too.
        map.on("contextmenu", (e) => {
            window.L.popup({ className: "coords-leaflet-popup" })
                .setLatLng(e.latlng)
                .setContent(popupContent(format(e.latlng)))
                .openOn(map);
        });
    }

    if (!window.L || !window.L.Map) {
        console.warn("coords.js: Leaflet not found; no coordinate tools.");
        return;
    }

    window.L.Map.addInitHook(function () {
        if (this.getContainer().id !== MAIN_MAP_ID) {
            return;
        }
        addReadout(this);
        addCopyPopup(this);
    });
})();
