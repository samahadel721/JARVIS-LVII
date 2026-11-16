"""
plugins/aircraft_report.py — what is flying over you right now.

Drop this single file into `plugins/`. Standard library only, no API key, no
account: it reads the OpenSky Network's public state vectors, which is the same
feed the flight-tracking sites consume.

AN API WITHOUT A KEY IS AN API WORTH BEING POLITE TO
    Anonymous OpenSky access is rate-limited per day. Every answer is cached for
    a minute, and a repeat question inside that window is served from the cache
    rather than from their servers. That is not only good manners — it is the
    difference between this plugin still working tomorrow and being blocked.

WHAT IT ANSWERS
    How many aircraft are overhead, which airline and where each is going by
    callsign, how high, how fast, and which way they are pointing. Plus a radar
    view: a local scope with the aircraft plotted around the house, exactly like
    the scope in a control tower, opened in the browser.

HONESTY ABOUT COVERAGE
    The feed only carries aircraft that broadcast ADS-B and are inside receiver
    range. Military traffic, some private jets and anything in a gap in the
    network simply are not there. The plugin says "I can see N" rather than
    "there are N", because those are different claims.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PLUGIN = {
    "name": "aircraft_report",
    "description": (
        "Reports live aircraft overhead: how many, which airline and flight "
        "number, altitude, speed and heading, and opens a radar scope with them "
        "plotted around the location. Use for: 'what planes are flying over', "
        "'how many planes are above me', 'is there a flight near me', 'show me "
        "the radar', 'what is that plane noise'. Reads live ADS-B data. Do NOT "
        "use web_search or flight_finder for this — flight_finder prices "
        "tickets, this one watches the sky."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'report' (default) for the aircraft overhead, 'radar' to "
                    "open the scope in the browser, or 'nearest' for just the "
                    "closest one."
                ),
            },
            "place": {
                "type": "STRING",
                "description": "Optional place to watch instead, e.g. 'Cairo', 'London'.",
            },
            "radius_km": {
                "type": "NUMBER",
                "description": "How far out to look, in kilometres (default 120).",
            },
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "aircraft_report",
    "title": "Aircraft Report / رادار الطائرات",
    "fields": [
        {"key": "latitude", "type": "text", "label": "Latitude / خط العرض",
         "default": "31.04", "placeholder": "31.04"},
        {"key": "longitude", "type": "text", "label": "Longitude / خط الطول",
         "default": "31.38", "placeholder": "31.38"},
        {"key": "radius_km", "type": "text", "label": "Search radius (km) / نطاق البحث",
         "default": "120"},
        {"key": "open_browser", "type": "toggle", "label": "Open the radar in a browser",
         "default": True},
    ],
}

#: Same gazetteer as the globe, trimmed to what a radius query needs.
PLACES = {
    "al mansurah": (31.04, 31.38), "mansoura": (31.04, 31.38), "cairo": (30.04, 31.24),
    "alexandria": (31.20, 29.92), "tanta": (30.79, 31.00), "giza": (30.01, 31.21),
    "luxor": (25.69, 32.64), "aswan": (24.09, 32.90), "hurghada": (27.26, 33.81),
    "sharm el sheikh": (27.91, 34.33), "mecca": (21.42, 39.83), "riyadh": (24.71, 46.68),
    "dubai": (25.20, 55.27), "doha": (25.29, 51.53), "kuwait": (29.38, 47.99),
    "amman": (31.95, 35.93), "beirut": (33.89, 35.50), "istanbul": (41.01, 28.98),
    "london": (51.51, -0.13), "paris": (48.86, 2.35), "new york": (40.71, -74.01),
    "tokyo": (35.68, 139.69), "frankfurt": (50.11, 8.68), "dubai intl": (25.25, 55.36),
    "القاهرة": (30.04, 31.24), "المنصورة": (31.04, 31.38), "الإسكندرية": (31.20, 29.92),
    "مكة": (21.42, 39.83), "دبي": (25.20, 55.27), "الرياض": (24.71, 46.68),
}

_ENDPOINT = "https://opensky-network.org/api/states/all"

#: ICAO three-letter airline prefixes worth naming. Anything absent keeps its
#: raw callsign, which is still useful — a registration is a fine answer too.
_AIRLINES = {
    "MSR": "EgyptAir", "ELY": "El Al", "SVA": "Saudia", "UAE": "Emirates",
    "QTR": "Qatar Airways", "ETH": "Ethiopian", "THY": "Turkish Airlines",
    "DLH": "Lufthansa", "BAW": "British Airways", "AFR": "Air France",
    "KLM": "KLM", "RYR": "Ryanair", "EZY": "easyJet", "SWR": "SWISS",
    "AUA": "Austrian", "IBE": "Iberia", "SAS": "SAS", "AEE": "Aegean",
    "RJA": "Royal Jordanian", "MEA": "Middle East Airlines", "KAC": "Kuwait Airways",
    "GFA": "Gulf Air", "OMA": "Oman Air", "ETD": "Etihad", "FDB": "flydubai",
    "AIC": "Air India", "SIA": "Singapore Airlines", "QFA": "Qantas",
    "UAL": "United", "DAL": "Delta", "AAL": "American", "SWA": "Southwest",
    "ACA": "Air Canada", "JAL": "Japan Airlines", "ANA": "All Nippon",
    "CPA": "Cathay Pacific", "KAL": "Korean Air", "CCA": "Air China",
    "CSC": "Sichuan Airlines", "CSN": "China Southern", "CES": "China Eastern",
    "THA": "Thai Airways", "MAS": "Malaysia Airlines", "TAR": "Tunisair",
    "RAM": "Royal Air Maroc", "DAH": "Air Algérie", "LAA": "Libyan Airlines",
    "SUD": "Sudan Airways", "KQA": "Kenya Airways", "NIA": "Nile Air",
    "TFL": "TUI fly", "TCX": "Thomas Cook", "VLG": "Vueling", "WZZ": "Wizz Air",
}

#: OpenSky state-vector field order — the feed is positional, so the names live
#: here once instead of as magic numbers scattered through the file.
_FIELDS = ("icao24", "callsign", "origin_country", "time_position", "last_contact",
           "longitude", "latitude", "baro_altitude", "on_ground", "velocity",
           "true_track", "vertical_rate", "sensors", "geo_altitude", "squawk",
           "spi", "position_source")

_cache: dict = {"when": 0.0, "key": "", "rows": []}
_CACHE_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Location and settings
# ---------------------------------------------------------------------------

def _base_dir() -> Path:
    try:
        from memory.config_manager import BASE_DIR
        return Path(BASE_DIR)
    except Exception:
        return Path(__file__).resolve().parent.parent


def _config() -> dict:
    values = {"latitude": "31.04", "longitude": "31.38", "radius_km": "120",
              "open_browser": True}
    try:
        from memory.config_manager import get_plugin_config
        for key, value in (get_plugin_config("aircraft_report") or {}).items():
            if value not in (None, ""):
                values[key] = value
    except Exception:
        pass
    return values


def _number(value, fallback: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _resolve(place: str, config: dict) -> tuple[float, float, str]:
    wanted = str(place or "").strip().lower()
    if wanted:
        if wanted in PLACES:
            lat, lon = PLACES[wanted]
            return lat, lon, str(place).strip().title()
        for key, (lat, lon) in PLACES.items():
            if key in wanted or wanted in key:
                return lat, lon, key.title()
    return (_number(config.get("latitude"), 31.04),
            _number(config.get("longitude"), 31.38), "your location")


# ---------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------

def _bounding_box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """A lat/lon box that certainly contains the circle of `radius_km`."""
    d_lat = radius_km / 111.0
    # Longitude degrees shrink towards the poles; guard the divide at the poles.
    d_lon = radius_km / (111.0 * max(0.05, math.cos(math.radians(lat))))
    return (max(-90.0, lat - d_lat), min(90.0, lat + d_lat),
            max(-180.0, lon - d_lon), min(180.0, lon + d_lon))


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine — the real distance on a sphere, not the flat approximation."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _compass(bearing: float) -> str:
    points = ("north", "north-east", "east", "south-east",
              "south", "south-west", "west", "north-west")
    return points[int((bearing + 22.5) % 360 // 45)]


def _airline(callsign: str) -> str:
    code = callsign[:3].upper()
    return _AIRLINES.get(code, "")


def _fetch_rows(lat: float, lon: float, radius_km: float) -> tuple[list[dict], str]:
    """Aircraft inside the circle. Second return value explains an empty answer."""
    key = f"{lat:.2f},{lon:.2f},{radius_km:.0f}"
    now = time.time()
    if _cache["key"] == key and (now - _cache["when"]) < _CACHE_SECONDS:
        return _cache["rows"], "cache"

    south, north, west, east = _bounding_box(lat, lon, radius_km)
    url = (f"{_ENDPOINT}?lamin={south:.4f}&lomin={west:.4f}"
           f"&lamax={north:.4f}&lomax={east:.4f}")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "JARVIS-aircraft/1.0"})
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("the free flight feed is rate-limiting me for a "
                               "moment — ask again in a minute")
        raise RuntimeError(f"the flight feed answered {exc.code}")
    except Exception as exc:
        raise RuntimeError(f"could not reach the flight feed: {exc}")

    rows: list[dict] = []
    for state in payload.get("states") or []:
        if len(state) < 17:
            continue
        row = dict(zip(_FIELDS, state[:17]))
        if row.get("latitude") is None or row.get("longitude") is None:
            continue
        if row.get("on_ground"):
            continue                       # taxiing aircraft are not "overhead"
        distance = _distance_km(lat, lon, row["latitude"], row["longitude"])
        if distance > radius_km:
            continue
        callsign = str(row.get("callsign") or "").strip()
        altitude = row.get("geo_altitude") or row.get("baro_altitude") or 0.0
        rows.append({
            "callsign": callsign or row["icao24"].upper(),
            "airline": _airline(callsign),
            "country": str(row.get("origin_country") or ""),
            "distance": round(distance, 1),
            "bearing": round(_bearing(lat, lon, row["latitude"], row["longitude"]), 1),
            "direction": _compass(_bearing(lat, lon, row["latitude"], row["longitude"])),
            "heading": round(float(row.get("true_track") or 0.0), 0),
            "altitude_m": round(float(altitude or 0.0)),
            "speed_kmh": round(float(row.get("velocity") or 0.0) * 3.6),
            "climb": round(float(row.get("vertical_rate") or 0.0), 1),
            "lat": round(float(row["latitude"]), 4),
            "lon": round(float(row["longitude"]), 4),
        })
    rows.sort(key=lambda a: a["distance"])

    _cache.update({"when": now, "key": key, "rows": rows})
    return rows, "live"


def _coverage_note(rows: list[dict], source: str) -> str:
    stamp = ""
    if source == "cache":
        age = int(time.time() - _cache["when"])
        stamp = f" (checked {age}s ago)"
    if not rows:
        return ("I can see nothing in that circle right now" + stamp +
                ". The feed only carries aircraft that broadcast ADS-B inside "
                "receiver range, so an empty sky here is not proof of an empty sky.")
    return ""


# ---------------------------------------------------------------------------
# The radar scope
# ---------------------------------------------------------------------------

_RADAR_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>JARVIS · RADAR — __LABEL__</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 html,body{height:100%;background:#000407;overflow:hidden;color:#8fdcff;
   font-family:'Courier New',ui-monospace,monospace}
 canvas{display:block;width:100%;height:100%}
 .hud{position:fixed;pointer-events:none;font-size:11px;letter-spacing:.13em;
   text-transform:uppercase;text-shadow:0 0 8px #46e0ff88}
 #t{top:16px;left:20px;font-size:13px;color:#46e0ff;letter-spacing:.3em}
 #t small{display:block;font-size:9px;color:#2f7f9c;letter-spacing:.2em;margin-top:3px}
 #l{top:16px;right:20px;text-align:right;line-height:1.8;color:#5fb8d8}
 #l b{color:#9fe8ff;font-weight:400}
 #b{bottom:14px;left:20px;color:#2f7f9c;line-height:1.7}
</style></head><body>
<canvas id="scope"></canvas>
<div class="hud" id="t">J.A.R.V.I.S · RADAR<small>__LABEL__ · __RANGE__ KM</small></div>
<div class="hud" id="l"><div>TRACKS <b id="n">0</b></div><div>UPDATED <b id="u">—</b></div></div>
<div class="hud" id="b">the scope sweeps with each pass · range rings every __RING__ km</div>
<script>
"use strict";
const AC = __AIRCRAFT__, RANGE = __RANGE__;
const cv = document.getElementById("scope"), ctx = cv.getContext("2d");
let W, H, cx, cy, R, sweep = 0, dpr = 1;
function resize(){
  dpr = Math.min(devicePixelRatio || 1, 2);
  W = innerWidth; H = innerHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  cx = W / 2; cy = H / 2; R = Math.min(W, H) * .42;
}
function ring(km, lw, alpha){
  ctx.beginPath(); ctx.arc(cx, cy, R * km / RANGE, 0, 6.2832);
  ctx.strokeStyle = "rgba(70,224,255," + alpha + ")"; ctx.lineWidth = lw; ctx.stroke();
}
function draw(){
  ctx.fillStyle = "#000407"; ctx.fillRect(0, 0, W, H);
  for (let km = RANGE / 4; km <= RANGE; km += RANGE / 4) ring(km, .8, .16);
  ctx.strokeStyle = "rgba(70,224,255,.10)"; ctx.lineWidth = .8;
  for (let a = 0; a < 360; a += 30){
    const rad = (a - 90) * Math.PI / 180;
    ctx.beginPath(); ctx.moveTo(cx, cy);
    ctx.lineTo(cx + R * Math.cos(rad), cy + R * Math.sin(rad)); ctx.stroke();
  }
  ctx.font = "9px 'Courier New',monospace"; ctx.fillStyle = "rgba(70,224,255,.45)";
  ctx.textAlign = "center";
  for (let km = RANGE / 4; km <= RANGE; km += RANGE / 4)
    ctx.fillText(km + "", cx + 3, cy - R * km / RANGE + 11);
  ["N","E","S","W"].forEach(function(c, i){
    const rad = (i * 90 - 90) * Math.PI / 180;
    ctx.fillStyle = "rgba(70,224,255,.6)";
    ctx.fillText(c, cx + (R + 16) * Math.cos(rad), cy + (R + 16) * Math.sin(rad) + 3);
  });
  // الحلقة الرادارية
  const grad = ctx.createConicGradient ? null : null;
  ctx.save(); ctx.beginPath(); ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, R, sweep - .5, sweep); ctx.closePath();
  ctx.fillStyle = "rgba(70,224,255,.13)"; ctx.fill(); ctx.restore();
  // الطائرات
  AC.forEach(function(a){
    const rad = (a.bearing - 90) * Math.PI / 180;
    const r = R * Math.min(1, a.distance / RANGE);
    const x = cx + r * Math.cos(rad), y = cy + r * Math.sin(rad);
    ctx.save(); ctx.translate(x, y); ctx.rotate(a.heading * Math.PI / 180);
    ctx.beginPath(); ctx.moveTo(0, -7); ctx.lineTo(5, 6); ctx.lineTo(0, 3); ctx.lineTo(-5, 6);
    ctx.closePath();
    ctx.fillStyle = a.altitude_m > 9000 ? "#ffd23f" : "#ff9f45";
    ctx.shadowColor = ctx.fillStyle; ctx.shadowBlur = 9; ctx.fill(); ctx.restore();
    ctx.font = "9px 'Courier New',monospace"; ctx.textAlign = "left";
    ctx.fillStyle = "rgba(255,225,150,.72)";
    ctx.fillText(a.callsign, x + 9, y - 5);
    ctx.fillStyle = "rgba(120,190,215,.6)";
    ctx.fillText(a.altitude_m + "m " + a.speed_kmh + "km/h", x + 9, y + 5);
  });
  sweep = (sweep + .012) % 6.2832;
  document.getElementById("u").textContent = new Date().toISOString().slice(11, 19);
  requestAnimationFrame(draw);
}
document.getElementById("n").textContent = AC.length;
addEventListener("resize", resize); resize(); draw();
</script></body></html>
"""


def _build_radar(rows: list[dict], label: str, radius_km: float) -> Path:
    folder = _base_dir() / "config" / ".plugin_cache" / "globe"
    folder.mkdir(parents=True, exist_ok=True)
    page = (_RADAR_PAGE
            .replace("__AIRCRAFT__", json.dumps(rows, ensure_ascii=False))
            .replace("__LABEL__", label.replace("<", "&lt;"))
            .replace("__RANGE__", f"{radius_km:.0f}")
            .replace("__RING__", f"{radius_km / 4:.0f}"))
    path = folder / "radar.html"
    path.write_text(page, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _describe(a: dict) -> str:
    who = f"{a['airline']} {a['callsign']}" if a["airline"] else a["callsign"]
    climb = ""
    if a["climb"] > 1.5:
        climb = ", climbing"
    elif a["climb"] < -1.5:
        climb = ", descending"
    return (f"{who} — {a['distance']:.0f} km {a['direction']} of you, "
            f"{a['altitude_m']:,} m, {a['speed_kmh']:,} km/h, "
            f"heading {a['heading']:.0f}°{climb}")


def _action_report(rows: list[dict], label: str, radius_km: float) -> str:
    if not rows:
        return f"No aircraft in the {radius_km:.0f} km around {label}."
    nearest = rows[0]
    highest = max(rows, key=lambda a: a["altitude_m"])
    lines = [f"{len(rows)} aircraft within {radius_km:.0f} km of {label}:"]
    for a in rows[:8]:
        lines.append("  " + _describe(a))
    if len(rows) > 8:
        lines.append(f"  …and {len(rows) - 8} more on the radar.")
    spoken = (f"I can see {len(rows)} aircraft within {radius_km:.0f} kilometres of "
              f"{label}, sir. The closest is {_describe(nearest)}. "
              f"The highest is {highest['callsign']} at {highest['altitude_m']:,} metres."
              + _coverage_note(rows, "live"))
    return spoken + "\n\n" + "\n".join(lines)


def _action_nearest(rows: list[dict], label: str) -> str:
    if not rows:
        return f"Nothing in range of {label} at the moment, sir."
    a = rows[0]
    return (f"The nearest aircraft to {label} is {_describe(a)}.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    try:
        config = _config()
        action = str(parameters.get("action", "report") or "report").strip().lower()
        lat, lon, label = _resolve(parameters.get("place", ""), config)
        radius = max(10.0, min(500.0, _number(
            parameters.get("radius_km"), _number(config.get("radius_km"), 120.0))))

        rows, source = _fetch_rows(lat, lon, radius)

        if action == "radar":
            path = _build_radar(rows, label, radius)
            opened = False
            if config.get("open_browser", True):
                try:
                    webbrowser.open(path.as_uri())
                    opened = True
                except Exception:
                    pass
            scope = ("I opened the radar scope" if opened
                     else f"The radar scope is saved at {path}")
            if not rows:
                return f"{scope}, sir, but the sky over {label} is empty right now."
            return (f"{scope}, sir — {len(rows)} tracks within {radius:.0f} km of "
                    f"{label}. The nearest is {rows[0]['callsign']}, "
                    f"{rows[0]['distance']:.0f} km {rows[0]['direction']}.")

        if action == "nearest":
            result = _action_nearest(rows, label)
        else:
            result = _action_report(rows, label, radius)

    except Exception as exc:
        return f"Sir, aircraft_report failed: {exc}"

    if player:
        try:
            player.write_log(f"JARVIS: {result.splitlines()[0]}")
        except Exception:
            pass
    return result


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    cfg = _config()
    cfg["open_browser"] = False
    for act, place in ((args[0] if args else "report", args[1] if len(args) > 1 else ""),):
        print(run({"action": act, "place": place}, player=None))
