"""OBS script that merges WebNowPlaying Redux data with a Tuna HTTP feed."""

import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from threading import Event, Lock, Thread
import json
import mimetypes
import time
import urllib.request
from urllib.parse import parse_qs, unquote, urlparse
from pathlib import Path

# Ignore ConnectionResetError logs from asyncio's Proactor when clients disconnect.
def _asyncio_ignore_connection_reset(loop, context):
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError):
        return
    handler = getattr(loop, "default_exception_handler", None)
    if handler:
        handler(context)

if not getattr(asyncio, "_wnp_tuna_overlay_patched", False):
    _original_new_event_loop = asyncio.new_event_loop

    def _wnp_new_event_loop():
        loop = _original_new_event_loop()
        loop.set_exception_handler(_asyncio_ignore_connection_reset)
        return loop

    asyncio.new_event_loop = _wnp_new_event_loop
    asyncio._wnp_tuna_overlay_patched = True

import obspython as obs
from pywnp import WNPRedux

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
DEFAULT_TUNA_URL = "http://127.0.0.1:1608/"
DEFAULT_TUNA_POLL_MS = 600
DEFAULT_FORMAT = "{title} - {artist} ({position}/{duration})"
FALLBACK_COVER_URL = (
    "https://raw.githubusercontent.com/keifufu/WebNowPlaying-Redux-OBS/main/widgets/images/nocover.png"
)
PALETTE_PROXY_PORT = 65432

# Script settings (mutated by OBS)
selected_widget = "None"
custom_format = DEFAULT_FORMAT
default_cover_url = ""
tuna_url = DEFAULT_TUNA_URL
tuna_poll_ms = DEFAULT_TUNA_POLL_MS
widgets_manifest = []

SCRIPT_DIR = Path(__file__).parent
LOCAL_WIDGETS_DIR = SCRIPT_DIR / "widgets"
LOCAL_WIDGETS_MANIFEST = LOCAL_WIDGETS_DIR / "manifest.json"

# Shared runtime state
latest_data = {"wnp": None, "tuna": None}
data_lock = Lock()
tuna_thread = None
tuna_stop = Event()
palette_proxy_server = None
palette_proxy_thread = None
last_tuna_track_id = None
last_tuna_progress_sec = 0.0
last_tuna_timestamp = 0.0

_timer_cleanup_done = False
_tuna_cleanup_done = False
_palette_cleanup_done = False
_wnp_cleanup_done = False
_frontend_callback_registered = False

SOURCE_KEY_ALIASES = {
    "Player": ["Player", "PlayerName"],
}

_widget_removed = False

_OBS_FRONTEND_EVENT_EXIT = getattr(obs, "OBS_FRONTEND_EVENT_EXIT", None)
_OBS_FRONTEND_EVENT_SHUTDOWN = getattr(obs, "OBS_FRONTEND_EVENT_SHUTDOWN", None)
_OBS_FRONTEND_EXIT_EVENTS = tuple(
    event for event in (_OBS_FRONTEND_EVENT_EXIT, _OBS_FRONTEND_EVENT_SHUTDOWN) if event is not None
)


class ThreadedPaletteProxy(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class PaletteProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/palette":
            self.send_error(404)
            return
        query = parse_qs(parsed.query)
        raw_url = query.get("url", [""])[0] if query else ""
        target_url = unquote(raw_url or "")
        if not target_url:
            self.send_error(400)
            return

        parsed_target = urlparse(target_url)
        scheme = (parsed_target.scheme or "").lower()
        try:
            if scheme in ("http", "https"):
                req = urllib.request.Request(target_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = response.read()
                    info = response.info()
                    content_type = info.get_content_type() or "application/octet-stream"
            elif scheme == "file":
                local_target = parsed_target.path or ""
                if parsed_target.netloc:
                    local_target = f"//{parsed_target.netloc}{local_target}"
                fs_path = urllib.request.url2pathname(local_target)
                local_path = Path(fs_path)
                if not local_path.is_file():
                    self.send_error(404)
                    return
                data = local_path.read_bytes()
                content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
            else:
                self.send_error(400)
                return
        except Exception:
            self.send_error(502)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=30")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def start_palette_proxy():
    global palette_proxy_server, palette_proxy_thread
    if palette_proxy_server:
        return
    try:
        palette_proxy_server = ThreadedPaletteProxy(("127.0.0.1", PALETTE_PROXY_PORT), PaletteProxyHandler)
    except OSError as exc:
        print(f"WNP_TUNA - INFO: Palette proxy disabled: {exc}")
        palette_proxy_server = None
        palette_proxy_thread = None
        return
    palette_proxy_thread = Thread(target=palette_proxy_server.serve_forever, daemon=True)
    palette_proxy_thread.start()


def stop_palette_proxy():
    global palette_proxy_server, palette_proxy_thread
    if palette_proxy_server:
        try:
            palette_proxy_server.shutdown()
            palette_proxy_server.server_close()
        except Exception as exc:
            print(f"WNP_TUNA - WARN: error stopping palette proxy: {exc}")
    palette_proxy_server = None
    if palette_proxy_thread and palette_proxy_thread.is_alive():
        try:
            palette_proxy_thread.join(timeout=2.0)
        except Exception as exc:
            print(f"WNP_TUNA - WARN: error joining palette proxy thread: {exc}")
        if palette_proxy_thread.is_alive():
            print("WNP_TUNA - WARN: palette proxy thread still alive after join")
    palette_proxy_thread = None


def _on_frontend_event(event):
    if event in _OBS_FRONTEND_EXIT_EVENTS:
        _remove_widget_source()
        _remove_update_timer()
        _stop_tuna_poller_once()
        _stop_palette_proxy_once()
        _stop_wnp_once()
        _unregister_frontend_callback()


def _remove_update_timer():
    global _timer_cleanup_done
    if _timer_cleanup_done:
        return
    _timer_cleanup_done = True
    try:
        obs.timer_remove(update)
    except Exception as exc:
        print(f"WNP_TUNA - WARN: timer_remove failed: {exc}")


def _stop_tuna_poller_once():
    global _tuna_cleanup_done
    if _tuna_cleanup_done:
        return
    _tuna_cleanup_done = True
    stop_tuna_poller()


def _stop_palette_proxy_once():
    global _palette_cleanup_done
    if _palette_cleanup_done:
        return
    _palette_cleanup_done = True
    stop_palette_proxy()


def _stop_wnp_once():
    global _wnp_cleanup_done
    if _wnp_cleanup_done:
        return
    _wnp_cleanup_done = True
    try:
        wnp_started = getattr(WNPRedux, "is_started", None)
        if wnp_started is None or wnp_started:
            print("WNP_TUNA - INFO: Stopping WNPRedux...")
            WNPRedux.stop()
            print("WNP_TUNA - INFO: WNPRedux stopped")
    except Exception as exc:
        print(f"WNP_TUNA - ERROR: WNPRedux.stop failed: {exc}")


def _register_frontend_callback():
    global _frontend_callback_registered
    if _frontend_callback_registered:
        return
    obs.obs_frontend_add_event_callback(_on_frontend_event)
    _frontend_callback_registered = True


def _unregister_frontend_callback():
    global _frontend_callback_registered
    if not _frontend_callback_registered:
        return
    obs.obs_frontend_remove_event_callback(_on_frontend_event)
    _frontend_callback_registered = False


def _remove_widget_source():
    global _widget_removed
    if _widget_removed:
        return
    _widget_removed = True
    source = obs.obs_get_source_by_name("WNP-Widget")
    if not source:
        return
    try:
        obs.obs_source_remove(source)
    finally:
        obs.obs_source_release(source)


def load_local_widget_manifest():
    if not LOCAL_WIDGETS_MANIFEST.exists():
        return []
    try:
        with LOCAL_WIDGETS_MANIFEST.open("r", encoding="utf-8") as file:
            data = json.load(file)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def script_description():
    return (
        "<b>WebNowPlaying + Tuna overlay</b><br>"
        "Starts pywnp directly so the legacy wnp-obs.py helper is no longer required and keeps the same OBS sources while falling back to a Tuna HTTP feed whenever WebNowPlaying is idle."
    )


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "selected_widget", "None")
    obs.obs_data_set_default_string(settings, "custom_format", DEFAULT_FORMAT)
    obs.obs_data_set_default_string(settings, "default_cover_url", "")
    obs.obs_data_set_default_string(settings, "tuna_url", DEFAULT_TUNA_URL)
    obs.obs_data_set_default_int(settings, "tuna_poll_ms", DEFAULT_TUNA_POLL_MS)


def script_properties():
    props = obs.obs_properties_create()

    widget_list = obs.obs_properties_add_list(
        props, "selected_widget", "Widget", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    obs.obs_property_list_add_string(widget_list, "None", "None")

    global widgets_manifest
    widgets_manifest = []
    try:
        req = urllib.request.Request(
            "https://raw.githubusercontent.com/keifufu/WebNowPlaying-Redux-OBS/main/widgets/manifest.json",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode())
            if isinstance(payload, list):
                widgets_manifest.extend(payload)
    except Exception:
        widgets_manifest = []

    local_widgets = load_local_widget_manifest()
    if local_widgets:
        widgets_manifest.extend(local_widgets)

    for widget in widgets_manifest:
        widget_name = widget.get("name")
        if widget_name:
            obs.obs_property_list_add_string(widget_list, widget_name, widget_name)

    obs.obs_properties_add_text(props, "custom_format", "Format", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_button(props, "create_sources", "Create Sources", create_sources)
    obs.obs_properties_add_text(props, "default_cover_url", "Default Cover URL", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "tuna_url", "Tuna URL", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_int(props, "tuna_poll_ms", "Tuna poll interval (ms)", 100, 5000, 100)
    return props


def script_update(settings):
    global custom_format, default_cover_url, selected_widget, tuna_url, tuna_poll_ms
    previous_url = tuna_url
    previous_interval = tuna_poll_ms

    selected_widget = obs.obs_data_get_string(settings, "selected_widget") or "None"
    custom_format = obs.obs_data_get_string(settings, "custom_format") or DEFAULT_FORMAT
    default_cover_url = obs.obs_data_get_string(settings, "default_cover_url") or ""
    tuna_url = obs.obs_data_get_string(settings, "tuna_url") or DEFAULT_TUNA_URL
    tuna_poll_ms = max(100, obs.obs_data_get_int(settings, "tuna_poll_ms") or DEFAULT_TUNA_POLL_MS)

    update_widget()
    if tuna_url != previous_url or tuna_poll_ms != previous_interval:
        restart_tuna_poller()


def script_load(settings):
    global _timer_cleanup_done, _tuna_cleanup_done, _palette_cleanup_done, _wnp_cleanup_done, _widget_removed
    _timer_cleanup_done = _tuna_cleanup_done = _palette_cleanup_done = _wnp_cleanup_done = False
    _widget_removed = False
    _unregister_frontend_callback()

    def logger(level, message):
        print(f"WNP_TUNA - {level}: {message}")

    WNPRedux.start(6534, "2.0.0", logger)
    start_tuna_poller()
    start_palette_proxy()
    obs.timer_add(update, 250)
    _register_frontend_callback()


def script_unload():
    print("WNP_TUNA - INFO: script_unload called")

    _remove_widget_source()
    _remove_update_timer()
    _stop_tuna_poller_once()
    _stop_palette_proxy_once()
    _stop_wnp_once()
    _unregister_frontend_callback()


# ---------------------------------------------------------------------------
# Data collection and normalization
# ---------------------------------------------------------------------------

def update():
    capture_wnp()
    data = pick_active_data()
    if data:
        render_data(data)
    else:
        clear_sources()


def capture_wnp():
    if not WNPRedux.is_started:
        with data_lock:
            latest_data["wnp"] = None
        return

    media = WNPRedux.media_info
    if not media or not media.title:
        with data_lock:
            latest_data["wnp"] = None
        return

    normalized = normalize_wnp(media)
    with data_lock:
        latest_data["wnp"] = normalized


def start_tuna_poller():
    global tuna_thread
    if tuna_thread and tuna_thread.is_alive():
        return
    tuna_stop.clear()
    tuna_thread = Thread(target=run_tuna_poller, daemon=True)
    tuna_thread.start()


def stop_tuna_poller():
    global tuna_thread
    tuna_stop.set()
    if tuna_thread:
        try:
            tuna_thread.join(timeout=3.0)
        except Exception as exc:
            print(f"WNP_TUNA - WARN: error joining tuna poller thread: {exc}")
        if tuna_thread.is_alive():
            print("WNP_TUNA - WARN: tuna poller thread still alive after join")
    tuna_thread = None


def restart_tuna_poller():
    stop_tuna_poller()
    start_tuna_poller()


def run_tuna_poller():
    while not tuna_stop.wait(max(0.1, tuna_poll_ms / 1000)):
        try:
            req = urllib.request.Request(tuna_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=2) as response:
                payload = json.loads(response.read().decode())
        except Exception:
            with data_lock:
                latest_data["tuna"] = None
            continue

        normalized = normalize_tuna(payload)
        normalized = adjust_tuna_progress(normalized)
        with data_lock:
            latest_data["tuna"] = normalized


def pick_active_data():
    with data_lock:
        wnp_data = latest_data.get("wnp")
        if is_playing(wnp_data):
            return wnp_data
        tuna_data = latest_data.get("tuna")
        if is_playing(tuna_data):
            return tuna_data
    return None


def is_playing(info):
    if not info:
        return False
    status = str(info.get("status", "")).lower()
    return (
        info.get("title")
        and info.get("durationSec", 0) > 0
        and status not in {"stopped", "paused"}
    )


def normalize_wnp(media):
    duration_sec = parse_time(media.duration)
    position_sec = parse_time(media.position)
    percent = int(media.position_percent) if media.position_percent is not None else 0
    artist_value = media.artist or ""
    artists = [part.strip() for part in artist_value.replace(" feat.", ",").split(",") if part.strip()]
    return {
        "player_name": media.player_name or "WebNowPlaying",
        "title": media.title or "",
        "artist": artist_value,
        "artists": artists or ([artist_value] if artist_value else []),
        "album": media.album or "",
        "durationSec": duration_sec,
        "progressSec": position_sec,
        "positionPercent": str(percent),
        "coverUrl": media.cover_url or getattr(media, "cover", "") or "",
        "status": media.state or "",
        "trackUrl": getattr(media, "track_url", "") or getattr(media, "url", "") or "",
    }


def normalize_tuna(payload):
    if not isinstance(payload, dict):
        return None
    title = str(payload.get("title", "")).strip()
    if not title:
        return None

    artists_field = payload.get("artists")
    artists = []
    if isinstance(artists_field, list):
        artists = [str(item or "").strip() for item in artists_field if str(item or "").strip()]
    artist_value = ", ".join(artists) if artists else str(payload.get("artist", "")).strip()

    duration_sec = parse_seconds(payload.get("duration")) or parse_seconds(payload.get("duration_ms"))
    progress_sec = parse_seconds(payload.get("progress")) or parse_seconds(payload.get("position"))
    percent = "0"
    if duration_sec and duration_sec > 0:
        percent = str(int(min(100, (progress_sec / duration_sec) * 100)))

    raw_cover = payload.get("cover_url") or payload.get("cover") or ""

    return {
        "player_name": str(payload.get("player", "Tuna")),
        "title": title,
        "artist": artist_value,
        "artists": artists or ([artist_value] if artist_value else []),
        "album": payload.get("album") or "",
        "durationSec": duration_sec,
        "progressSec": progress_sec,
        "positionPercent": percent,
        "coverUrl": normalize_cover_url(raw_cover),
        "status": payload.get("status") or payload.get("state") or "",
        "trackUrl": payload.get("url") or payload.get("track_url") or "",
    }


def _get_tuna_track_identifier(data):
    if not data:
        return None
    return (
        (data.get("title") or "").strip(),
        (data.get("artist") or "").strip(),
        (data.get("trackUrl") or "").strip(),
        data.get("durationSec"),
    )


def adjust_tuna_progress(data):
    global last_tuna_track_id, last_tuna_progress_sec, last_tuna_timestamp
    if not data:
        last_tuna_track_id = None
        last_tuna_progress_sec = 0.0
        last_tuna_timestamp = 0.0
        return data

    now = time.monotonic()
    duration_sec = data.get("durationSec") or 0
    progress_sec = data.get("progressSec") or 0
    track_id = _get_tuna_track_identifier(data)

    if last_tuna_track_id == track_id and last_tuna_timestamp:
        elapsed = max(0.0, now - last_tuna_timestamp)
        expected_progress = last_tuna_progress_sec + elapsed
        if expected_progress > progress_sec:
            clamped = expected_progress
            if duration_sec:
                clamped = min(clamped, duration_sec)
            progress_sec = clamped

    data["progressSec"] = progress_sec
    if duration_sec:
        percent = min(100, (progress_sec / duration_sec) * 100)
        data["positionPercent"] = str(int(percent))
    else:
        data["positionPercent"] = "0"

    last_tuna_track_id = track_id
    last_tuna_progress_sec = progress_sec
    last_tuna_timestamp = now
    return data


def normalize_cover_url(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme in ("http", "https", "file"):
        return text
    if scheme:
        return ""
    try:
        return Path(text).expanduser().resolve().as_uri()
    except Exception:
        return ""


def parse_seconds(value):
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric / 1000 if numeric > 10000 else numeric
    text = str(value).strip()
    if not text:
        return 0
    if ":" in text:
        total = 0
        for part in text.split(":"):
            try:
                total = total * 60 + float(part)
            except ValueError:
                pass
        return total
    digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    try:
        numeric = float(digits)
    except ValueError:
        return 0
    return numeric / 1000 if numeric > 10000 else numeric


def parse_time(text):
    return parse_seconds(text)


def format_mmss(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------------------
# OBS source management
# ---------------------------------------------------------------------------

def render_data(data):

    player_name = data.get("player_name") or "N/A"
    title = data.get("title") or "N/A"
    artist = data.get("artist") or "N/A"
    album = data.get("album") or ""
    duration = format_mmss(data.get("durationSec"))
    position = format_mmss(data.get("progressSec"))
    percent = data.get("positionPercent", "0")
    cover_url = data.get("coverUrl") or default_cover_url or FALLBACK_COVER_URL

    update_source(["Player", "PlayerName"], "text", player_name)
    update_source("Title", "text", title)
    update_source("Artist", "text", artist)
    update_source("Album", "text", album)
    update_source("Duration", "text", duration)
    update_source("Position", "text", position)
    update_source("Cover", "url", cover_url)

    try:
        formatted = custom_format.format(
            player_name=player_name,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            position=position,
            positionPercent=percent,
            position_percent=percent,
        )
        update_source("Formatted", "text", formatted)
    except Exception:
        pass

def clear_sources():
    render_data(
        {
            "player_name": "N/A",
            "title": "N/A",
            "artist": "N/A",
            "album": "",
            "durationSec": 0,
            "progressSec": 0,
            "positionPercent": "0",
            "coverUrl": default_cover_url or FALLBACK_COVER_URL,
        }
    )


def create_sources(*_args):
    text_sources = {
        "Player": "N/A",
        "Title": "N/A",
        "Artist": "N/A",
        "Album": "",
        "Duration": "0:00",
        "Position": "0:00",
        "Formatted": custom_format.format(
            player_name="N/A",
            title="N/A",
            artist="N/A",
            album="",
            duration="0:00",
            position="0:00",
            positionPercent="0",
            position_percent="0",
        ),
    }
    for name, placeholder in text_sources.items():
        aliases = SOURCE_KEY_ALIASES.get(name, [name])
        for alias in aliases:
            create_text_source(f"WNP-{alias}", placeholder)
    create_cover_source("WNP-Cover", default_cover_url or FALLBACK_COVER_URL)
    update_widget()


def update_source(source_key, field, value):
    keys = source_key if isinstance(source_key, (list, tuple)) else (source_key,)
    for key in keys:
        source = obs.obs_get_source_by_name(f"WNP-{key}")
        if source is None:
            continue
        settings = obs.obs_data_create()
        obs.obs_data_set_string(settings, field, str(value))
        obs.obs_source_update(source, settings)
        obs.obs_data_release(settings)
        obs.obs_source_release(source)


def create_text_source(name, placeholder):
    source = obs.obs_get_source_by_name(name)
    if source is None:
        current_scene = obs.obs_frontend_get_current_scene()
        scene = obs.obs_scene_from_source(current_scene)
        settings = obs.obs_data_create()
        obs.obs_data_set_string(settings, "text", placeholder)
        source = obs.obs_source_create("text_gdiplus", name, settings, None)
        obs.obs_scene_add(scene, source)
        obs.obs_scene_release(scene)
        obs.obs_data_release(settings)
    if source:
        obs.obs_source_release(source)


def create_cover_source(name, url):
    source = obs.obs_get_source_by_name(name)
    if source is None:
        current_scene = obs.obs_frontend_get_current_scene()
        scene = obs.obs_scene_from_source(current_scene)
        settings = obs.obs_data_create()
        obs.obs_data_set_string(settings, "url", url)
        obs.obs_data_set_int(settings, "width", 300)
        obs.obs_data_set_int(settings, "height", 300)
        obs.obs_data_set_string(settings, "css", "img { width: auto; height: 100%; object-fit: cover; }")
        source = obs.obs_source_create("browser_source", name, settings, None)
        obs.obs_scene_add(scene, source)
        obs.obs_scene_release(scene)
        obs.obs_data_release(settings)
    if source:
        obs.obs_source_release(source)


def get_widget_entry(name):
    if not widgets_manifest:
        return None
    return next((item for item in widgets_manifest if item.get("name") == name), None)


def build_widget_url(entry, fallback_name):
    if not entry:
        return ""
    local_path = entry.get("local_path")
    if local_path:
        target = (LOCAL_WIDGETS_DIR / local_path).resolve()
        return target.as_uri()
    return f"https://raw.githack.com/keifufu/WebNowPlaying-Redux-OBS/main/widgets/{fallback_name}.html"


def update_widget():
    source = obs.obs_get_source_by_name("WNP-Widget")
    if selected_widget == "None":
        if source:
            obs.obs_source_remove(source)
            obs.obs_source_release(source)
        return

    entry = get_widget_entry(selected_widget)
    if entry is None:
        return

    width = entry.get("width", 0)
    height = entry.get("height", 0)
    url = entry.get("url") or build_widget_url(entry, selected_widget)
    css = (
        "body { background-color: rgba(0, 0, 0, 0); margin: 0 auto; overflow: hidden; } "
        + ":root { --default-cover-url: url(\"%s\"); }"
    ) % (default_cover_url or FALLBACK_COVER_URL)

    if source is None:
        current_scene = obs.obs_frontend_get_current_scene()
        scene = obs.obs_scene_from_source(current_scene)
        settings = obs.obs_data_create()
        obs.obs_data_set_string(settings, "url", url)
        obs.obs_data_set_int(settings, "width", width)
        obs.obs_data_set_int(settings, "height", height)
        obs.obs_data_set_string(settings, "css", css)
        source = obs.obs_source_create("browser_source", "WNP-Widget", settings, None)
        obs.obs_scene_add(scene, source)
        obs.obs_scene_release(scene)
        obs.obs_data_release(settings)
    else:
        settings = obs.obs_data_create()
        obs.obs_data_set_string(settings, "url", url)
        obs.obs_data_set_int(settings, "width", width)
        obs.obs_data_set_int(settings, "height", height)
        obs.obs_data_set_string(settings, "css", css)
        obs.obs_source_update(source, settings)
        obs.obs_data_release(settings)
        obs.obs_source_release(source)
