const API_URL = "";
// Grid-cell classes from the first analysis.
const CLASSES = {
  cycle_infra: { label: "On cycle infrastructure", color: "#898781" },
  road_no_infra: { label: "Road without cycle infra", color: "#2a78d6" },
  osm_not_bikeable: { label: "OSM way closed to bikes", color: "#1baf7a" },
  no_osm_way: { label: "No OSM way at all", color: "#eb6834" },
};
// Gaps in the router graph. Separate and partly-alongside paths are the gaps
// GPS covers and share one green; grey marks the low-value group (sidewalks
// next to roads the router already has).
const GAPS = {
  separate: { label: "Separate path", color: "#1baf7a", extraWidth: 2 },
  partly: { label: "Partly alongside a router way", color: "#1baf7a", extraWidth: 2 },
  alongside: { label: "Alongside a router way", color: "#898781", extraWidth: 1 },
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
const toggles = [...document.querySelectorAll("input[data-layers]")];
let gapFeatures = [];

async function getJson(layer) {
  const response = await fetch(`${API_URL}/api/coverage/${layer}`);
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || `Could not load ${layer}.`);
  return body;
}

function percent(value) {
  return `${(value * 100).toFixed(value < 0.1 ? 1 : 0)}%`;
}

function renderSummary(summary, router, ways) {
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
  const groups = summary.gps_layer.gap_groups;
  const covered = ["separate", "partly"].map((name) => groups[name]);
  const coveredKm = covered.reduce((sum, group) => sum + group.ridden_km, 0);
  const coveredShare = covered.reduce((sum, group) => sum + group.share_of_gap_trip_km, 0);
  document.querySelector("#gap-covered").textContent = `${coveredKm.toFixed(1)} km · ${percent(coveredShare)}`;
  document.querySelector("#gap-alongside").textContent =
    `${groups.alongside.ridden_km} km · ${percent(groups.alongside.share_of_gap_trip_km)}`;
  const routerKm = router.features.reduce((sum, feature) => sum + lineLength(feature.geometry), 0);
  document.querySelector("#router-km").textContent = `${Math.round(routerKm).toLocaleString()} km`;
  document.querySelector("#ridden-count").textContent = `${ways.features.length.toLocaleString()} ways`;
}

// Rough length in km of a (Multi)LineString in lon/lat; good enough for a legend.
function lineLength(geometry) {
  const lines = geometry.type === "LineString" ? [geometry.coordinates] : geometry.coordinates;
  let km = 0;
  for (const line of lines) {
    for (let index = 1; index < line.length; index += 1) {
      const [x1, y1] = line[index - 1];
      const [x2, y2] = line[index];
      const dx = (x2 - x1) * 111.32 * Math.cos(((y1 + y2) / 2) * (Math.PI / 180));
      km += Math.hypot(dx, (y2 - y1) * 110.57);
    }
  }
  return km;
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
  const status = properties.gap_group
    ? `<strong>${GAPS[properties.gap_group].label}</strong>, not in router graph (${Math.round(properties.alongside_share * 100)}% within 25 m of a router way)`
    : "in router graph";
  return `${wayName(properties)} (${properties.highway})<br />${status}<br />
    ${properties.trips} trips · ${properties.ridden_m} of ${properties.length_m} m ridden`;
}

function candidateText(properties) {
  return `#${properties.rank} · ${CLASSES[properties.cell_class].label}<br />
    ${properties.length_m} m long · median ${properties.median_trips} trips (max ${properties.max_trips})`;
}

function renderGapList() {
  const shown = new Set(
    toggles.filter((input) => input.checked && input.dataset.group).flatMap((input) => input.dataset.group.split(",")),
  );
  const list = document.querySelector("#gap-list");
  list.replaceChildren();
  const top = gapFeatures
    .filter((feature) => shown.has(feature.properties.gap_group))
    .slice(0, 15);
  if (!top.length) list.innerHTML = "<li>Switch on a gap group above.</li>";
  for (const feature of top) {
    const { properties } = feature;
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "candidate";
    button.innerHTML = `<span class="swatch" style="background:${GAPS[properties.gap_group].color}"></span>
      <span>${wayName(properties)}</span><span class="value">${properties.ridden_m} m · ${properties.trips} trips</span>`;
    button.addEventListener("click", () => {
      const bounds = boundsOf(feature.geometry);
      map.fitBounds(bounds, { padding: 120, maxZoom: 17 });
      popup.setLngLat(bounds.getCenter()).setHTML(wayText(properties)).addTo(map);
    });
    item.append(button);
    list.append(item);
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

function addLayers({ cells, edges, ways, router, candidates }) {
  const hidden = { visibility: "none", "line-cap": "round" };
  // First-analysis layers at the bottom.
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
  const edgeScale = tripScale(edges.features);
  map.addSource("edges", { type: "geojson", data: edges });
  map.addLayer({
    id: "edges",
    type: "line",
    source: "edges",
    layout: hidden,
    paint: { "line-color": edgeScale.color, "line-width": edgeScale.width(0) },
  });

  // 2 · Where riders go.
  const wayScale = tripScale(ways.features);
  document.querySelector("#ramp-max").textContent = wayScale.maxTrips.toLocaleString();
  map.addSource("ways", { type: "geojson", data: ways });
  map.addLayer({
    id: "gps-ways",
    type: "line",
    source: "ways",
    layout: hidden,
    paint: { "line-color": wayScale.color, "line-width": wayScale.width(0) },
  });

  // 1 · The router's graph: a thin dark reference line above the GPS colours.
  map.addSource("router", { type: "geojson", data: router });
  map.addLayer({
    id: "router",
    type: "line",
    source: "router",
    layout: { visibility: "none" },
    paint: {
      "line-color": "#172033",
      "line-width": ["interpolate", ["linear"], ["zoom"], 12, 0.6, 16, 2],
      "line-opacity": 0.8,
    },
  });

  // 3 · Gaps, drawn on top; least valuable group lowest.
  for (const name of ["alongside", "partly", "separate"]) {
    map.addLayer({
      id: `gap-${name}`,
      type: "line",
      source: "ways",
      filter: ["==", ["get", "gap_group"], name],
      layout: hidden,
      paint: { "line-color": GAPS[name].color, "line-width": wayScale.width(GAPS[name].extraWidth) },
    });
  }

  map.addSource("candidates", { type: "geojson", data: candidates });
  map.addLayer({
    id: "candidates-fill",
    type: "fill",
    source: "candidates",
    layout: { visibility: "none" },
    paint: { "fill-color": classColor, "fill-opacity": 0.2 },
  });
  map.addLayer({
    id: "candidates-line",
    type: "line",
    source: "candidates",
    layout: { visibility: "none" },
    paint: { "line-color": classColor, "line-width": 2 },
  });
}

function applyToggle(input) {
  for (const layer of input.dataset.layers.split(",")) {
    map.setLayoutProperty(layer, "visibility", input.checked ? "visible" : "none");
  }
  if (input.id === "toggle-cells") document.querySelector("#cells-legend").hidden = !input.checked;
  if (input.dataset.group) renderGapList();
}

function hoverText(feature) {
  const { properties } = feature;
  const id = feature.layer.id;
  if (id === "gps-ways" || id.startsWith("gap-")) return wayText(properties);
  if (id === "router") return `${wayName(properties)} (${properties.highway})<br />In the router's graph`;
  if (id === "edges") {
    return `${wayName(properties)}<br />${properties.trips} trips · ${properties.edge_class.replace("_", " ")}`;
  }
  if (id === "cells") return `${CLASSES[properties.cell_class].label}<br />${properties.trips} trips`;
  return candidateText(properties);
}

function addHover() {
  const layers = ["candidates-fill", "cells", "edges", "gps-ways", "router", "gap-alongside", "gap-partly", "gap-separate"];
  for (const layer of layers) {
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
    const names = ["summary", "cells", "edges", "ways", "router", "candidates"];
    const loaded = Object.fromEntries(
      (await Promise.all(names.map(getJson))).map((body, index) => [names[index], body]),
    );
    renderSummary(loaded.summary, loaded.router, loaded.ways);
    addLayers(loaded);
    const busiest = (feature) => feature.properties.trips * feature.properties.ridden_m;
    gapFeatures = loaded.ways.features
      .filter((feature) => feature.properties.gap_group)
      .sort((a, b) => busiest(b) - busiest(a));
    toggles.forEach(applyToggle);
    addHover();
    const { summary } = loaded;
    statusText.textContent = `${summary.exported_cells.toLocaleString()} grid cells with ≥ ${summary.settings.min_trips} trips and ≥ ${summary.settings.min_users} riders.`;
  } catch (error) {
    statusText.textContent = error.message;
  }
});

for (const input of toggles) {
  input.addEventListener("change", () => applyToggle(input));
}
