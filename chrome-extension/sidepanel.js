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
function drawMap(points,geometry) {
  const svg=$("routeMap"); svg.replaceChildren();
  const NS="http://www.w3.org/2000/svg";
  const all=geometry.coordinates;
  const lons=all.map(c=>c[0]), lats=all.map(c=>c[1]);
  let minX=Math.min(...lons),maxX=Math.max(...lons),minY=Math.min(...lats),maxY=Math.max(...lats);
  if(maxX-minX<.01){minX-=.01;maxX+=.01;} if(maxY-minY<.01){minY-=.01;maxY+=.01;}
  const pad=18, w=320-2*pad,h=180-2*pad;
  const xy=c=>[pad+(c[0]-minX)/(maxX-minX)*w,180-pad-(c[1]-minY)/(maxY-minY)*h];
  for(let i=0;i<5;i++){const line=document.createElementNS(NS,"path");const y=pad+i*h/4;line.setAttribute("d","M "+pad+" "+y+" H "+(320-pad));line.setAttribute("stroke","#263747");line.setAttribute("stroke-width","1");svg.appendChild(line);}
  const path=document.createElementNS(NS,"polyline");path.setAttribute("points",all.map(c=>xy(c).join(",")).join(" "));path.setAttribute("fill","none");path.setAttribute("stroke","#49d49a");path.setAttribute("stroke-width","3");path.setAttribute("stroke-linecap","round");path.setAttribute("stroke-linejoin","round");svg.appendChild(path);
  points.forEach((p,i)=>{const [x,y]=xy([p.lon,p.lat]);const circ=document.createElementNS(NS,"circle");circ.setAttribute("cx",x);circ.setAttribute("cy",y);circ.setAttribute("r",5);circ.setAttribute("fill",i===0?"#73b7ff":i===points.length-1?"#f5c46a":"#49d49a");circ.setAttribute("stroke","#0b1118");circ.setAttribute("stroke-width","2");svg.appendChild(circ);const t=document.createElementNS(NS,"text");t.setAttribute("x",x+7);t.setAttribute("y",y-7);t.setAttribute("fill","#e8edf5");t.setAttribute("font-size","9");t.textContent=String(i+1);svg.appendChild(t);});
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
