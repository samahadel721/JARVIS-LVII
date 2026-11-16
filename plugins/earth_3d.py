"""
plugins/earth_3d.py — a live, interactive holographic Earth that opens in the browser.

Drop this single file into `plugins/`. No dependencies at all: not plotly, not
numpy, not a map library. The coastline is Natural Earth's 110 m public-domain
outline, compressed into this file, and the globe is drawn by about six hundred
lines of plain JavaScript that the plugin writes into one self-contained HTML
page. That page keeps working with the Wi-Fi off, on any machine with a browser.

WHY NOT THE OBVIOUS ROUTE
    The tempting version renders through a heavyweight 3D library, which means a
    hundred megabytes of dependency, a version that breaks next year, and a blank
    window when it does. What is here instead is an orthographic projection —
    the maths is thirty lines — drawn on a 2-D canvas. It spins, it tilts, it
    zooms, it answers the mouse, and the only thing it needs is the browser that
    every desktop already has.

WHAT IT SHOWS
    · The real coastline, with the night side shaded from the actual position of
      the sun, so the terminator you see is today's, to the minute.
    · Live earthquakes from the USGS feed, sized by magnitude.
    · The International Space Station, where it is right now.
    · Any place you name, marked and centred.

ACTIONS THE MODEL CAN ASK FOR
    show (default) · quakes · iss · focus <place> · all
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import math
import urllib.request
import webbrowser
import zlib
from pathlib import Path

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

PLUGIN = {
    "name": "earth_3d",
    "description": (
        "Opens an interactive 3D globe of Earth in the browser: real coastlines, "
        "the live day/night line, current earthquakes and the position of the "
        "International Space Station. Use for: 'show me the Earth', 'open the "
        "3D globe', 'where is the ISS', 'show me the earthquakes today', 'show "
        "me Egypt on the globe', 'what is the world doing right now'. The user "
        "can drag to spin it and scroll to zoom. Do NOT use screen_process or "
        "web_search for this — it is a rendered globe, not an image or a search."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'show' (default) for the globe with the day/night line, "
                    "'quakes' to add today's earthquakes, 'iss' to add the space "
                    "station, 'focus' to centre it on a named place, or 'all' for "
                    "everything at once."
                ),
            },
            "place": {
                "type": "STRING",
                "description": (
                    "For 'focus': a city or country to centre on, e.g. 'Cairo', "
                    "'Egypt', 'Mecca', 'Tokyo'. Also accepts Arabic names."
                ),
            },
            "zoom": {
                "type": "NUMBER",
                "description": "Optional zoom factor, 1 is the whole globe, 2.5 is close in.",
            },
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "earth_3d",
    "title": "Earth 3D / الكرة الأرضية",
    "fields": [
        {"key": "auto_spin", "type": "toggle", "label": "Spin on open / دوران تلقائي",
         "default": True},
        {"key": "show_quakes", "type": "toggle", "label": "Earthquakes on open / الزلازل",
         "default": True},
        {"key": "show_iss", "type": "toggle", "label": "ISS on open / محطة الفضاء",
         "default": True},
        {"key": "home_lat", "type": "text", "label": "Home latitude / خط عرض البيت",
         "default": "31.04", "placeholder": "31.04"},
        {"key": "home_lon", "type": "text", "label": "Home longitude / خط طول البيت",
         "default": "31.38", "placeholder": "31.38"},
        {"key": "open_browser", "type": "toggle", "label": "Open the browser automatically",
         "default": True},
    ],
}

#: Natural Earth 110 m land polygons — public domain (naturalearthdata.com).
#: Coordinates rounded to one decimal (≈11 km), points that add no shape dropped,
#: then zlib + base64. 5,000 points in 25 KB. `_coastlines()` below unpacks it and
#: `python plugins/earth_3d.py --dump-coast` prints it back as readable text.
COASTLINE_DATA = (
    "eNpdnVeW5DoORDekmkNvTu5/X5OMG6Cy3083iklRFC1s4K/v/43nb6X/pefvS2/R/fkb6X/lS+dTPsr/qsq/v452fjXd/zdF"
    "f1sYw3Wq6qRLZ7fZROd419BTU29/+/D5y98/in74vibzBLXytxtNf9TzRz2tzHVaP39M/TH4I+uPQgNFf7TzR/If38rnPV2t"
    "pef3pZ+/1l3r+0Orp/0v3UXzQBbdopNfusZXteav/Za04ZH6dqqpA196fb9W48vI9qL60E0jrvo9RvBb0jWCU3PTVdOjltyH"
    "b5223c8lut4+Lw9MEv1+11D5VP37vd/RL4yXPjVnWtXg5kxT95eqP77P58KwtjMwp4GhP86wlqI/aKAwfRqtn/ecl3ZXO00X"
    "PTo18OeP8b6n/3SnTPdAzwxXK89va5+/remdGsE99Xg53dzjtPilk+giuqtOEv3t6V4/5Vt9V3lOyQ+fV6fsP85XJf3H684v"
    "U39Mnmm3I7dTn7+haZpss+Wn2/OWL9G84nQnuYPUzNEPumY6vx86q+swQ+XW7G4nXzp7frfoGuM5s5/Kl+aT0/t5M7k/X3ps"
    "96GITvHr0Fb9llCnRs2hxTy0rb41s2rSZv8Zh+/i7NrfQ1/R1fKXbqJXlHOsfGmOnhoH1iiuv0R3HV5Z9BDdf+gtOutQK6KX"
    "6DMvWn1DW9tvGX77eWqqPq1Ntzb1FUk049BuT/SlHJcen+pxe9/L+LCT0nsczzhS71x86R6rYmSvhxTHLvM+sud9aaxmzPJg"
    "TVe302I/M26z+dur6BHjMJuvBFZUjdGb2oKMxpde8dVzqD+arzm8nofocVfpcMvplnfX4ch66wy9a3oNN9H0sIq+Z89k62vM"
    "uRimZvBL5/srPVy+Vtalt+uvOPDdq+0vbXHgM5IrdjrlM2ZtxR7nWq33+tSsMbYre/zPdVh8JU/RZ50v00v0aUdv7Dq0lq6S"
    "Tn+0Wnp1O1UXR482m8afd7X5vrc1l59LQW9Z2gWtuM+nTnKd71vqz7fU4Xa+9Wvzt3/7U4N1+D5Vk6//b6/K8uX4/bXs9wIt"
    "+l4uuEuf9rPH/NDV5d8Wanc59I4LunbPYBVdY65r9Zqhb0X0VPtJdNN7V6yrol3JevteOT1WThleFad+d/n5lur6319Lcflp"
    "J3svfH8tnOGa/bzeVZRpX+dSHr5Gz8XXvSO66Ls3M2uYm3L4tH/r6OTPzfS5jtm/hfvT9Hlv9g2SRHMX6G7zqf79d/v4PxOl"
    "nnHue6n/kGdd65M0/cwC7I0Pp2+15nbPnPl15/Z03e84xdUikhtnPOneTNlUfuLx9DTfH/vp91wZPif7M/1reabvn/1ws5w1"
    "uU2lJyc/3A/JybqeXHyNjUM2t5Srye9j7e7z3P2qdsgdpdMH337y8gn/JeMa3E9Jt7GSb8dKdgvtKXEBzKfUez+W5k5+yeFO"
    "fsn43C+5TNanbH9beWq63WHh60iuxeQ8JFdPe2r1rbUeNo+u5NpMfut2f0V66ri3V52/5PZtX5cv+29j+3ILLbqTD7l9F7bi"
    "A3McMl7cmlmE+rTow/cxrtnDz8J26/ZtWpTjTHnTSTBOQYN1OGME2yyyJ1/y+5Dbd3zPLi1PLxZzvnXrZRR6My+xHo5eSuEV"
    "zox0xkEtTPML8+malnFWBaf6OL/16E57zMecro9gCNODBATJtX2+eDQ39n2s+9u+i3+YnI++6pD7GfMl10vGXKSXLM/lzOYz"
    "Yt7UwvKKojGtycu/Tber7elb/3yA62ZePH3bj21WtT4zX4505nvEzHx3/WVu02Ou9rwNeUJbhJNQTXJYap2ZFzlfwcWtz4I7"
    "0OrjYtAHwAOof1wjWmcruNL8WO493+J7+5zZXJOaAG5ncT6rmvy20DzH39JYqd/GxltXR7/macErngWzlpdRP+Tygln7NrbT"
    "5bN2Nvk9zspbWt3JL9m8YL4VYpWUQy53fY+33Xl7ttft735fbHlGHT40++V7PKZ8lQFHGlraEKe8ep/ML928JZLofesPl582"
    "p3t76rybJaftOt9TmcNa+ynnbH69iZ7eqTkXt3/od9tm7kWXRx/Os939P20Ot3Po+X5vXnd8jyAck3zk4JABjnw7o34p3vfn"
    "+qju8/eCKc19PuX9/ZYyfsqnVwP0vnWWD6ZzO22/61unpiur5Jp/6PJ+o49xjXlt94DLtVtKmaKzJZZDz5hHeCzmrsZ8NdEj"
    "+lOnF92ps1z+HYe6/eyXbuntfwtZ69D1h445OnS/GyS34bVRRMeBd+jhzZnbtNz1nd+2rgSSe+zlb314Yu323Ivp77M+3qGb"
    "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"
)


# ---------------------------------------------------------------------------
# Places worth marking, and where "focus Cairo" lands. English + Arabic names,
# because the model may hand over either.
# ---------------------------------------------------------------------------

PLACES = {
    "al mansurah": (31.04, 31.38), "mansoura": (31.04, 31.38), "egypt": (26.8, 30.8),
    "cairo": (30.04, 31.24), "alexandria": (31.20, 29.92), "giza": (30.01, 31.21),
    "luxor": (25.69, 32.64), "aswan": (24.09, 32.90), "tanta": (30.79, 31.00),
    "mecca": (21.42, 39.83), "makkah": (21.42, 39.83), "medina": (24.47, 39.61),
    "riyadh": (24.71, 46.68), "jeddah": (21.49, 39.19), "dubai": (25.20, 55.27),
    "abu dhabi": (24.45, 54.38), "doha": (25.29, 51.53), "kuwait": (29.38, 47.99),
    "amman": (31.95, 35.93), "beirut": (33.89, 35.50), "damascus": (33.51, 36.29),
    "baghdad": (33.31, 44.36), "jerusalem": (31.78, 35.22), "gaza": (31.50, 34.47),
    "istanbul": (41.01, 28.98), "ankara": (39.93, 32.86), "tehran": (35.69, 51.39),
    "casablanca": (33.57, -7.59), "rabat": (34.02, -6.84), "tunis": (36.80, 10.18),
    "algiers": (36.75, 3.06), "tripoli": (32.89, 13.19), "khartoum": (15.50, 32.56),
    "nairobi": (-1.29, 36.82), "lagos": (6.52, 3.38), "johannesburg": (-26.20, 28.05),
    "london": (51.51, -0.13), "paris": (48.86, 2.35), "berlin": (52.52, 13.40),
    "madrid": (40.42, -3.70), "rome": (41.90, 12.50), "moscow": (55.76, 37.62),
    "athens": (37.98, 23.73), "amsterdam": (52.37, 4.90), "vienna": (48.21, 16.37),
    "new york": (40.71, -74.01), "washington": (38.91, -77.04), "chicago": (41.88, -87.63),
    "los angeles": (34.05, -118.24), "toronto": (43.65, -79.38), "mexico city": (19.43, -99.13),
    "sao paulo": (-23.55, -46.63), "buenos aires": (-34.60, -58.38), "lima": (-12.05, -77.04),
    "delhi": (28.61, 77.21), "mumbai": (19.08, 72.88), "karachi": (24.86, 67.01),
    "beijing": (39.90, 116.41), "shanghai": (31.23, 121.47), "tokyo": (35.68, 139.69),
    "seoul": (37.57, 126.98), "singapore": (1.35, 103.82), "jakarta": (-6.21, 106.85),
    "sydney": (-33.87, 151.21), "melbourne": (-37.81, 144.96),
    "القاهرة": (30.04, 31.24), "مصر": (26.8, 30.8), "الإسكندرية": (31.20, 29.92),
    "المنصورة": (31.04, 31.38), "مكة": (21.42, 39.83), "المدينة": (24.47, 39.61),
    "الرياض": (24.71, 46.68), "دبي": (25.20, 55.27), "الدوحة": (25.29, 51.53),
    "بغداد": (33.31, 44.36), "القدس": (31.78, 35.22), "عمان": (31.95, 35.93),
    "بيروت": (33.89, 35.50), "دمشق": (33.51, 36.29), "الخرطوم": (15.50, 32.56),
    "طوكيو": (35.68, 139.69), "لندن": (51.51, -0.13), "باريس": (48.86, 2.35),
    "نيويورك": (40.71, -74.01), "إسطنبول": (41.01, 28.98), "طرابلس": (32.89, 13.19),
}

#: Cities spaced around the globe so the spin has something to read on every face.
CITIES = [
    (31.04, 31.38, "Al Mansurah"), (30.04, 31.24, "Cairo"), (31.20, 29.92, "Alexandria"),
    (21.42, 39.83, "Mecca"), (24.71, 46.68, "Riyadh"), (25.20, 55.27, "Dubai"),
    (51.51, -0.13, "London"), (48.86, 2.35, "Paris"), (40.71, -74.01, "New York"),
    (35.68, 139.69, "Tokyo"), (1.35, 103.82, "Singapore"), (-33.87, 151.21, "Sydney"),
    (-23.55, -46.63, "Sao Paulo"), (28.61, 77.21, "Delhi"), (-1.29, 36.82, "Nairobi"),
    (55.76, 37.62, "Moscow"), (37.57, 126.98, "Seoul"), (19.43, -99.13, "Mexico City"),
]

_QUAKE_FEED = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
               "{window}.geojson")
_ISS_FEEDS = ("https://api.wheretheiss.at/v1/satellites/25544",
              "http://api.open-notify.org/iss-now.json")


# ---------------------------------------------------------------------------
# Astronomy — the same solar position the prayer times use, so the day/night
# line on this globe and the Fajr time in the other plugin never disagree.
# ---------------------------------------------------------------------------

def _fix_angle(a: float) -> float:
    return a - 360.0 * math.floor(a / 360.0)


def subsolar_point(when: _dt.datetime | None = None) -> tuple[float, float]:
    """
    (latitude, longitude) of the point where the sun is exactly overhead.

    The latitude is the sun's declination; the longitude falls out of the
    equation of time. Everything the night shading needs is these two numbers —
    a point is in daylight when it is less than 90° of arc from them.
    """
    when = when or _dt.datetime.now(_dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)

    jd = when.timestamp() / 86400.0 + 2440587.5
    d = jd - 2451545.0
    g = math.radians(_fix_angle(357.529 + 0.98560028 * d))
    q = _fix_angle(280.459 + 0.98564736 * d)
    lon_ecl = math.radians(_fix_angle(q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)))
    obliq = math.radians(23.439 - 0.00000036 * d)

    declination = math.degrees(math.asin(math.sin(obliq) * math.sin(lon_ecl)))
    right_asc = _fix_angle(math.degrees(math.atan2(
        math.cos(obliq) * math.sin(lon_ecl), math.cos(lon_ecl)))) / 15.0
    equation = q / 15.0 - right_asc
    hours = (when.hour + when.minute / 60.0 + when.second / 3600.0)
    subsolar_lon = -15.0 * (hours + equation - 12.0)
    return declination, ((subsolar_lon + 540.0) % 360.0) - 180.0


# ---------------------------------------------------------------------------
# Live data — every fetch is optional, and every failure degrades to silence
# rather than to an error. A globe without earthquakes is still a globe.
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(url, headers={
        "User-Agent": "JARVIS-earth3d/1.0 (+local assistant)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_quakes(window: str = "all_day", minimum: float = 2.5,
                 limit: int = 220) -> list[dict]:
    """
    Today's earthquakes from the USGS, newest first, largest first.

    Reading the feed only — no map tile is ever requested, so nothing here
    depends on a paid service or an API key.
    """
    try:
        payload = _get_json(_QUAKE_FEED.format(window=window))
    except Exception:
        return []
    out: list[dict] = []
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        magnitude = props.get("mag")
        if magnitude is None or len(coords) < 3 or float(magnitude) < minimum:
            continue
        out.append({
            "lat": round(float(coords[1]), 2),
            "lon": round(float(coords[0]), 2),
            "depth": round(float(coords[2]), 1),
            "mag": round(float(magnitude), 1),
            "place": str(props.get("place") or "unknown region")[:60],
        })
    out.sort(key=lambda q: q["mag"], reverse=True)
    return out[:limit]


def fetch_iss() -> dict | None:
    """Where the space station is, from whichever feed answers first."""
    for feed in _ISS_FEEDS:
        try:
            payload = _get_json(feed)
        except Exception:
            continue
        if "latitude" in payload:
            return {"lat": round(float(payload["latitude"]), 3),
                    "lon": round(float(payload["longitude"]), 3),
                    "alt_km": round(float(payload.get("altitude") or 0), 1),
                    "velocity_kmh": round(float(payload.get("velocity") or 0), 0)}
        block = payload.get("iss_position")
        if isinstance(block, dict):
            return {"lat": round(float(block["latitude"]), 3),
                    "lon": round(float(block["longitude"]), 3),
                    "alt_km": 418.0, "velocity_kmh": 27600.0}
    return None


# ---------------------------------------------------------------------------
# Where the file goes, and the settings
# ---------------------------------------------------------------------------

def _base_dir() -> Path:
    try:
        from memory.config_manager import BASE_DIR
        return Path(BASE_DIR)
    except Exception:
        return Path(__file__).resolve().parent.parent


def _output_dir() -> Path:
    folder = _base_dir() / "config" / ".plugin_cache" / "globe"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _config() -> dict:
    values = {"auto_spin": True, "show_quakes": True, "show_iss": True,
              "home_lat": "31.04", "home_lon": "31.38", "open_browser": True}
    try:
        from memory.config_manager import get_plugin_config
        for key, value in (get_plugin_config("earth_3d") or {}).items():
            if value not in (None, ""):
                values[key] = value
    except Exception:
        pass
    return values


def _coastlines() -> list[list[float]]:
    """
    Unpack the embedded outline into rings of flat [lon, lat, lon, lat, …].

    Reading the blob is 25 KB of zlib — under a millisecond — and it happens on
    every call rather than at import, so plugin discovery stays instant.
    """
    text = zlib.decompress(base64.b64decode(COASTLINE_DATA)).decode("ascii")
    rings: list[list[float]] = []
    for ring in text.split(";"):
        values = [float(v) for v in ring.split(",")]
        if len(values) >= 8:
            rings.append(values)
    return rings


def _resolve_place(name: str, home: tuple[float, float]) -> tuple[float, float, str]:
    """Spoken place name → (lat, lon, label). Home coordinates when unsure."""
    wanted = str(name or "").strip().lower()
    if not wanted:
        return home[0], home[1], "Home"
    if wanted in PLACES:
        lat, lon = PLACES[wanted]
        return lat, lon, str(name).strip().title()
    for key, (lat, lon) in PLACES.items():          # "cairo, egypt" → "cairo"
        if key in wanted or wanted in key:
            return lat, lon, key.title()
    return home[0], home[1], f"{home[0]:.2f}, {home[1]:.2f}"


# ---------------------------------------------------------------------------
# The page. One self-contained HTML file: the outline, the renderer, the data
# and the controls all live inside it, so it opens from the disk with no server,
# no internet and no local file permissions to fight with.
# ---------------------------------------------------------------------------

_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JARVIS · EARTH — __SUBTITLE__</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%;background:#000407;overflow:hidden;
    font-family:'Courier New',ui-monospace,monospace;color:#8fdcff}
  #stage{position:fixed;inset:0}
  canvas{display:block;width:100%;height:100%;cursor:grab;touch-action:none}
  canvas.dragging{cursor:grabbing}
  .hud{position:fixed;pointer-events:none;font-size:11px;letter-spacing:.13em;
    text-transform:uppercase;text-shadow:0 0 8px #46e0ff88}
  #top{top:16px;left:20px;font-size:13px;color:#46e0ff;letter-spacing:.3em}
  #top small{display:block;font-size:9px;color:#2f7f9c;letter-spacing:.22em;margin-top:3px}
  #read{top:16px;right:20px;text-align:right;line-height:1.85;color:#5fb8d8}
  #read b{color:#9fe8ff;font-weight:400}
  #foot{bottom:14px;left:20px;color:#2f7f9c;line-height:1.7}
  #foot b{color:#46e0ff;font-weight:400}
  #panel{position:fixed;bottom:14px;right:16px;display:flex;gap:6px;flex-wrap:wrap;
    justify-content:flex-end;max-width:62vw}
  button{font:inherit;font-size:10px;letter-spacing:.12em;text-transform:uppercase;
    padding:7px 11px;background:#03141dcc;color:#4a94ad;border:1px solid #11445455;
    border-radius:2px;cursor:pointer;transition:.15s}
  button:hover{color:#9fe8ff;border-color:#46e0ff66}
  button.on{color:#031018;background:#46e0ff;border-color:#46e0ff;
    box-shadow:0 0 14px #46e0ff66}
  #legend{position:fixed;bottom:52px;right:16px;text-align:right;font-size:10px;
    color:#2f7f9c;line-height:1.8;letter-spacing:.08em;pointer-events:none}
  #legend i{font-style:normal;display:inline-block;width:8px;height:8px;border-radius:50%;
    margin-left:6px;vertical-align:middle}
</style>
</head>
<body>
<div id="stage"><canvas id="globe"></canvas></div>

<div class="hud" id="top">J.A.R.V.I.S · EARTH<small id="sub">__SUBTITLE__</small></div>
<div class="hud" id="read">
  <div>UTC <b id="utc">—</b></div>
  <div>VIEW <b id="rot">—</b></div>
  <div>SUBSOLAR <b id="sun">—</b></div>
  <div id="issline" hidden>ISS <b id="issbox">—</b></div>
</div>
<div class="hud" id="foot">
  <div>drag to spin · scroll to zoom · <b>double-click</b> to reset</div>
  <div id="qline" hidden>earthquakes shown: <b id="qbox">0</b> (USGS, last 24 h)</div>
</div>
<div id="legend" hidden></div>
<div id="panel"></div>

<script>
"use strict";
const DATA = __DATA__;

/* coastlines: one flat [lon,lat,lon,lat…] array per ring */
const COAST = DATA.coast.split(";").map(function (s) { return new Float32Array(s.split(",").map(Number)); });

const DEG = Math.PI / 180, RAD = 180 / Math.PI, TAU = Math.PI * 2;
const cv = document.getElementById("globe"), ctx = cv.getContext("2d");

/* The night shading is rendered small and scaled up. It is a soft gradient, so
   the upscale is invisible, and 65 000 pixels a frame costs nothing. */
const SHADE_N = 256;
const shadeCv = document.createElement("canvas");
shadeCv.width = shadeCv.height = SHADE_N;
const shadeCtx = shadeCv.getContext("2d");
const shadeImg = shadeCtx.createImageData(SHADE_N, SHADE_N);

let W = 0, H = 0, cx = 0, cy = 0, R = 0;
let rotLon = DATA.view.lon, rotLat = DATA.view.lat, zoom = DATA.view.zoom || 1;
let spin = !!DATA.spin, dragging = false, lastX = 0, lastY = 0;

const layers = Object.assign({coast: true, grid: true, night: true, cities: true,
                              quakes: false, iss: false}, DATA.layers || {});

/* ── geometry ─────────────────────────────────────────────────────────────── */
function proj(lon, lat) {                 /* orthographic — the maths is 3 lines */
  const l = (lon - rotLon) * DEG, f = lat * DEG, f0 = rotLat * DEG;
  const cf = Math.cos(f), sf = Math.sin(f), cf0 = Math.cos(f0), sf0 = Math.sin(f0);
  const cl = Math.cos(l), sl = Math.sin(l);
  return [cf * sl, cf0 * sf - sf0 * cf * cl, sf0 * sf + cf0 * cf * cl];
}
function toScreen(p) { return [cx + R * p[0], cy - R * p[1], p[2]]; }
function visibility(lat, lon) { return proj(lon, lat)[2]; }

/* The subsolar point drifts a quarter of a degree a minute, so it is recomputed
   live: leave the page open and the terminator genuinely moves. */
function subsolar(ms) {
  const d = ms / 86400000 + 2440587.5 - 2451545.0;
  const g = (357.529 + 0.98560028 * d) * DEG;
  const q = 280.459 + 0.98564736 * d;
  const L = (q + 1.915 * Math.sin(g) + 0.020 * Math.sin(2 * g)) * DEG;
  const e = (23.439 - 0.00000036 * d) * DEG;
  const dec = Math.asin(Math.sin(e) * Math.sin(L)) * RAD;
  const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L)) * RAD / 15;
  const hours = (ms / 3600000) % 24;
  let lon = -15 * (hours + (q / 15 - ra) - 12);
  return {lat: dec, lon: ((lon + 540) % 360 + 360) % 360 - 180};
}

function resize() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  W = cv.clientWidth; H = cv.clientHeight;
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  cx = W / 2; cy = H / 2;
  R = Math.min(W, H) * 0.40 * zoom;
}
function reset() {
  rotLon = DATA.view.lon; rotLat = DATA.view.lat; zoom = DATA.view.zoom || 1; resize();
}

/* ── drawing ──────────────────────────────────────────────────────────────── */
function drawOcean() {
  const g = ctx.createRadialGradient(cx - R * .30, cy - R * .35, R * .10, cx, cy, R * 1.05);
  g.addColorStop(0, "#0d354a");
  g.addColorStop(.55, "#07202e");
  g.addColorStop(1, "#02090f");
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, TAU); ctx.fillStyle = g; ctx.fill();

  ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, R, 0, TAU); ctx.clip();
  ctx.strokeStyle = "rgba(70,224,255,.05)"; ctx.lineWidth = R * .06;
  ctx.beginPath(); ctx.arc(cx - R * .35, cy - R * .38, R * .90, 0, TAU); ctx.stroke();
  ctx.restore();

  ctx.beginPath(); ctx.arc(cx, cy, R + .5, 0, TAU);
  ctx.strokeStyle = "rgba(70,224,255,.55)"; ctx.lineWidth = 1.2;
  ctx.shadowColor = "#46e0ff"; ctx.shadowBlur = 18; ctx.stroke(); ctx.shadowBlur = 0;
}

/* Every pixel of the disc is un-projected back to a latitude and longitude and
   asked one question: how high is the sun there? That is the entire day/night
   model, and it is why the terminator bends the way it really does. */
function drawNight(sub) {
  const d = shadeImg.data, N = SHADE_N, half = N / 2;
  const f0 = rotLat * DEG, cf0 = Math.cos(f0), sf0 = Math.sin(f0);
  const sl = Math.sin(sub.lat * DEG), cl = Math.cos(sub.lat * DEG);
  for (let j = 0; j < N; j++) {
    const y = (half - j - .5) / half;
    for (let i = 0; i < N; i++) {
      const o = (j * N + i) * 4;
      const x = (i - half + .5) / half, r2 = x * x + y * y;
      if (r2 > 1) { d[o + 3] = 0; continue; }
      const rho = Math.sqrt(r2);
      let lat, lon;
      if (rho < 1e-9) { lat = rotLat; lon = rotLon; }
      else {
        const c = Math.asin(Math.min(1, rho)), sc = Math.sin(c), cc = Math.cos(c);
        lat = Math.asin(cc * sf0 + (y * sc * cf0) / rho) * RAD;
        lon = rotLon + Math.atan2(x * sc, rho * cf0 * cc - y * sf0 * sc) * RAD;
      }
      const sinEl = Math.sin(lat * DEG) * sl +
                    Math.cos(lat * DEG) * cl * Math.cos((lon - sub.lon) * DEG);
      /* soft civil-twilight band instead of a hard edge */
      let a = (0.10 - sinEl) / 0.35;
      a = a < 0 ? 0 : a > 1 ? 1 : a;
      d[o] = 1; d[o + 1] = 6; d[o + 2] = 12; d[o + 3] = a * 218;
    }
  }
  shadeCtx.putImageData(shadeImg, 0, 0);
  ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, R, 0, TAU); ctx.clip();
  ctx.drawImage(shadeCv, cx - R, cy - R, 2 * R, 2 * R);
  ctx.restore();
}

function drawGrid() {
  ctx.strokeStyle = "rgba(70,190,230,.13)"; ctx.lineWidth = .7;
  ctx.beginPath();
  for (let lon = -180; lon < 180; lon += 30) {
    let started = false;
    for (let lat = -90; lat <= 90; lat += 3) {
      const p = proj(lon, lat);
      if (p[2] < 0) { started = false; continue; }
      const s = toScreen(p);
      if (started) ctx.lineTo(s[0], s[1]); else { ctx.moveTo(s[0], s[1]); started = true; }
    }
  }
  for (let lat = -60; lat <= 60; lat += 30) {
    let started = false;
    for (let lon = -180; lon <= 180; lon += 3) {
      const p = proj(lon, lat);
      if (p[2] < 0) { started = false; continue; }
      const s = toScreen(p);
      if (started) ctx.lineTo(s[0], s[1]); else { ctx.moveTo(s[0], s[1]); started = true; }
    }
  }
  ctx.stroke();
  ctx.strokeStyle = "rgba(70,224,255,.22)"; ctx.beginPath();
  let st = false;
  for (let lon = -180; lon <= 180; lon += 2) {
    const p = proj(lon, 0);
    if (p[2] < 0) { st = false; continue; }
    const s = toScreen(p);
    if (st) ctx.lineTo(s[0], s[1]); else { ctx.moveTo(s[0], s[1]); st = true; }
  }
  ctx.stroke();
}

function drawCoast() {
  ctx.save();
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, TAU); ctx.clip();
  ctx.lineJoin = "round"; ctx.lineCap = "round";
  ctx.strokeStyle = "#57e8ff"; ctx.lineWidth = Math.max(.55, R * .0035);
  ctx.shadowColor = "rgba(70,224,255,.85)"; ctx.shadowBlur = Math.max(2, R * .022);
  ctx.beginPath();
  for (let k = 0; k < COAST.length; k++) {
    const ring = COAST[k];
    let started = false;
    for (let i = 0; i < ring.length; i += 2) {
      const p = proj(ring[i], ring[i + 1]);
      if (p[2] < 0) { started = false; continue; }
      const s = toScreen(p);
      if (started) ctx.lineTo(s[0], s[1]); else { ctx.moveTo(s[0], s[1]); started = true; }
    }
  }
  ctx.stroke();
  ctx.restore();
}

function marker(lon, lat, color, size, glow) {
  const p = proj(lon, lat);
  if (p[2] < 0.02) return null;
  const s = toScreen(p);
  ctx.beginPath(); ctx.arc(s[0], s[1], size, 0, TAU);
  ctx.fillStyle = color; ctx.shadowColor = glow || color; ctx.shadowBlur = size * 3.2;
  ctx.fill(); ctx.shadowBlur = 0;
  return s;
}

function drawCities() {
  ctx.font = Math.max(8, R * .021) + "px 'Courier New',monospace";
  ctx.textAlign = "left"; ctx.textBaseline = "middle";
  for (let k = 0; k < DATA.cities.length; k++) {
    const lat = DATA.cities[k][0], lon = DATA.cities[k][1], name = DATA.cities[k][2];
    const s = marker(lon, lat, "#ffd23f", Math.max(1.4, R * .006), "#ffd23f");
    if (!s) continue;
    if (zoom > 1.35 || visibility(lat, lon) > .55) {
      ctx.fillStyle = "rgba(255,225,150,.72)";
      ctx.fillText(String(name).toUpperCase(), s[0] + R * .022, s[1] - R * .016);
    }
  }
}

function drawQuakes() {
  ctx.font = Math.max(8, R * .02) + "px 'Courier New',monospace";
  ctx.textAlign = "left"; ctx.textBaseline = "middle";
  for (let k = 0; k < DATA.quakes.length; k++) {
    const q = DATA.quakes[k];
    const size = Math.max(1.6, (q.mag - 2) * R * .0065);
    const heat = Math.min(1, (q.mag - 2.5) / 4);
    const color = "rgba(255," + Math.round(180 - heat * 150) + "," +
                  Math.round(70 - heat * 70) + ",.92)";
    const s = marker(q.lon, q.lat, color, size, "#ff7043");
    if (s && q.mag >= 5.2) {
      ctx.fillStyle = "rgba(255,190,140,.85)";
      ctx.fillText("M" + q.mag.toFixed(1), s[0] + R * .03, s[1] + R * .022);
    }
  }
}

function drawIss() {
  const i = DATA.iss; if (!i) return;
  const p = proj(i.lon, i.lat); if (p[2] < 0) return;
  const s = toScreen(p), r = Math.max(5, R * .022);
  ctx.save();
  ctx.strokeStyle = "#7dff9b"; ctx.lineWidth = 1.4;
  ctx.shadowColor = "#7dff9b"; ctx.shadowBlur = 12;
  ctx.beginPath(); ctx.arc(s[0], s[1], r, 0, TAU); ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(s[0] - r * 1.8, s[1]); ctx.lineTo(s[0] - r * .55, s[1]);
  ctx.moveTo(s[0] + r * .55, s[1]); ctx.lineTo(s[0] + r * 1.8, s[1]);
  ctx.moveTo(s[0], s[1] - r * 1.8); ctx.lineTo(s[0], s[1] - r * .55);
  ctx.moveTo(s[0], s[1] + r * .55); ctx.lineTo(s[0], s[1] + r * 1.8);
  ctx.stroke(); ctx.restore();
  ctx.font = Math.max(8, R * .021) + "px 'Courier New',monospace";
  ctx.fillStyle = "#a9ffc0"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText("ISS", s[0] + r * 1.5, s[1] - r * 1.5);
}

function drawCorners() {
  const m = 12, L = Math.min(W, H) * .07;
  ctx.strokeStyle = "rgba(70,224,255,.30)"; ctx.lineWidth = 1.4;
  const c = [[m, m, 1, 1], [W - m, m, -1, 1], [m, H - m, 1, -1], [W - m, H - m, -1, -1]];
  for (let k = 0; k < 4; k++) {
    const x = c[k][0], y = c[k][1], sx = c[k][2], sy = c[k][3];
    ctx.beginPath();
    ctx.moveTo(x + sx * L, y); ctx.lineTo(x, y); ctx.lineTo(x, y + sy * L);
    ctx.stroke();
  }
}

function fmt(v) {
  const d = Math.abs(v).toFixed(1);
  return (v >= 0 ? "+" : "-") + (d.length < 4 ? "0" + d : d);
}

function draw() {
  ctx.clearRect(0, 0, W, H);
  const sub = subsolar(Date.now());
  drawOcean();
  if (layers.night)  drawNight(sub);
  if (layers.grid)   drawGrid();
  if (layers.coast)  drawCoast();
  if (layers.quakes) drawQuakes();
  if (layers.iss)    drawIss();
  if (layers.cities) drawCities();
  drawCorners();
  document.getElementById("utc").textContent = new Date().toISOString().slice(11, 19);
  document.getElementById("rot").textContent = fmt(rotLat) + " " + fmt(rotLon) +
                                               " · " + zoom.toFixed(2) + "\u00d7";
  document.getElementById("sun").textContent = fmt(sub.lat) + " " + fmt(sub.lon);
}

let lastTs = 0;
function loop(ts) {
  const dt = lastTs ? Math.min(0.1, (ts - lastTs) / 1000) : 0;
  lastTs = ts;
  /* one revolution every ~100 s, identical on a 60 Hz and a 144 Hz screen */
  if (spin && !dragging) rotLon += 3.6 * dt;
  if (rotLon > 180) rotLon -= 360;
  draw();
  requestAnimationFrame(loop);
}

/* ── interaction ──────────────────────────────────────────────────────────── */
cv.addEventListener("pointerdown", function (e) {
  dragging = true; lastX = e.clientX; lastY = e.clientY;
  cv.classList.add("dragging");
  if (cv.setPointerCapture) cv.setPointerCapture(e.pointerId);
});
cv.addEventListener("pointermove", function (e) {
  if (!dragging) return;
  rotLon -= (e.clientX - lastX) * (0.28 / zoom);
  rotLat = Math.max(-89, Math.min(89, rotLat + (e.clientY - lastY) * (0.28 / zoom)));
  if (rotLon > 180) rotLon -= 360;
  if (rotLon < -180) rotLon += 360;
  lastX = e.clientX; lastY = e.clientY;
});
addEventListener("pointerup", function () { dragging = false; cv.classList.remove("dragging"); });
cv.addEventListener("wheel", function (e) {
  e.preventDefault();
  zoom = Math.max(0.55, Math.min(6, zoom * (e.deltaY < 0 ? 1.12 : 0.893)));
  resize();
}, {passive: false});
cv.addEventListener("dblclick", function () { spin = true; syncButtons(); reset(); });

const BUTTONS = [["grid", "Grid"], ["coast", "Coast"], ["night", "Night"],
                 ["cities", "Places"], ["quakes", "Quakes"], ["iss", "ISS"],
                 ["spin", "Spin"]];
const panel = document.getElementById("panel");
for (let k = 0; k < BUTTONS.length; k++) {
  (function (key, label) {
    const b = document.createElement("button");
    b.textContent = label; b.dataset.key = key;
    b.onclick = function () {
      if (key === "spin") spin = !spin; else layers[key] = !layers[key];
      syncButtons();
    };
    panel.appendChild(b);
  })(BUTTONS[k][0], BUTTONS[k][1]);
}

function syncButtons() {
  const kids = panel.children;
  for (let k = 0; k < kids.length; k++) {
    const key = kids[k].dataset.key;
    kids[k].classList.toggle("on", !!(key === "spin" ? spin : layers[key]));
  }
  document.getElementById("qline").hidden = !(layers.quakes && DATA.quakes.length);
  document.getElementById("qbox").textContent = DATA.quakes.length;
  document.getElementById("issline").hidden = !(layers.iss && DATA.iss);
  if (DATA.iss) {
    document.getElementById("issbox").textContent =
      fmt(DATA.iss.lat) + " " + fmt(DATA.iss.lon) + " · " + DATA.iss.alt_km + " km";
  }
  const rows = [];
  if (layers.cities) rows.push('<div>major cities<i style="background:#ffd23f"></i></div>');
  if (layers.quakes && DATA.quakes.length)
    rows.push('<div>earthquakes M2.5+<i style="background:#ff7043"></i></div>');
  if (layers.iss && DATA.iss)
    rows.push('<div>space station<i style="background:#7dff9b"></i></div>');
  const legend = document.getElementById("legend");
  legend.innerHTML = rows.join(""); legend.hidden = !rows.length;
}

addEventListener("resize", resize);
resize(); syncButtons();
requestAnimationFrame(loop);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Build, open, and answer
# ---------------------------------------------------------------------------

def _number(value, fallback: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _render_page(payload: dict, subtitle: str) -> str:
    """
    Fill the placeholders. Plain replacement, not str.format — the page is full
    of CSS braces and a single stray pair would break the whole file.
    """
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    # These two characters are legal JSON but illegal inside a <script> block.
    data = data.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    page = _PAGE.replace("__DATA__", data)
    page = page.replace("__SUBTITLE__", subtitle.replace("<", "&lt;"))
    return page


def _spoken_summary(place: str, quakes: list[dict], iss: dict | None,
                    config: dict) -> str:
    parts = [f"The globe is open in your browser, sir — centred on {place}."]
    if quakes:
        strongest = quakes[0]
        parts.append(f"{len(quakes)} earthquakes in the last day, the strongest "
                     f"magnitude {strongest['mag']:.1f} near {strongest['place']}.")
    if iss:
        parts.append(f"The station is over {iss['lat']:.1f}, {iss['lon']:.1f} "
                     f"at {iss['alt_km']:.0f} kilometres.")
    parts.append("Drag to spin it, scroll to zoom.")
    return " ".join(parts)


def build(parameters: dict, config: dict | None = None) -> tuple[Path, str, dict]:
    """
    Render the page and return (path, spoken summary, payload).

    Split out from run() so a test — or a future tool — can produce the file
    without opening a browser or being inside the app.
    """
    config = config or _config()
    parameters = parameters or {}
    action = str(parameters.get("action", "show") or "show").strip().lower()

    home_lat = _number(config.get("home_lat"), 31.04)
    home_lon = _number(config.get("home_lon"), 31.38)
    lat, lon, label = _resolve_place(parameters.get("place", ""), (home_lat, home_lon))

    want_quakes = action in ("quakes", "all") or (
        action in ("show", "focus", "") and bool(config.get("show_quakes", True)))
    want_iss = action in ("iss", "all") or (
        action in ("show", "focus", "") and bool(config.get("show_iss", True)))
    if action == "quakes":
        want_iss = False
    if action == "iss":
        want_quakes = False

    quakes = fetch_quakes() if want_quakes else []
    iss = fetch_iss() if want_iss else None

    zoom = max(0.55, min(6.0, _number(parameters.get("zoom"), 1.0)))
    if str(parameters.get("place", "")).strip() and "zoom" not in parameters:
        zoom = 1.8                       # a named place deserves to be seen

    now = _dt.datetime.now(_dt.timezone.utc)
    sun_lat, sun_lon = subsolar_point(now)
    payload = {
        "coast": _coast_text(),
        "view": {"lat": lat, "lon": lon, "zoom": zoom},
        "spin": bool(config.get("auto_spin", True)),
        "layers": {"quakes": bool(quakes), "iss": bool(iss)},
        "cities": [[c[0], c[1], c[2]] for c in CITIES],
        "quakes": quakes,
        "iss": iss,
    }
    subtitle = (f"{label} · {now.strftime('%d %b %Y')} · "
                f"sun over {sun_lat:+.1f}°, {sun_lon:+.1f}°")

    path = _output_dir() / "earth.html"
    path.write_text(_render_page(payload, subtitle), encoding="utf-8")
    return path, _spoken_summary(label, quakes, iss, config), payload


def _coast_text() -> str:
    """The outline as the plain text the page parses. Cached after the first call."""
    global _COAST_TEXT
    if _COAST_TEXT is None:
        _COAST_TEXT = zlib.decompress(base64.b64decode(COASTLINE_DATA)).decode("ascii")
    return _COAST_TEXT


_COAST_TEXT: str | None = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    try:
        config = _config()
        path, summary, payload = build(parameters, config)
        if config.get("open_browser", True):
            try:
                webbrowser.open(path.as_uri())
            except Exception:
                summary += f" I could not start the browser — the file is at {path}."
        if player:
            try:
                player.write_log(f"JARVIS: {summary}")
            except Exception:
                pass
        return summary
    except Exception as exc:
        return f"Sir, earth_3d failed: {exc}"


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if "--dump-coast" in args:
        text = _coast_text()
        rings = text.split(";")
        print(f"{len(rings)} rings, {len(text.split(',')) // 2} points")
        print(text[:400], "…")
        sys.exit(0)
    opened = {
        "action": args[0] if args else "show",
        "place": args[1] if len(args) > 1 else "",
    }
    cfg = _config()
    cfg["open_browser"] = False          # never launch a browser from a test run
    path, summary, payload = build(opened, cfg)
    print(f"file:    {path}  ({path.stat().st_size / 1024:.0f} KB)")
    print(f"quakes:  {len(payload['quakes'])}   iss: {payload['iss']}")
    print(f"spoken:  {summary}")
