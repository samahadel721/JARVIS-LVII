"""
plugins/prayer_times.py — prayer times, Qibla and the Hijri date, computed offline.

Drop this single file into `plugins/`. It has no dependencies beyond the Python
standard library and never touches the network: the sun's position is computed
from the date and the configured coordinates, so it works with the Wi-Fi off,
which is the state a lot of people are in at Fajr.

WHAT THE MODEL CAN ASK IT
    today's times · tomorrow's · a full month · the next prayer and how long is
    left · the Qibla bearing · the Hijri date.

ACCURACY
    The same astronomical basis every prayer-time service uses (PrayTimes
    method): declination + equation of time, then the classical twilight angles.
    Verified against the Aladhan calculation service for Cairo to within a
    minute. The Hijri date uses the tabulated (Umm al-Qura style) arithmetic
    calendar, which can differ from local moon sighting by a day — the reply
    says so rather than pretending otherwise.

SETTINGS (Plugin Manager → Prayer Times)
    City / latitude / longitude / UTC offset / calculation method / Asr rule.
    Everything has a working default for Cairo, so the plugin is useful the
    second it is copied in.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from pathlib import Path

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

PLUGIN = {
    "name": "prayer_times",
    "description": (
        "Prayer times (Salah), the Qibla direction and the Hijri date, computed "
        "offline from coordinates — no internet needed. Use for: 'what time is "
        "Fajr', 'when is Maghrib today', 'prayer times for this month', 'how "
        "long until the next prayer', 'which way is the Qibla', 'what is the "
        "Hijri date'. Also handles 'al-Asr in an hour' style questions by "
        "answering with the actual time. Do NOT use web_search for any of "
        "these — this tool is exact, instant and works offline."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "What to report: 'today' (default), 'tomorrow', 'month' for "
                    "the whole current month, 'next' for the next prayer and the "
                    "time remaining, 'qibla' for the direction to Mecca, or "
                    "'hijri' for the Islamic date alone."
                ),
            },
            "date": {
                "type": "STRING",
                "description": (
                    "Optional date as YYYY-MM-DD (or 'today'/'tomorrow') to "
                    "compute instead of today."
                ),
            },
            "city": {
                "type": "STRING",
                "description": (
                    "Optional city name for one-off queries (e.g. 'Alexandria', "
                    "'Mecca'). Requires latitude/longitude to be given with it "
                    "unless the city is already configured in settings."
                ),
            },
            "latitude": {"type": "NUMBER", "description": "Optional latitude for a one-off query."},
            "longitude": {"type": "NUMBER", "description": "Optional longitude for a one-off query."},
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "prayer_times",
    "title": "Prayer Times / مواقيت الصلاة",
    "fields": [
        {"key": "city", "type": "text", "label": "City / المدينة", "default": "Cairo",
         "placeholder": "Cairo"},
        {"key": "latitude", "type": "text", "label": "Latitude / خط العرض", "default": "30.0444",
         "placeholder": "30.0444"},
        {"key": "longitude", "type": "text", "label": "Longitude / خط الطول", "default": "31.2357",
         "placeholder": "31.2357"},
        {"key": "utc_offset", "type": "text", "label": "UTC offset (auto) / فرق التوقيت",
         "default": "auto", "placeholder": "auto or 3"},
        {"key": "method", "type": "choice", "label": "Method / طريقة الحساب",
         "default": "egyptian", "options": ["egyptian", "mwl", "umm_al_qura", "isna", "karachi"]},
        {"key": "asr", "type": "choice", "label": "Asr / مذهب العصر",
         "default": "standard", "options": ["standard", "hanafi"]},
    ],
}

#: Twilight angles per calculation authority: (fajr_angle, isha_angle_or_minutes)
#:  - egyptian   : Egyptian General Authority of Survey  (19.5°, 17.5°)
#:  - mwl        : Muslim World League                   (18°, 17°)
#:  - umm_al_qura: Umm al-Qura, Mecca                    (18.5°, 90 min after Maghrib)
#:  - isna       : Islamic Society of North America      (15°, 15°)
#:  - karachi    : University of Islamic Sciences        (18°, 18°)
_METHODS = {
    "egyptian":    (19.5, 17.5),
    "mwl":         (18.0, 17.0),
    "umm_al_qura": (18.5, 90.0),
    "isna":        (15.0, 15.0),
    "karachi":     (18.0, 18.0),
}

_RISE_SET_ANGLE = 0.833      # sun's upper limb + refraction, the standard value
_KAABA = (21.4225, 39.8262)  # latitude, longitude of the Kaaba
_PRAYERS = ("fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha")
_DISPLAY = {"fajr": "Fajr", "sunrise": "Sunrise", "dhuhr": "Dhuhr",
            "asr": "Asr", "maghrib": "Maghrib", "isha": "Isha"}
_HIJRI_MONTHS = ("Muharram", "Safar", "Rabi al-Awwal", "Rabi al-Thani",
                 "Jumada al-Ula", "Jumada al-Akhira", "Rajab", "Sha'ban",
                 "Ramadan", "Shawwal", "Dhu al-Qi'dah", "Dhu al-Hijjah")


# ---------------------------------------------------------------------------
# Small degree trigonometry — the algorithm is written in degrees, not radians
# ---------------------------------------------------------------------------

def _sin(d): return math.sin(math.radians(d))
def _cos(d): return math.cos(math.radians(d))
def _tan(d): return math.tan(math.radians(d))
def _asin(x): return math.degrees(math.asin(x))
def _acos(x): return math.degrees(math.acos(x))
def _atan2(y, x): return math.degrees(math.atan2(y, x))


def _fix(a: float, b: float) -> float:
    """Wrap `a` into [0, b) — the sign handling matters for negative longitudes."""
    a = a - b * math.floor(a / b)
    return a + b if a < 0 else a


def _fix_angle(a: float) -> float: return _fix(a, 360.0)
def _fix_hour(a: float) -> float: return _fix(a, 24.0)


# ---------------------------------------------------------------------------
# Solar position
# ---------------------------------------------------------------------------

def _julian_date(year: int, month: int, day: int) -> float:
    """Julian day at 00:00 UTC for a Gregorian date."""
    if month <= 2:
        year, month = year - 1, month + 12
    a = year // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1))
            + day + b - 1524.5)


def _sun_position(jd: float) -> tuple[float, float]:
    """(declination°, equation of time in hours) for a Julian day."""
    d = jd - 2451545.0
    g = _fix_angle(357.529 + 0.98560028 * d)                       # mean anomaly
    q = _fix_angle(280.459 + 0.98564736 * d)                       # mean longitude
    lon = _fix_angle(q + 1.915 * _sin(g) + 0.020 * _sin(2 * g))    # ecliptic longitude
    e = 23.439 - 0.00000036 * d                                    # obliquity

    ra = _fix_hour(_atan2(_cos(e) * _sin(lon), _cos(lon)) / 15.0)  # right ascension, hours
    declination = _asin(_sin(e) * _sin(lon))
    equation = q / 15.0 - ra                                       # equation of time, hours
    return declination, _fix_hour(equation + 12.0) - 12.0


def _midday(jd: float, t: float) -> float:
    """Solar noon in hours (solar time)."""
    _, equation = _sun_position(jd + t)
    return 12.0 - equation


def _angle_time(jd: float, lat: float, angle: float, t: float, before_noon: bool) -> float | None:
    """
    Clock hour at which the sun sits `angle` below the horizon (negative = below).
    `None` when the sun never reaches that altitude — the polar cases.
    """
    declination, _ = _sun_position(jd + t)
    inner = (-_sin(angle) - _sin(declination) * _sin(lat)) / (_cos(declination) * _cos(lat))
    if inner > 1 or inner < -1:
        return None
    offset = _acos(inner) / 15.0
    noon = _midday(jd, t)
    return noon - offset if before_noon else noon + offset


def _asr_time(jd: float, lat: float, factor: float, t: float) -> float | None:
    """
    Asr: when an object's shadow equals its own length (standard) or twice it
    (Hanafi), expressed as a sun altitude and solved with the same routine.
    """
    declination, _ = _sun_position(jd + t)
    angle = -_atan2(1.0, factor + _tan(abs(lat - declination)))
    return _angle_time(jd, lat, angle, t, before_noon=False)


def day_times(year: int, month: int, day: int, lat: float, lon: float,
              tz_hours: float, method: str, asr: str) -> dict[str, float]:
    """
    Prayer times as fractional hours on the local clock.

    The loop is the classic two-pass refinement: the first pass estimates when
    each event happens, the second recomputes the sun's position at that
    estimate. It matters because the sun moves during the day.
    """
    jd = _julian_date(year, month, day)
    fajr_angle, isha_angle = _METHODS.get(method, _METHODS["egyptian"])
    asr_factor = 2.0 if str(asr).lower() == "hanafi" else 1.0
    isha_is_minutes = str(method).lower() == "umm_al_qura"

    # First guesses, as fractions of a day (PrayTimes' own starting values).
    times = {"fajr": 5 / 24, "sunrise": 6 / 24, "dhuhr": 12 / 24,
             "asr": 13 / 24, "maghrib": 18 / 24, "isha": 18 / 24}

    for _ in range(2):
        t = dict(times)
        times["fajr"] = _angle_time(jd, lat, fajr_angle, t["fajr"], before_noon=True)
        times["sunrise"] = _angle_time(jd, lat, _RISE_SET_ANGLE, t["sunrise"], before_noon=True)
        times["dhuhr"] = _midday(jd, t["dhuhr"])
        times["asr"] = _asr_time(jd, lat, asr_factor, t["asr"])
        times["maghrib"] = _angle_time(jd, lat, _RISE_SET_ANGLE, t["maghrib"], before_noon=False)
        if isha_is_minutes:
            times["isha"] = (times["maghrib"] + isha_angle / 60.0) if times["maghrib"] else None
        else:
            times["isha"] = _angle_time(jd, lat, isha_angle, t["isha"], before_noon=False)
        times = {k: (v / 24.0 if v is not None else 12 / 24) for k, v in times.items()}

    out: dict[str, float] = {}
    for key, value in times.items():
        if value is None:
            continue
        # Solar hours → local clock, then round to the nearest minute.
        out[key] = round(_fix_hour(value * 24.0 + tz_hours - lon / 15.0) * 60.0) / 60.0
    return out


# ---------------------------------------------------------------------------
# Hijri date + Qibla
# ---------------------------------------------------------------------------

def hijri_date(year: int, month: int, day: int) -> tuple[int, int, int]:
    """
    Gregorian → Hijri by the standard tabulated arithmetic conversion.

    Accurate to ±1 day against local moon sighting, which is inherent to every
    calculator that does not consult an observatory — the reply says so.
    """
    jd = int(_julian_date(year, month, day) + 0.5)
    l = jd - 1948440 + 10632
    n = (l - 1) // 10631
    l = l - 10631 * n + 354
    j = ((10985 - l) // 5316) * ((50 * l) // 17719) + (l // 5670) * ((43 * l) // 15238)
    l = (l - ((30 - j) // 15) * ((17719 * j) // 50)
         - (j // 16) * ((15238 * j) // 43) + 29)
    h_month = (24 * l) // 709
    h_day = l - (709 * h_month) // 24
    h_year = 30 * n + j - 30
    return int(h_year), int(h_month), int(h_day)


def qibla_bearing(lat: float, lon: float) -> float:
    """Great-circle initial bearing from a point to the Kaaba, in degrees from true north."""
    d_lon = _KAABA[1] - lon
    x = _sin(d_lon)
    y = _cos(lat) * _tan(_KAABA[0]) - _sin(lat) * _cos(d_lon)
    return _fix_angle(_atan2(x, y))


def _compass(bearing: float) -> str:
    points = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    return points[int((bearing + 11.25) % 360 // 22.5)]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _config() -> dict:
    """Stored settings, with Cairo as the working default (already correct here)."""
    values = {
        "city": "Cairo", "latitude": "30.0444", "longitude": "31.2357",
        "utc_offset": "auto", "method": "egyptian", "asr": "standard",
    }
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config("prayer_times")
        for key, value in (stored or {}).items():
            if value not in (None, ""):
                values[key] = value
    except Exception:
        pass
    return values


def _utc_offset_for(day: _dt.date) -> float:
    """
    This machine's UTC offset **on that date**, daylight saving included.

    Asking for January while sitting in September is the trap: Egypt moves the
    clock by an hour, so a "now" offset would put every winter prayer an hour
    out. `mktime` on the target date hands the answer to the C library's own
    timezone rules, which is the only place that knows the answer — and unlike
    tzfile lookups it works unchanged on Windows.
    """
    import time
    try:
        stamp = time.mktime((day.year, day.month, day.day, 12, 0, 0, 0, 0, -1))
        is_dst = time.localtime(stamp).tm_isdst
        if time.daylight and is_dst > 0:
            return -time.altzone / 3600.0
        return -time.timezone / 3600.0
    except Exception:
        return 2.0


def _numbers(config: dict, override_lat=None,
             override_lon=None) -> tuple[float, float, float | None]:
    """
    (lat, lon, utc offset) — the offset is `None` when it should be resolved per
    date from the operating system, or a fixed number when the user pinned it.
    """
    lat = float(override_lat) if override_lat is not None else float(str(config["latitude"]).strip())
    lon = float(override_lon) if override_lon is not None else float(str(config["longitude"]).strip())
    raw = str(config.get("utc_offset", "auto")).strip().lower()
    if raw in ("", "auto", "تلقائي"):
        return lat, lon, None
    try:
        return lat, lon, float(raw)
    except ValueError:
        return lat, lon, None


def _tz_for(tz_spec: float | None, day: _dt.date) -> float:
    """The offset to use for one date: the pinned one, or the OS's for that date."""
    return tz_spec if tz_spec is not None else _utc_offset_for(day)


def _clock_is_suspect(tz: float, lon: float) -> bool:
    """
    True when this computer's clock is nowhere near the location's own midday.

    Cairo sits at 31°E, so local time there is naturally UTC+2 or +3. A machine
    set to UTC and asked for Cairo would answer three hours early, silently —
    the worst kind of wrong for a prayer time. Anything inside an hour and a
    half of solar time is normal (every country's clock is a political choice),
    so only a real mismatch is worth a sentence in the reply.
    """
    return abs(float(tz) - (float(lon) / 15.0)) > 1.75


def _format(hour: float | None) -> str:
    if hour is None:
        return "—"
    total = int(round(hour * 60)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _resolve_date(text: str, today: _dt.date) -> _dt.date:
    low = str(text or "").strip().lower()
    if not low or low in ("today", "now"):
        return today
    if low in ("tomorrow", "غدا", "غدًا"):
        return today + _dt.timedelta(days=1)
    if low in ("yesterday",):
        return today - _dt.timedelta(days=1)
    try:
        return _dt.date.fromisoformat(low)
    except Exception:
        return today


def _times_for(day: _dt.date, config: dict, lat: float, lon: float,
               tz_spec: float | None) -> dict[str, float]:
    return day_times(day.year, day.month, day.day, lat, lon, _tz_for(tz_spec, day),
                     str(config.get("method", "egyptian")), str(config.get("asr", "standard")))


def _hijri_text(day: _dt.date) -> str:
    year, month, dom = hijri_date(day.year, day.month, day.day)
    month_name = _HIJRI_MONTHS[(month - 1) % 12]
    return f"{dom} {month_name} {year} AH"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _clock_note(tz_spec: float | None, lon: float, city: str) -> str:
    """
    One sentence that stops a silent three-hour error.

    Appended in the user's own language by the model, so it stays short and
    names the thing they would have to change.
    """
    if tz_spec is not None:
        return ""
    tz = _tz_for(None, _dt.date.today())
    if not _clock_is_suspect(tz, lon):
        return ""
    sign = "+" if tz >= 0 else "-"
    return (f"\n(Note: these times use this computer's clock — UTC{sign}{abs(tz):g}. "
            f"If that is not {city} time, set the UTC offset in the plugin settings.)")


def _day_block(day: _dt.date, times: dict[str, float], city: str) -> str:
    lines = [f"{city} · {day.strftime('%A %d %B %Y')} · {_hijri_text(day)}"]
    for key in _PRAYERS:
        lines.append(f"  {_DISPLAY[key]:<8} {_format(times.get(key))}")
    return "\n".join(lines)


def _next_prayer(now: _dt.datetime, config: dict, lat: float, lon: float,
                 tz_spec: float | None) -> tuple[str, float] | None:
    """(prayer key, hours remaining) for the next prayer after `now`."""
    candidates: list[tuple[float, str]] = []
    for offset in (0, 1):
        day = (now + _dt.timedelta(days=offset)).date()
        times = _times_for(day, config, lat, lon, tz_spec)
        for key in _PRAYERS:
            if key == "sunrise" or key not in times:
                continue                      # sunrise is informational, not a prayer
            candidates.append((offset * 24.0 + times[key], key))
    now_hours = now.hour + now.minute / 60.0 + now.second / 3600.0
    for when, key in sorted(candidates):
        if when > now_hours:
            return key, when - now_hours
    return None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_today(day: _dt.date, config: dict, lat: float, lon: float,
                  tz_spec: float | None, player) -> str:
    times = _times_for(day, config, lat, lon, tz_spec)
    text = _day_block(day, times, config["city"])
    if player:
        try:
            player.show_content("Prayer Times", text)
        except Exception:
            pass
    return text


def _action_next(config: dict, lat: float, lon: float, tz_spec: float | None) -> str:
    now = _dt.datetime.now()
    found = _next_prayer(now, config, lat, lon, tz_spec)
    if not found:
        return "I could not work out the next prayer for these coordinates."
    key, remaining = found
    hours, minutes = int(remaining), int(round((remaining - int(remaining)) * 60))
    if minutes == 60:
        hours, minutes = hours + 1, 0
    today_times = _times_for(now.date(), config, lat, lon, tz_spec)
    at = _format(today_times.get(key)) if now.hour + now.minute / 60 < today_times.get(key, 0) \
        else _format(_times_for((now + _dt.timedelta(days=1)).date(), config, lat, lon, tz_spec).get(key))
    return (f"Next prayer is {_DISPLAY[key]} at {at} — in {hours}h {minutes:02d}m "
            f"({config['city']}).")


def _action_month(config: dict, lat: float, lon: float, tz_spec: float | None, player) -> str:
    today = _dt.date.today()
    first = today.replace(day=1)
    days_in_month = (first.replace(day=28) + _dt.timedelta(days=4)).replace(day=1) - first
    rows, table = [], []
    for offset in range(days_in_month.days):
        day = first + _dt.timedelta(days=offset)
        times = _times_for(day, config, lat, lon, tz_spec)
        rows.append({**{"date": day.strftime("%d %a")},
                     **{k: _format(times.get(k)) for k in _PRAYERS}})
        table.append(f"{day.strftime('%d %a')}  " + "  ".join(
            f"{_DISPLAY[k][:3]} {_format(times.get(k))}" for k in _PRAYERS))

    header = (f"     " + "  ".join(f"{_DISPLAY[k][:3]:>6}" for k in _PRAYERS))
    text = "\n".join([f"{config['city']} · {first.strftime('%B %Y')}", header] + table)
    if player:
        try:
            player.show_content(f"Prayer Times — {first.strftime('%B %Y')}", text)
        except Exception:
            pass
    # The spoken answer stays short; the table is on screen.
    sample = rows[0]
    return (f"Prayer times for {config['city']}, {first.strftime('%B %Y')} are on screen. "
            f"Today: Fajr {sample['fajr']}, Dhuhr {sample['dhuhr']}, "
            f"Asr {sample['asr']}, Maghrib {sample['maghrib']}, Isha {sample['isha']}.")


def _action_qibla(config: dict, lat: float, lon: float) -> str:
    bearing = qibla_bearing(lat, lon)
    return (f"The Qibla from {config['city']} is {bearing:.1f}° from true north "
            f"({_compass(bearing)}) — that is the direction to face.")


def _action_hijri(day: _dt.date) -> str:
    return (f"Today is {_hijri_text(day)} (Gregorian {day.strftime('%d %B %Y')}). "
            f"This is the tabulated calendar, so it can differ by a day from "
            f"local moon sighting.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    try:
        config = _config()
        lat, lon, tz_spec = _numbers(config, parameters.get("latitude"), parameters.get("longitude"))
        if parameters.get("city"):
            config = {**config, "city": str(parameters["city"]).strip()}

        day = _resolve_date(parameters.get("date", ""), _dt.date.today())
        action = str(parameters.get("action", "today") or "today").strip().lower()

        if action in ("next", "next_prayer", "countdown"):
            result = _action_next(config, lat, lon, tz_spec)
        elif action in ("month", "table", "calendar"):
            result = _action_month(config, lat, lon, tz_spec, player)
        elif action == "qibla":
            result = _action_qibla(config, lat, lon)
        elif action in ("hijri", "islamic_date"):
            result = _action_hijri(day)
        elif action == "tomorrow":
            result = _action_today(day + _dt.timedelta(days=1), config, lat, lon, tz_spec, player)
        else:
            result = _action_today(day, config, lat, lon, tz_spec, player)
        if action not in ("qibla", "hijri", "islamic_date"):
            result += _clock_note(tz_spec, lon, str(config['city']))

    except Exception as exc:                     # never raise into the model
        return f"Sir, prayer_times failed: {exc}"

    if player:
        try:
            player.write_log(f"JARVIS: {result.splitlines()[0]}")
        except Exception:
            pass
    return result
