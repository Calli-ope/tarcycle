const API_URL = "";
// Grey = already fine; the three gap classes use a colour-blind-validated set.
const CLASSES = {
  cycle_infra: { label: "On cycle infrastructure", color: "#898781" },
  road_no_infra: { label: "Road without cycle infra", color: "#2a78d6" },
  osm_not_bikeable: { label: "OSM way closed to bikes", color: "#1baf7a" },
  no_osm_way: { label: "No OSM way at all", color: "#eb6834" },
};
const RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"];
const classColor = [
  "match",
  ["get", "cell_class"],
  ...Object.entries(CLASSES).flatMap(([name, { color }]) => [name, color]),
  "#898781",
];

const map = new maplibregl.Map({
  container: "map",
  center: [26.729, 58.3776],
  zoom: 13,
  style: {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "&copy; OpenStreetMap contributors",
      },
    },
    // Washed-out basemap so the data layers carry the colour.
    layers: [
      {
        id: "osm",
        type: "raster",
        source: "osm",
        paint: { "raster-saturation": -0.9, "raster-opacity": 0.6 },
      },
    ],
  },
});
const statusText = document.querySelector("#status");
const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });

async function getJson(layer) {
  const response = await fetch(`${API_URL}/api/coverage/${layer}`);
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || `Could not load ${layer}.`);
  return body;
}

function percent(value) {
  return `${(value * 100).toFixed(value < 0.1 ? 1 : 0)}%`;
}

function renderSummary(summary) {
  const bar = document.querySelector("#share-bar");
  const legend = document.querySelector("#class-legend");
  const shares = summary.share_of_ridden_distance;
  bar.setAttribute(
    "aria-label",
    Object.entries(CLASSES)
      .map(([name, { label }]) => `${label} ${percent(shares[name])}`)
      .join(", "),
  );
  for (const [name, { label, color }] of Object.entries(CLASSES)) {
    const segment = document.createElement("span");
    segment.style.flexGrow = shares[name];
    segment.style.background = color;
    segment.title = `${label}: ${percent(shares[name])}`;
    bar.append(segment);
    const item = document.createElement("li");
    item.innerHTML = `<span class="swatch" style="background:${color}"></span>
      <span>${label}</span>
      <span class="value">${percent(shares[name])} · ${Math.round(summary.ridden_km_approx[name]).toLocaleString()} km</span>`;
    legend.append(item);
  }
}

function boundsOf(geometry) {
  const coordinates = geometry.coordinates.flat(Infinity);
  const bounds = new maplibregl.LngLatBounds();
  for (let index = 0; index < coordinates.length; index += 2) {
    bounds.extend([coordinates[index], coordinates[index + 1]]);
  }
  return bounds;
}

function candidateText(properties) {
  return `#${properties.rank} · ${CLASSES[properties.cell_class].label}<br />
    ${properties.length_m} m long · median ${properties.median_trips} trips (max ${properties.max_trips})`;
}

function renderCandidates(candidates) {
  const list = document.querySelector("#candidate-list");
  const top = candidates.features.slice(0, 15);
  if (!top.length) list.innerHTML = "<li>No candidates above the privacy threshold.</li>";
  for (const feature of top) {
    const { properties } = feature;
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "candidate";
    button.innerHTML = `<span class="swatch" style="background:${CLASSES[properties.cell_class].color}"></span>
      <span>${CLASSES[properties.cell_class].label}</span>
      <span class="value">${properties.length_m} m · ${properties.median_trips} trips</span>`;
    button.addEventListener("click", () => {
      const bounds = boundsOf(feature.geometry);
      map.fitBounds(bounds, { padding: 120, maxZoom: 17 });
      popup.setLngLat(bounds.getCenter()).setHTML(candidateText(properties)).addTo(map);
    });
    item.append(button);
    list.append(item);
  }
}

function addLayers(cells, edges, candidates) {
  const maxTrips = Math.max(10, ...edges.features.map((feature) => feature.properties.trips));
  // Log-spaced, strictly increasing colour stops (MapLibre requires ascending stops).
  const stops = [];
  for (let index = 0; index < RAMP.length; index += 1) {
    const stop = Math.round(10 * (maxTrips / 10) ** (index / RAMP.length));
    stops.push(Math.max(stop, (stops[index - 1] ?? 9) + 1));
  }
  document.querySelector("#ramp-min").textContent = stops[0];
  document.querySelector("#ramp-max").textContent = maxTrips.toLocaleString();

  map.addSource("edges", { type: "geojson", data: edges });
  map.addLayer({
    id: "edges",
    type: "line",
    source: "edges",
    layout: { visibility: "none", "line-cap": "round" },
    paint: {
      "line-color": ["step", ["get", "trips"], RAMP[0], ...stops.slice(1).flatMap((stop, index) => [stop, RAMP[index + 1]])],
      "line-width": ["interpolate", ["linear"], ["log10", ["max", ["get", "trips"], 1]], 1, 1.5, Math.max(Math.log10(maxTrips), 1.1), 6],
    },
  });

  map.addSource("cells", { type: "geojson", data: cells });
  map.addLayer({
    id: "cells",
    type: "circle",
    source: "cells",
    paint: {
      "circle-color": classColor,
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 12, 1.2, 15, 3, 18, 8],
      "circle-opacity": ["interpolate", ["linear"], ["log10", ["get", "trips"]], 1, 0.45, 3, 0.95],
    },
  });

  map.addSource("candidates", { type: "geojson", data: candidates });
  map.addLayer({
    id: "candidates-fill",
    type: "fill",
    source: "candidates",
    paint: { "fill-color": classColor, "fill-opacity": 0.2 },
  });
  map.addLayer({
    id: "candidates-line",
    type: "line",
    source: "candidates",
    paint: { "line-color": classColor, "line-width": 2 },
  });
}

function setView(view) {
  map.setLayoutProperty("cells", "visibility", view === "cells" ? "visible" : "none");
  map.setLayoutProperty("edges", "visibility", view === "edges" ? "visible" : "none");
  document.querySelector("#edge-legend").hidden = view !== "edges";
  document.querySelector("#view-cells").classList.toggle("active", view === "cells");
  document.querySelector("#view-edges").classList.toggle("active", view === "edges");
}

function hoverText(feature) {
  const { properties } = feature;
  if (feature.layer.id === "edges") {
    const name = properties.name || properties.highway;
    return `${name}<br />${properties.trips} trips · ${properties.edge_class.replace("_", " ")}`;
  }
  if (feature.layer.id === "cells") {
    return `${CLASSES[properties.cell_class].label}<br />${properties.trips} trips`;
  }
  return candidateText(properties);
}

function addHover() {
  for (const layer of ["candidates-fill", "cells", "edges"]) {
    map.on("mousemove", layer, (event) => {
      map.getCanvas().style.cursor = "pointer";
      popup.setLngLat(event.lngLat).setHTML(hoverText(event.features[0])).addTo(map);
    });
    map.on("mouseleave", layer, () => {
      map.getCanvas().style.cursor = "";
      popup.remove();
    });
  }
}

map.on("load", async () => {
  statusText.textContent = "Loading coverage layers...";
  try {
    const [summary, cells, edges, candidates] = await Promise.all(
      ["summary", "cells", "edges", "candidates"].map(getJson),
    );
    renderSummary(summary);
    addLayers(cells, edges, candidates);
    renderCandidates(candidates);
    addHover();
    statusText.textContent = `${summary.exported_cells.toLocaleString()} grid cells with ≥ ${summary.settings.min_trips} trips and ≥ ${summary.settings.min_users} riders.`;
  } catch (error) {
    statusText.textContent = error.message;
  }
});

document.querySelector("#view-cells").addEventListener("click", () => setView("cells"));
document.querySelector("#view-edges").addEventListener("click", () => setView("edges"));
document.querySelector("#show-candidates").addEventListener("change", (event) => {
  const visibility = event.target.checked ? "visible" : "none";
  map.setLayoutProperty("candidates-fill", "visibility", visibility);
  map.setLayoutProperty("candidates-line", "visibility", visibility);
});
