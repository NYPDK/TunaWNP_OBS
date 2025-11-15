# OBS Music Overlay

Simple OBS integration to merge WebNowPlaying Redux data with a Tuna HTTP feed in OBS.

## Requirements
- [OBS Studio](https://obsproject.com/) (to load the Python script and browser widget)
- Python 3.9+ with the following packages installed in the same interpreter that OBS uses:
  - `pywnp` (install via `pip install pywnp`)
  - `obspython` is provided by OBS and does not need a separate install
- WebNowPlaying Redux (2.0+) providing the WebSocket feed via the browser extension:
  - Chrome version: [WebNowPlaying Redux on the Chrome Web Store](https://chromewebstore.google.com/detail/webnowplaying/jfakgfcdgpghbbefmdfjkbdlibjgnbli)
  - Firefox version: [WebNowPlaying Redux on Mozilla Add-ons](https://addons.mozilla.org/en-US/firefox/addon/webnowplaying)
- [Tuna OBS plugin](https://obsproject.com/forum/resources/tuna.843/) hosting the HTTP server at `http://127.0.0.1:1608/`; this is required as the primary Spotify data source for the overlay.

## Installation
1. Copy the whole repository next to your OBS configuration so `wnp_tuna_overlay.py` and the `widgets` directory stay together.
2. Install the Python dependency: `pip install pywnp`
3. Launch OBS and add `wnp_tuna_overlay.py` as a script (Tools ▶ Scripts ▶ `wnp_tuna_overlay.py`).
4. In the script properties, pick a widget (e.g., `GlowCard`).

## Customization
- Modify `widgets/GlowCard.css`/`GlowCard.js` if you need a different layout or animation.
- You can add more widgets by updating `widgets/manifest.json` and providing new HTML/CSS/JS bundles.

## Troubleshooting
- If covers stall, refresh the browser source in OBS; the widget caches `pendingCoverUrl` only briefly and should restart cleanly.
- Ensure the WebSocket port 6534 is reachable by WebNowPlaying Redux and that any fallback Tuna server responds with JSON containing `title`, `artist`, and `cover_url`.

## Credits
- WebNowPlaying Redux (and its widgets: Spotify, Modern, ModernCard, Minimalistic, etc.) is maintained by keifufu. This overlay leverages those assets and the WebSocket feed they provide.
- The Tuna OBS plugin is a project created and maintained by **univrsal** (aka **universallp**); 
