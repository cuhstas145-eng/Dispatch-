const APP_URL = "https://cargosync-dispatch.onrender.com/";

document.getElementById("openFull").addEventListener("click", () => {
  chrome.tabs.create({ url: APP_URL });
});
