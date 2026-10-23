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
  setupLocationInput(row.querySelector("input"));
  row.querySelector("button").addEventListener("click", () => row.remove());
  $("stops").appendChild(row);
}
$("addStop").addEventListener("click", () => addStop());
$("openWorkspace").addEventListener("click", () => chrome.tabs.create({url:APP_URL}));
function setMessage(text, error=false) { $("message").textContent=text; $("message").classList.toggle("error",error); }
function featureLabel(feature) {
  const p = feature.properties || {};
  const name = p.name || p.street || p.city || p.county || "Unnamed location";
  const details = [p.housenumber && p.street ? p.housenumber+" "+p.street : (!p.housenumber ? p.street : ""), p.district, p.city || p.town || p.village, p.state, p.country]
    .filter(Boolean).filter((v,i,a)=>a.indexOf(v)===i && v!==name);
  return {name, detail:details.join(", "), full:[name,...details].filter(Boolean).join(", ")};
}
function setupLocationInput(input) {
  if (input.dataset.suggestReady) return;
  input.dataset.suggestReady = "1";
  input.setAttribute("autocomplete","off");
  const field = input.closest(".field");
  const list = document.createElement("div");
  list.className = "location-suggestions hidden";
  list.setAttribute("role","listbox");
  list.setAttribute("aria-label","Location suggestions");
  field.appendChild(list);
  let timer = null, controller = null, activeIndex = -1, matches = [];
  const close = () => { list.classList.add("hidden"); list.replaceChildren(); activeIndex=-1; };
  const choose = feature => {
    const [lon,lat] = feature.geometry.coordinates;
    const label = featureLabel(feature);
    input.value = label.full;
    input.dataset.lat = String(lat);
    input.dataset.lon = String(lon);
    input.dataset.place = label.full;
    close();
    input.dispatchEvent(new Event("change",{bubbles:true}));
  };
  const paint = features => {
    matches = features;
    list.replaceChildren();
    activeIndex = -1;
    features.forEach(feature => {
      const label = featureLabel(feature);
      const option = document.createElement("button");
      option.type = "button";
      option.className = "location-option";
      option.setAttribute("role","option");
      const title = document.createElement("strong");
      title.textContent = label.name;
      const detail = document.createElement("span");
      detail.textContent = label.detail || label.full;
      option.append(title,detail);
      option.addEventListener("pointerdown",event=>event.preventDefault());
      option.addEventListener("click",()=>choose(feature));
      list.appendChild(option);
    });
    if(features.length) list.classList.remove("hidden");
    else close();
  };
  input.addEventListener("input",()=>{
    delete input.dataset.lat; delete input.dataset.lon; delete input.dataset.place;
    clearTimeout(timer);
    if(controller) controller.abort();
    const q=input.value.trim();
    if(q.length<2){close();return;}
    list.classList.remove("hidden");
    list.innerHTML='<div class="suggestion-status">Searching locations…</div>';
    timer=setTimeout(async()=>{
      controller=new AbortController();
      try{
        const url="https://photon.komoot.io/api/?q="+encodeURIComponent(q)+"&limit=7&lang=en";
        const response=await fetch(url,{signal:controller.signal,headers:{"Accept":"application/json"}});
        if(!response.ok) throw new Error("Location search unavailable");
        const data=await response.json();
        if(input.value.trim()!==q)return;
        const features=(data.features||[]).filter(f=>f.geometry?.coordinates?.length===2 && (f.properties?.name||f.properties?.street));
        paint(features);
        if(!features.length){list.innerHTML='<div class="suggestion-status">No matches. Try adding a state or country.</div>';list.classList.remove("hidden");}
      }catch(err){if(err.name!=="AbortError" && input.value.trim()===q){list.innerHTML='<div class="suggestion-status">Suggestions unavailable. You can still enter a full city or address.</div>';list.classList.remove("hidden");}}
    },250);
  });
  input.addEventListener("keydown",event=>{
    const options=[...list.querySelectorAll(".location-option")];
    if(list.classList.contains("hidden"))return;
    if(event.key==="ArrowDown" && options.length){event.preventDefault();activeIndex=Math.min(activeIndex+1,options.length-1);}
    else if(event.key==="ArrowUp" && options.length){event.preventDefault();activeIndex=Math.max(activeIndex-1,0);}
    else if(event.key==="Enter" && activeIndex>=0 && matches[activeIndex]){event.preventDefault();choose(matches[activeIndex]);return;}
    else if(event.key==="Escape"){close();return;}
    else return;
    options.forEach((el,i)=>{el.classList.toggle("active",i===activeIndex);el.setAttribute("aria-selected",String(i===activeIndex));});
    options[activeIndex]?.scrollIntoView({block:"nearest"});
  });
  input.addEventListener("blur",()=>setTimeout(close,160));
}
["truck","pickup","delivery"].forEach(id=>setupLocationInput($(id)));
document.addEventListener("pointerdown",event=>{if(!event.target.closest(".field"))document.querySelectorAll(".location-suggestions").forEach(el=>el.classList.add("hidden"));});
async function geocode(query, input) {
  const lat=Number(input?.dataset.lat), lon=Number(input?.dataset.lon);
  if(input?.dataset.place && Number.isFinite(lat) && Number.isFinite(lon)) return {name:input.dataset.place,lat,lon};
  const url="https://photon.komoot.io/api/?q="+encodeURIComponent(query)+"&limit=1&lang=en";
  const res=await fetch(url,{headers:{"Accept":"application/json"}});
  if(!res.ok) throw new Error("Location search unavailable ("+res.status+")");
  const data=await res.json();
  const feature=data.features?.[0];
  if(!feature?.geometry?.coordinates) throw new Error('Location not found: "'+query+'". Select a suggestion or try adding a state/country.');
  const [pointLon,pointLat]=feature.geometry.coordinates;
  return {name:featureLabel(feature).full,lat:Number(pointLat),lon:Number(pointLon)};
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
let mapZoom = 5;
let mapCenter = [39,-96];
let currentGeometry = null;
let currentPoints = [];
function project(lat, lon, zoom) {
  const scale = 256 * Math.pow(2, zoom);
  const sin = Math.sin(lat * Math.PI / 180);
  return {x:(lon + 180) / 360 * scale, y:(0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * scale};
}
function unproject(x,y,zoom) {
  const scale=256*Math.pow(2,zoom);
  return {lon:x/scale*360-180,lat:Math.atan(Math.sinh(Math.PI*(1-2*y/scale)))*180/Math.PI};
}
function fitMap() {
  if(!currentGeometry?.coordinates?.length) return;
  const coords=currentGeometry.coordinates;
  const lons=coords.map(c=>c[0]),lats=coords.map(c=>c[1]);
  const midLon=(Math.min(...lons)+Math.max(...lons))/2,midLat=(Math.min(...lats)+Math.max(...lats))/2;
  mapCenter=[midLat,midLon];
  const box=$("routeMap").getBoundingClientRect(), width=box.width||400,height=box.height||300;
  for(let z=12;z>=2;z--){
    const pts=coords.map(c=>project(c[1],c[0],z));
    if(Math.max(...pts.map(p=>p.x))-Math.min(...pts.map(p=>p.x)) < width-60 && Math.max(...pts.map(p=>p.y))-Math.min(...pts.map(p=>p.y)) < height-60){mapZoom=z;break;}
  }
}
function renderMap() {
 const map=$("routeMap"),tiles=$("mapTiles"),svg=$("routeOverlay");
 if(!map||!tiles||!svg)return;
 const width=map.clientWidth||400,height=map.clientHeight||300;
 tiles.replaceChildren();svg.replaceChildren();svg.setAttribute("viewBox",`0 0 ${width} ${height}`);
 const center=project(mapCenter[0],mapCenter[1],mapZoom);
 const left=center.x-width/2,top=center.y-height/2;
 const minTX=Math.floor(left/256),maxTX=Math.floor((left+width)/256),minTY=Math.floor(top/256),maxTY=Math.floor((top+height)/256),n=Math.pow(2,mapZoom);
 for(let x=minTX;x<=maxTX;x++)for(let y=minTY;y<=maxTY;y++){
  if(y<0||y>=n)continue;
  const img=document.createElement("img");img.alt="";img.draggable=false;img.loading="lazy";
  img.src=`https://tile.openstreetmap.org/${mapZoom}/${((x%n)+n)%n}/${y}.png`;
  img.style.cssText=`position:absolute;width:256px;height:256px;left:${x*256-left}px;top:${y*256-top}px;max-width:none;`;
  img.onerror=()=>{img.style.background="#dfe5e8";};tiles.appendChild(img);
 }
 if(currentGeometry?.coordinates?.length){
  const ns="http://www.w3.org/2000/svg";
  const toXY=c=>{const p=project(c[1],c[0],mapZoom);return [p.x-left,p.y-top];};
  const line=document.createElementNS(ns,"polyline");line.setAttribute("points",currentGeometry.coordinates.map(c=>toXY(c).join(",")).join(" "));line.setAttribute("fill","none");line.setAttribute("stroke","#13a979");line.setAttribute("stroke-width","5");line.setAttribute("stroke-linecap","round");line.setAttribute("stroke-linejoin","round");line.setAttribute("vector-effect","non-scaling-stroke");svg.appendChild(line);
  currentPoints.forEach((p,i)=>{const [x,y]=toXY([p.lon,p.lat]);if(x<-15||x>width+15||y<-15||y>height+15)return;const circ=document.createElementNS(ns,"circle");circ.setAttribute("cx",x);circ.setAttribute("cy",y);circ.setAttribute("r","6");circ.setAttribute("fill",i===0?"#73b7ff":i===currentPoints.length-1?"#f5c46a":"#49d49a");circ.setAttribute("stroke","#fff");circ.setAttribute("stroke-width","2");svg.appendChild(circ);});
 }
}
function drawMap(points,geometry) {
 currentPoints=points;currentGeometry=geometry;fitMap();renderMap();
}
$("zoomIn").addEventListener("click",()=>{mapZoom=Math.min(18,mapZoom+1);renderMap()});
$("zoomOut").addEventListener("click",()=>{mapZoom=Math.max(2,mapZoom-1);renderMap()});
$("fitRoute").addEventListener("click",()=>{fitMap();renderMap()});
let drag=null;
$("routeMap").addEventListener("pointerdown",e=>{if(e.target.closest(".map-controls,.map-attribution"))return;drag={x:e.clientX,y:e.clientY,center:project(mapCenter[0],mapCenter[1],mapZoom)};$("routeMap").setPointerCapture(e.pointerId)});
$("routeMap").addEventListener("pointermove",e=>{if(!drag)return;const p=unproject(drag.center.x-(e.clientX-drag.x),drag.center.y-(e.clientY-drag.y),mapZoom);mapCenter=[p.lat,p.lon];renderMap()});
$("routeMap").addEventListener("pointerup",()=>drag=null);
$("routeMap").addEventListener("pointercancel",()=>drag=null);
$("routeMap").addEventListener("wheel",e=>{e.preventDefault();mapZoom=Math.max(2,Math.min(18,mapZoom+(e.deltaY<0?1:-1)));renderMap()},{passive:false});
window.addEventListener("resize",()=>{if(currentGeometry)renderMap()});

function updateRPM(){const rate=Number($("rate").value)||0;const miles=Number($("rpmMiles").value)||0;$("rpm").textContent="$"+(miles>0?rate/miles:0).toFixed(2)+" / mi";}
$("rate").addEventListener("input",updateRPM);$("rpmMiles").addEventListener("input",updateRPM);
$("routeForm").addEventListener("submit",async e=>{
 e.preventDefault();
 const button=$("calculate");button.disabled=true;button.textContent="Calculating…";$("results").classList.add("hidden");setMessage("Finding locations and calculating route…");
 try{
  const inputs=[$("truck"),$("pickup"),...document.querySelectorAll(".stop-input"),$("delivery")];
  const names=inputs.map(input=>input.value.trim());
  if(names.some(n=>!n)) throw new Error("Fill in truck location, pickup, and delivery.");
  const points=[];for(let i=0;i<names.length;i++){setMessage("Confirming "+names[i]+"…");points.push(await geocode(names[i],inputs[i]));}
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
