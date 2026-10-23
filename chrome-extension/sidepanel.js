const APP_URL = "https://cargosync-dispatch.onrender.com/";
const open = (suffix = "") => chrome.tabs.create({ url: APP_URL + suffix });

document.getElementById("openRoute").addEventListener("click", () => open(""));
document.getElementById("openSchedule").addEventListener("click", () => open("?view=schedule"));
document.getElementById("openQuickRpm").addEventListener("click", () => open(""));
document.getElementById("openApp").addEventListener("click", () => open(""));
