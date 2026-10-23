const APP_URL = "https://cargosync-dispatch.onrender.com/";
const $ = id => document.getElementById(id);
let stopCount = 0;
let lastRouteMiles = 0;
const stopRows = () => [...document.querySelectorAll(".stop-input")].map(el => el.value.trim()).filter(Boolean);
function addStop(value = "") {
  const id = ++stopCount;
  const row = document.createElement("div");
  row.className = "field stop-field";
  row.innerHTML = '<label for="stop-'+id+'">Additional stop '+id+'</label><div class="stop-input-row"><input class="stop-input" id="stop-'+id+'" placeholder="Stop city or address" autocomplete="street-address"><button type="button" class="remove-stop" aria-label="Remove stop">×</button></div>';
  row.querySelector("input").value = value;
  row.querySelector("button").addEventListener("click", () => row.remove());
  $("stops").appendChild(row);
}
$("addStop").addEventListener("click", () => addStop());
$("openWorkspace").addEventListener("click", () => chrome.tabs.create({url:APP_URL}));
function setMessage(text, error=false) { $("message").textContent=text; $("message").classList.toggle("error",error); }
async function geocode(query) {
  const url = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q="+encodeURIComponent(query);
  const res = await fetch(url, {headers:{"Accept":"application/json"}});
  if(!res.ok) throw new Error("Geocoding service unavailable ("+res.status+")");
  const data = await res.json();
  if(!data.length) throw new Error('Location not found: "'+query+'". Try a more specific city or address.');
  return {name:query,lat:Number(data[0].lat),lon:Number(data[0].lon)};
}
async function routeBetween(points) {
  const coords=points.map(p=>p.lon+","+p.lat).join(";");
  const res=await fetch("https://router.project-osrm.org/route/v1/driving/"+coords+"?overview=full&geometries=geojson&steps=false");
  if(!res.ok) throw new Error("Routing service unavailable ("+res.status+")");
  const data=await res.json();
  if(data.code!=="Ok"||!data.routes?.length) throw new Error("No drivable route found for these locations.");
  return data.routes[0];
}
function fmtDuration(seconds){const h=Math.round(seconds/3600);const d=Math.floor(h/24);return d?d+"d "+(h%24)+"h":h+"h";}
let leafletMap = null;
let routeLayer = null;
let markersLayer = null;
function drawMap(points,geometry) {
  if (!window.L) throw new Error("Interactive map library did not load. Check your internet connection and reload the extension.");
  if (!leafletMap) {
    leafletMap = L.map("routeMap", { zoomControl: true, scrollWheelZoom: true });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>'
    }).addTo(leafletMap);
    routeLayer = L.layerGroup().addTo(leafletMap);
    markersLayer = L.layerGroup().addTo(leafletMap);
  }
  routeLayer.clearLayers();
  markersLayer.clearLayers();
  const latlngs = geometry.coordinates.map(c => [c[1], c[0]]);
  L.polyline(latlngs, { color:"#49d49a", weight:5, opacity:.95, lineJoin:"round" }).addTo(routeLayer);
  points.forEach((p,i) => {
    const color = i === 0 ? "#73b7ff" : i === points.length-1 ? "#f5c46a" : "#49d49a";
    const marker = L.circleMarker([p.lat,p.lon], {radius:7,color:"#0b1118",weight:2,fillColor:color,fillOpacity:1});
    marker.bindTooltip((i+1)+". "+p.name, {direction:"top"});
    marker.addTo(markersLayer);
  });
  leafletMap.fitBounds(L.latLngBounds(latlngs), {padding:[24,24]});
  setTimeout(() => leafletMap.invalidateSize(), 100);
}
function updateRPM(){const rate=Number($("rate").value)||0;const miles=Number($("rpmMiles").value)||0;$("rpm").textContent="$"+(miles>0?rate/miles:0).toFixed(2)+" / mi";}
$("rate").addEventListener("input",updateRPM);$("rpmMiles").addEventListener("input",updateRPM);
$("routeForm").addEventListener("submit",async e=>{
 e.preventDefault();
 const button=$("calculate");button.disabled=true;button.textContent="Calculating…";$("results").classList.add("hidden");setMessage("Finding locations and calculating route…");
 try{
  const names=[$("truck").value.trim(),$("pickup").value.trim(),...stopRows(),$("delivery").value.trim()];
  if(names.some(n=>!n)) throw new Error("Fill in truck location, pickup, and delivery.");
  const points=[];for(const name of names){setMessage("Finding "+name+"…");points.push(await geocode(name));}
  setMessage("Calculating road route…");
  const route=await routeBetween(points);
  const detour=Math.max(0,Number($("detour").value)||0);
  lastRouteMiles=route.distance/1609.344+detour;
  $("miles").textContent=lastRouteMiles.toLocaleString(undefined,{maximumFractionDigits:1})+" mi";
  $("duration").textContent=fmtDuration(route.duration);
  $("rpmMiles").value=lastRouteMiles.toFixed(1);updateRPM();
  const legs=$("legs");legs.replaceChildren();
  route.legs.forEach((leg,i)=>{const li=document.createElement("li");li.innerHTML='<strong></strong><span></span>';li.querySelector("strong").textContent=points[i].name+" → "+points[i+1].name;li.querySelector("span").textContent=(leg.distance/1609.344).toLocaleString(undefined,{maximumFractionDigits:1})+" mi · "+fmtDuration(leg.duration);legs.appendChild(li);});
  drawMap(points,route.geometry);$("results").classList.remove("hidden");setMessage("Route calculated. Review mileage before dispatch.");
 }catch(err){setMessage(err.message||"Unable to calculate this route.",true);}
 finally{button.disabled=false;button.textContent="Calculate route";}
});
