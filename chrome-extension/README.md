# CargoSync Route Planner — Chrome extension

This is a lightweight Manifest V3 launcher for the existing Render-hosted CargoSync app. It does not duplicate the planner or migrate its data.

## Features
- Toolbar popup with **Open full Route Planner** and **Open in side panel**.
- Side panel embeds the existing app and provides a fallback button to open it in a regular tab.
- Route Planner, Quick RPM Calculator, map, loads, calculations, login, and server-side storage remain in the existing web app.

## Install locally
1. Download this `chrome-extension` folder to your computer.
2. Open `chrome://extensions` in desktop Chrome.
3. Enable **Developer mode**.
4. Choose **Load unpacked** and select the `chrome-extension` folder.
5. Pin **CargoSync Route Planner** from Chrome's Extensions menu.

## Notes
- The current configured URL is `https://cargosync-dispatch.onrender.com/`.
- If your active Render service uses a different URL, update `APP_URL` in `popup.js` and `sidepanel.js`, and the iframe URL in `sidepanel.html`.
- If Render or the app sets anti-framing headers, the embedded view may not render in the side panel. The **Open full app** button is the reliable fallback. Authentication inside embedded third-party contexts may also depend on browser cookie settings.
- This extension is a separate folder on a separate branch. It does not change the production app or deploy branch.
