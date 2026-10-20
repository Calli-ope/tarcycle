const API_URL = "";
// Grey = already fine; the three gap classes use a colour-blind-validated set.
const CLASSES = {
  cycle_infra: { label: "On cycle infrastructure", color: "#898781" },
  road_no_infra: { label: "Road without cycle infra", color: "#2a78d6" },
  osm_not_bikeable: { label: "OSM way closed to bikes", color: "#1baf7a" },
  no_osm_way: { label: "No OSM way at all", color: "#eb6834" },
};
const MISSING_COLOR = "#1baf7a";
const RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"];
const TRIPS_LABEL = "Smart Bike trips in 2025 per way";
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
const VIEWS = {
  ways: ["ways-graph", "ways-missing"],
  cells: ["cells"],
  edges: ["edges"],
};
const rampMax = {};

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
  if (summary.gps_layer) {
    document.querySelector("#missing-km").textContent =
      `${summary.gps_layer.missing_ridden_length_km.toLocaleString()} km`;
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

function wayName(properties) {
  return properties.name || `Unnamed ${properties.highway.replaceAll("_", " ")}`;
}

function wayText(properties) {
  const status = properties.in_bike_graph ? "in router graph" : "missing from router graph";
  return `${wayName(properties)} (${properties.highway}) · ${status}<br />
    ${properties.trips} trips · ${Math.round(properties.covered_share * 100)}% of ${properties.length_m} m ridden`;
}

function candidateText(properties) {
  return `#${properties.rank} · ${CLASSES[properties.cell_class].label}<br />
    ${properties.length_m} m long · median ${properties.median_trips} trips (max ${properties.max_trips})`;
}

function addListItem(list, feature, swatchColor, label, value, text) {
  const item = document.createElement("li");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "candidate";
  button.innerHTML = `<span class="swatch" style="background:${swatchColor}"></span>
    <span>${label}</span><span class="value">${value}</span>`;
  button.addEventListener("click", () => {
    const bounds = boundsOf(feature.geometry);
    map.fitBounds(bounds, { padding: 120, maxZoom: 17 });
    popup.setLngLat(bounds.getCenter()).setHTML(text).addTo(map);
  });
  item.append(button);
  list.append(item);
}

function renderMissingWays(ways) {
  const list = document.querySelector("#missing-list");
  const ridden = (feature) =>
    feature.properties.trips * feature.properties.length_m * feature.properties.covered_share;
  const missing = ways.features
    .filter((feature) => !feature.properties.in_bike_graph)
    .sort((a, b) => ridden(b) - ridden(a))
    .slice(0, 15);
  if (!missing.length) list.innerHTML = "<li>No ridden ways outside the router graph.</li>";
  for (const feature of missing) {
    const { properties } = feature;
    addListItem(
      list,
      feature,
      MISSING_COLOR,
      wayName(properties),
      `${Math.round(properties.length_m * properties.covered_share)} m · ${properties.trips} trips`,
      wayText(properties),
    );
  }
}

function renderCandidates(candidates) {
  const list = document.querySelector("#candidate-list");
  for (const feature of candidates.features.slice(0, 15)) {
    const { properties } = feature;
    addListItem(
      list,
      feature,
      CLASSES[properties.cell_class].color,
      CLASSES[properties.cell_class].label,
      `${properties.length_m} m · ${properties.median_trips} trips`,
      candidateText(properties),
    );
  }
}

// Log-spaced, strictly increasing colour stops (MapLibre requires ascending stops).
function tripScale(features) {
  const maxTrips = Math.max(10, ...features.map((feature) => feature.properties.trips));
  const stops = [];
  for (let index = 0; index < RAMP.length; index += 1) {
    const stop = Math.round(10 * (maxTrips / 10) ** (index / RAMP.length));
    stops.push(Math.max(stop, (stops[index - 1] ?? 9) + 1));
  }
  const color = ["step", ["get", "trips"], RAMP[0], ...stops.slice(1).flatMap((stop, index) => [stop, RAMP[index + 1]])];
  const width = (extra) => [
    "interpolate", ["linear"], ["log10", ["max", ["get", "trips"], 1]],
    1, 1.5 + extra, Math.max(Math.log10(maxTrips), 1.1), 6 + extra,
  ];
  return { maxTrips, color, width };
}

function addLayers(cells, edges, ways, candidates) {
  const edgeScale = tripScale(edges.features);
  rampMax.edges = edgeScale.maxTrips;
  map.addSource("edges", { type: "geojson", data: edges });
  map.addLayer({
    id: "edges",
    type: "line",
    source: "edges",
    layout: { visibility: "none", "line-cap": "round" },
    paint: { "line-color": edgeScale.color, "line-width": edgeScale.width(0) },
  });

  const wayScale = tripScale(ways.features);
  rampMax.ways = wayScale.maxTrips;
  map.addSource("ways", { type: "geojson", data: ways });
  map.addLayer({
    id: "ways-graph",
    type: "line",
    source: "ways",
    filter: ["==", ["get", "in_bike_graph"], true],
    layout: { "line-cap": "round" },
    paint: { "line-color": wayScale.color, "line-width": wayScale.width(0) },
  });
  // Drawn last and thicker so the gaps in the router graph stand out.
  map.addLayer({
    id: "ways-missing",
    type: "line",
    source: "ways",
    filter: ["==", ["get", "in_bike_graph"], false],
    layout: { "line-cap": "round" },
    paint: { "line-color": MISSING_COLOR, "line-width": wayScale.width(2) },
  });

  map.addSource("cells", { type: "geojson", data: cells });
  map.addLayer({
    id: "cells",
    type: "circle",
    source: "cells",
    layout: { visibility: "none" },
    paint: {
      "circle-color": classColor,
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 12, 1.2, 15, 3, 18, 8],
      "circle-opacity": ["interpolate", ["linear"], ["log10", ["get", "trips"]], 1, 0.45, 3, 0.95],
    },
  });

  map.addSource("candidates", { type: "geojson", data: candidates });
  for (const [id, type, paint] of [
    ["candidates-fill", "fill", { "fill-color": classColor, "fill-opacity": 0.2 }],
    ["candidates-line", "line", { "line-color": classColor, "line-width": 2 }],
  ]) {
    map.addLayer({ id, type, source: "candidates", layout: { visibility: "none" }, paint });
  }
}

function setView(view) {
  for (const [name, layers] of Object.entries(VIEWS)) {
    for (const layer of layers) {
      map.setLayoutProperty(layer, "visibility", name === view ? "visible" : "none");
    }
    document.querySelector(`#view-${name}`).classList.toggle("active", name === view);
  }
  document.querySelector("#ways-legend").hidden = view !== "ways";
  document.querySelector("#cells-legend").hidden = view !== "cells";
  document.querySelector("#ramp-legend").hidden = view === "cells";
  if (view !== "cells") {
    document.querySelector("#ramp-min").textContent = "10";
    document.querySelector("#ramp-label").textContent =
      view === "ways" ? `${TRIPS_LABEL} (router graph)` : TRIPS_LABEL;
    document.querySelector("#ramp-max").textContent = rampMax[view].toLocaleString();
  }
}

function hoverText(feature) {
  const { properties } = feature;
  if (feature.layer.id.startsWith("ways")) return wayText(properties);
  if (feature.layer.id === "edges") {
    return `${wayName(properties)}<br />${properties.trips} trips · ${properties.edge_class.replace("_", " ")}`;
  }
  if (feature.layer.id === "cells") {
    return `${CLASSES[properties.cell_class].label}<br />${properties.trips} trips`;
  }
  return candidateText(properties);
}

function addHover() {
  for (const layer of ["candidates-fill", "cells", "edges", "ways-graph", "ways-missing"]) {
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
    const [summary, cells, edges, ways, candidates] = await Promise.all(
      ["summary", "cells", "edges", "ways", "candidates"].map(getJson),
    );
    renderSummary(summary);
    addLayers(cells, edges, ways, candidates);
    renderMissingWays(ways);
    renderCandidates(candidates);
    addHover();
    setView("ways");
    statusText.textContent = `${summary.exported_cells.toLocaleString()} grid cells with ≥ ${summary.settings.min_trips} trips and ≥ ${summary.settings.min_users} riders.`;
  } catch (error) {
    statusText.textContent = error.message;
  }
});

for (const view of Object.keys(VIEWS)) {
  document.querySelector(`#view-${view}`).addEventListener("click", () => setView(view));
}
document.querySelector("#show-candidates").addEventListener("change", (event) => {
  const visibility = event.target.checked ? "visible" : "none";
  map.setLayoutProperty("candidates-fill", "visibility", visibility);
  map.setLayoutProperty("candidates-line", "visibility", visibility);
});
