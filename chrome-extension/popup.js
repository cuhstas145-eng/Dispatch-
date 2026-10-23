const APP_URL = "https://cargosync-dispatch.onrender.com/";

document.getElementById("openApp").addEventListener("click", async () => {
  await chrome.tabs.create({ url: APP_URL });
  window.close();
});

document.getElementById("openPanel").addEventListener("click", async () => {
  const status = document.getElementById("status");
  try {
    await chrome.sidePanel.open({ windowId: (await chrome.windows.getCurrent()).id });
    window.close();
  } catch (error) {
    status.textContent = "Could not open the side panel. Open it from Chrome's extension menu.";
  }
});
