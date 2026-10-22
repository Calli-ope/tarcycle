const API_URL = "";
const stationSource = "stations";
const routeSource = "route";
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
    layers: [{ id: "osm", type: "raster", source: "osm" }],
  },
});

const fromSelect = document.querySelector("#from");
const toSelect = document.querySelector("#to");
const status = document.querySelector("#status");
let mode = "stations";
let backend = "networkx";
let hasRoute = false;
let routes = [];

function routeSummary(feature) {
  const { route_length_m, time_minutes } = feature.properties;
  const time = time_minutes ? `, ${Math.round(time_minutes)} min` : "";
  return `${(route_length_m / 1000).toFixed(2)} km${time}`;
}

function showRoute(selected) {
  map.getSource(routeSource).setData({
    type: "FeatureCollection",
    features: routes.map((feature, index) => ({
      ...feature,
      properties: { ...feature.properties, index, selected: index === selected },
    })),
  });
  const { backend, fallback } = routes[selected].properties;
  const via = fallback ? `${backend} (Valhalla unavailable)` : backend;
  const others = routes.filter((_, index) => index !== selected).map(routeSummary);
  const alternative = others.length ? ` Alternative (grey, click to switch): ${others.join(", ")}.` : "";
  status.textContent = `${routeSummary(routes[selected])}, routed with ${via}.${alternative}`;
}

function setBackend(nextBackend) {
  backend = nextBackend;
  for (const button of document.querySelectorAll(".backend")) {
    button.classList.toggle("active", button.dataset.backend === backend);
  }
  if (hasRoute) planRoute();
}

function setMode(nextMode) {
  mode = nextMode;
  document.querySelector("#station-inputs").hidden = mode !== "stations";
  document.querySelector("#address-inputs").hidden = mode !== "addresses";
  document.querySelector("#stations-mode").classList.toggle("active", mode === "stations");
  document.querySelector("#addresses-mode").classList.toggle("active", mode === "addresses");
}

function stationFeatureCollection(stations) {
  return {
    type: "FeatureCollection",
    features: stations.map((station) => ({
      type: "Feature",
      properties: station,
      geometry: {
        type: "Point",
        coordinates: [station.longitude, station.latitude],
      },
    })),
  };
}

function addStationOptions(stations) {
  for (const select of [fromSelect, toSelect]) {
    for (const station of stations) {
      const option = document.createElement("option");
      option.value = station.id;
      option.textContent = `${station.name} (${station.capacity} bikes)`;
      select.append(option);
    }
  }
  if (stations.length > 1) toSelect.selectedIndex = 1;
  map.addSource(stationSource, {
    type: "geojson",
    data: stationFeatureCollection(stations),
  });
  map.addLayer({
    id: "station-points",
    type: "circle",
    source: stationSource,
    paint: {
      "circle-color": "#1683d8",
      "circle-radius": 6,
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 2,
    },
  });
}

async function loadStations() {
  status.textContent = "Loading stations...";
  const response = await fetch(`${API_URL}/api/stations`);
  if (!response.ok) throw new Error("Could not load stations.");
  addStationOptions(await response.json());
  status.textContent = "Choose two stations.";
}

async function planRoute() {
  if (mode === "stations" && fromSelect.value === toSelect.value) {
    status.textContent = "Choose two different stations.";
    return;
  }
  status.textContent = "Calculating route...";
  const query = new URLSearchParams({ mode, backend });
  if (mode === "stations") {
    query.set("from", fromSelect.value);
    query.set("to", toSelect.value);
  } else {
    query.set("from_address", document.querySelector("#from-address").value);
    query.set("to_address", document.querySelector("#to-address").value);
  }
  try {
    const response = await fetch(`${API_URL}/api/route?${query}`);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `Route calculation failed (${response.status}).`);
    routes = body.features;
    const bounds = new maplibregl.LngLatBounds();
    for (const feature of routes) {
      for (const coordinate of feature.geometry.coordinates) bounds.extend(coordinate);
    }
    map.fitBounds(bounds, { padding: 60 });
    hasRoute = true;
    showRoute(0);
  } catch (error) {
    status.textContent = error.message;
  }
}

map.on("load", () => {
  map.addSource(routeSource, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "route-alternative",
    type: "line",
    source: routeSource,
    filter: ["!", ["get", "selected"]],
    paint: { "line-color": "#64748b", "line-width": 5, "line-opacity": 0.7 },
  });
  map.addLayer({
    id: "route-line",
    type: "line",
    source: routeSource,
    filter: ["get", "selected"],
    paint: { "line-color": "#1d70f7", "line-width": 5, "line-opacity": 0.9 },
  });
  map.on("click", "route-alternative", (event) => showRoute(event.features[0].properties.index));
  map.on("mouseenter", "route-alternative", () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", "route-alternative", () => (map.getCanvas().style.cursor = ""));
  loadStations().catch((error) => {
    status.textContent = error.message;
  });
});

document.querySelector("#route").addEventListener("click", planRoute);
document.querySelector("#stations-mode").addEventListener("click", () => setMode("stations"));
document.querySelector("#addresses-mode").addEventListener("click", () => setMode("addresses"));
for (const button of document.querySelectorAll(".backend")) {
  button.addEventListener("click", () => setBackend(button.dataset.backend));
}
