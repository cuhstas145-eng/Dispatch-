chrome.runtime.onInstalled.addListener(() => {
  if (chrome.sidePanel?.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  }
});
chrome.action.onClicked.addListener(async (tab) => {
  if (tab.windowId != null) {
    try { await chrome.sidePanel.open({ windowId: tab.windowId }); } catch (error) {}
  }
});
