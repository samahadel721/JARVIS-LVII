"""
plugins/habit_tracker.py — daily habits, streaks and a week at a glance.

Drop this single file into `plugins/`. Standard library only, no account, no
cloud: habits live in a small JSON file inside the app's own config folder, and
they survive restarts and app updates because they never leave the machine.

WHY NOT THE `reminder` TOOL
    `reminder` fires a one-off OS notification. A habit is a count that repeats
    and accumulates — the value is in the streak, not in the ping. This plugin
    owns the counting side and leaves reminding to the tool that already does it.

SPOKEN ANSWERS STAY SHORT
    "Four of eight glasses, five-day streak" is a sentence. The full week goes
    to the screen through player.show_content, which is where a grid belongs.

SETTINGS (Plugin Manager → Habit Tracker)
    The habit list ('water:8, walk:1, reading:1'), the default goal, and the
    unit word used when a habit has none.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

PLUGIN = {
    "name": "habit_tracker",
    "description": (
        "Keeps count of daily habits and reports streaks: water, walking, "
        "reading, prayer, exercise, anything countable. Use for: 'I drank "
        "another glass of water', 'log 30 minutes of reading', 'add two cups "
        "of coffee', 'how am I doing on my habits', 'what is my streak', "
        "'show me this week', 'how much water today'. Counts are cumulative for "
        "the day and can be corrected. Do NOT use reminder for this — this tool "
        "is the record, not the alarm."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'log' (default) to add to today's count, 'status' for all "
                    "habits today with streaks, 'week' for the last seven days, "
                    "'add' or 'remove' to change the habit list, or 'reset' to "
                    "clear one habit's count for today."
                ),
            },
            "habit": {
                "type": "STRING",
                "description": "Which habit, e.g. 'water', 'reading', 'exercise'.",
            },
            "count": {
                "type": "INTEGER",
                "description": "How much to add for 'log' (default 1). For 'reset', the count becomes 0.",
            },
            "date": {
                "type": "STRING",
                "description": "Optional YYYY-MM-DD to log against (default today).",
            },
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "habit_tracker",
    "title": "Habit Tracker / متابعة العادات",
    "fields": [
        {"key": "habits", "type": "text", "label": "Habits (name:goal, …) / العادات",
         "default": "water:8, walk:1, reading:1",
         "placeholder": "water:8, walk:1, reading:1"},
        {"key": "default_goal", "type": "text", "label": "Default goal / الهدف الافتراضي",
         "default": "1"},
        {"key": "default_unit", "type": "text", "label": "Default unit / الوحدة",
         "default": "times", "placeholder": "glasses, minutes, pages…"},
    ],
}

#: Words people actually say, mapped onto the stored habit name.
_ALIASES = {
    "water": "water", "ماء": "water", "مياه": "water", "ميّة": "water",
    "walk": "walk", "walking": "walk", "مشي": "walk",
    "reading": "reading", "read": "reading", "قراءة": "reading",
    "exercise": "exercise", "workout": "exercise", "رياضة": "exercise",
    "prayer": "prayer", "صلاة": "prayer", "قرآن": "quran", "quran": "quran",
    "sleep": "sleep", "نوم": "sleep", "study": "study", "مذاكرة": "study",
}

_UNITS = {
    "water": "glasses", "walk": "walks", "reading": "sessions", "exercise": "sessions",
    "prayer": "prayers", "quran": "pages", "sleep": "hours", "study": "hours",
}

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _base_dir() -> Path:
    try:
        from memory.config_manager import BASE_DIR
        return Path(BASE_DIR)
    except Exception:
        return Path(__file__).resolve().parent.parent


def _store_path() -> Path:
    folder = _base_dir() / "config" / ".plugin_cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "habit_tracker.json"


def _read() -> dict:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict) -> None:
    _store_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _config() -> dict:
    values = {"habits": "water:8, walk:1, reading:1", "default_goal": "1",
              "default_unit": "times"}
    try:
        from memory.config_manager import get_plugin_config
        for key, value in (get_plugin_config("habit_tracker") or {}).items():
            if value not in (None, ""):
                values[key] = value
    except Exception:
        pass
    try:
        values["default_goal"] = max(1, int(float(values["default_goal"])))
    except (TypeError, ValueError):
        values["default_goal"] = 1
    return values


def _parse_configured(text: str, default_goal: int) -> dict[str, dict]:
    """'water:8, walk:1' → {'water': {'goal': 8, 'unit': 'glasses'}, …}"""
    out: dict[str, dict] = {}
    for chunk in str(text or "").replace(";", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        name, _, goal = item.partition(":")
        name = name.strip().lower()
        if not name:
            continue
        try:
            goal_value = max(1, int(float(goal.strip()))) if goal.strip() else default_goal
        except (TypeError, ValueError):
            goal_value = default_goal
        out[name] = {"goal": goal_value, "unit": _UNITS.get(name, "")}
    return out


def _habits(config: dict) -> dict[str, dict]:
    """
    Configured habits merged with any habit added out loud.

    Additions are stored in the data file rather than written back into the
    settings JSON, so the Plugin Manager form stays exactly as the user typed it
    and a spoken 'add pushups' never rewrites their config behind their back.
    """
    hints = _parse_configured(config["habits"], config["default_goal"])
    stored = _read().get("habits")
    merged: dict[str, dict] = {}
    if isinstance(stored, dict):
        for name, meta in stored.items():
            if isinstance(meta, dict):
                merged[str(name)] = {
                    "goal": int(meta.get("goal") or config["default_goal"]),
                    "unit": meta.get("unit") or _UNITS.get(str(name), config["default_unit"]),
                    "days": dict(meta.get("days") or {}),
                }
    for name, meta in hints.items():
        entry = merged.setdefault(name, {
            "goal": meta["goal"],
            "unit": meta["unit"] or config["default_unit"],
            "days": {},
        })
        entry["goal"] = meta["goal"]            # settings win over a spoken goal
        if meta["unit"]:
            entry["unit"] = meta["unit"]
    return merged


def _save(habits: dict[str, dict]) -> None:
    _write({"habits": habits})


def _key(text: str) -> str:
    """Spoken habit name → stored name, via the alias table."""
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    if raw in _ALIASES:
        return _ALIASES[raw]
    for word, name in _ALIASES.items():          # "another glass of water"
        if word in raw.split() or (len(word) > 3 and word in raw):
            return name
    return raw.replace(" ", "_")


def _day(text: str, today: _dt.date) -> _dt.date:
    raw = str(text or "").strip().lower()
    if not raw or raw in ("today", "now"):
        return today
    if raw in ("yesterday",):
        return today - _dt.timedelta(days=1)
    if raw in ("tomorrow",):
        return today + _dt.timedelta(days=1)
    try:
        return _dt.date.fromisoformat(raw)
    except Exception:
        return today


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _pick(habits: dict[str, dict], wanted: str) -> str | None:
    """
    Resolve a spoken name to a stored one, and say nothing when it is ambiguous.

    'water' matches 'water'; 'شرب ماء' matches through the alias table; an
    unknown word creates nothing — logging a typo as a new habit would quietly
    fragment somebody's streak data.
    """
    key = _key(wanted)
    if not key:
        return None
    if key in habits:
        return key
    for name in habits:
        if key and (key in name or name in key):
            return name
    return None


def _streak(days: dict, goal: int, today: _dt.date) -> int:
    """
    Consecutive days meeting the goal, counting back from today.

    A day that has not finished yet does not break the streak: if nothing is
    logged today, the count continues from yesterday. Otherwise every user would
    watch their streak reset every midnight until they had their first glass of
    water, which is exactly when they are asleep.
    """
    count = 0
    cursor = today
    if int(days.get(cursor.isoformat(), 0) or 0) < goal:
        cursor -= _dt.timedelta(days=1)
    while True:
        value = int(days.get(cursor.isoformat(), 0) or 0)
        if value < goal:
            break
        count += 1
        if count > 3650:
            break
        cursor -= _dt.timedelta(days=1)
    return count


def _line(name: str, meta: dict, today: _dt.date) -> str:
    done = float(meta["days"].get(today.isoformat(), 0) or 0)
    goal = int(meta["goal"])
    unit = meta.get("unit") or "times"
    streak = _streak(meta["days"], goal, today)
    # "1/1 sessions ✓" reads naturally whether the count is 1, 3 or 8 — and the
    # tick is what the model actually repeats out loud.
    mark = f"{done:g}/{goal} {unit}" + (" \u2713" if done >= goal else "")
    tail = f", {streak}-day streak" if streak > 1 else ""
    return f"{name}: {mark}{tail}"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_log(parameters: dict, config: dict, player) -> str:
    wanted = parameters.get("habit")
    if not wanted:
        return "Which habit should I log, sir?"
    habit = _pick(_habits(config), wanted)
    if habit is None:
        known = ", ".join(_habits(config)) or "none yet"
        return (f"'{wanted}' is not one of the habits I track ({known}). "
                f"Say 'add {wanted} as a habit' first.")
    try:
        amount = float(parameters.get("count", 1) or 1)
    except (TypeError, ValueError):
        amount = 1.0
    if amount <= 0:
        return "How much should I add, sir?"

    habits = _habits(config)
    meta = habits[habit]
    day = _day(parameters.get("date", ""), _dt.date.today())
    days = meta.setdefault("days", {})
    days[day.isoformat()] = round(float(days.get(day.isoformat(), 0) or 0) + amount, 2)
    _save(habits)

    return (_line(habit, meta, _dt.date.today())
            + f". Logged {amount:g} for {day.strftime('%d %b')}.")


def _action_status(config: dict) -> str:
    habits = _habits(config)
    today = _dt.date.today()
    if not habits:
        return "No habits are set up yet, sir. Say 'add a habit' or set the list in plugin settings."
    lines = [_line(name, meta, today) for name, meta in sorted(habits.items())]
    return "Today: " + "; ".join(lines) + "."


def _action_week(config: dict, player) -> str:
    habits = _habits(config)
    today = _dt.date.today()
    if not habits:
        return "No habits are set up yet, sir."
    days = [today - _dt.timedelta(days=offset) for offset in reversed(range(7))]
    header = "habit".ljust(12) + "".join(d.strftime("%a")[:3].rjust(7) for d in days)
    rows = [header]
    for name, meta in sorted(habits.items()):
        cells = []
        for day in days:
            value = int(meta["days"].get(day.isoformat(), 0) or 0)
            cells.append(("✓" if value >= int(meta["goal"]) else (str(value) if value else "·")).rjust(7))
        rows.append(name[:11].ljust(12) + "".join(cells))
    text = "\n".join(rows) + "\n\n✓ = goal met"
    if player:
        try:
            player.show_content("Habits — last 7 days", text)
        except Exception:
            pass
    met_today = sum(1 for meta in habits.values()
                    if int(meta["days"].get(today.isoformat(), 0) or 0) >= int(meta["goal"]))
    best = max((name for name in habits), key=lambda n: _streak(habits[n]["days"], int(habits[n]["goal"]), today))
    streak = _streak(habits[best]["days"], int(habits[best]["goal"]), today)
    return (f"The week is on screen, sir. {met_today} of {len(habits)} habits met today"
            + (f", and {best} has the longest run at {streak} days." if streak > 1 else "."))


def _action_add(parameters: dict, config: dict) -> str:
    wanted = _key(parameters.get("habit"))
    if not wanted:
        return "What should I call the new habit, sir?"
    habits = _habits(config)
    if wanted in habits:
        return f"'{wanted}' is already on the list."
    try:
        goal = max(1, int(float(parameters.get("count") or config["default_goal"])))
    except (TypeError, ValueError):
        goal = config["default_goal"]
    habits[wanted] = {"goal": goal, "unit": _UNITS.get(wanted, config["default_unit"]),
                      "days": {}}
    _save(habits)
    return (f"Added '{wanted}' with a daily goal of {goal} {habits[wanted]['unit']}. "
            f"To make it permanent, add it to the plugin settings list too.")


def _action_remove(parameters: dict, config: dict) -> str:
    habit = _pick(_habits(config), parameters.get("habit"))
    if habit is None:
        return "I do not track a habit by that name, sir."
    habits = _habits(config)
    removed = habits.pop(habit, {})
    _save(habits)
    logged = len(removed.get("days") or {})
    note = (f" Its {logged} logged days are gone too." if logged else "")
    if habit in _parse_configured(config["habits"], config["default_goal"]):
        note += " Remove it from the plugin settings list, or it returns at next start."
    return f"Removed '{habit}'.{note}"


def _action_reset(parameters: dict, config: dict) -> str:
    habit = _pick(_habits(config), parameters.get("habit"))
    if habit is None:
        return "Which habit should I reset, sir?"
    habits = _habits(config)
    day = _day(parameters.get("date", ""), _dt.date.today())
    (habits[habit].setdefault("days", {})).pop(day.isoformat(), None)
    _save(habits)
    return f"Cleared '{habit}' for {day.strftime('%d %b')}."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    try:
        config = _config()
        action = str(parameters.get("action", "log") or "log").strip().lower()
        if action in ("status", "today", "how"):
            result = _action_status(config)
        elif action in ("week", "week_grid", "history"):
            result = _action_week(config, player)
        elif action == "add":
            result = _action_add(parameters, config)
        elif action in ("remove", "delete"):
            result = _action_remove(parameters, config)
        elif action == "reset":
            result = _action_reset(parameters, config)
        else:
            result = _action_log(parameters, config, player)
    except Exception as exc:
        return f"Sir, habit_tracker failed: {exc}"

    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result


if __name__ == "__main__":
    print(run({"action": "log", "habit": "water", "count": 3}))
    print(run({"action": "log", "habit": "reading", "count": 1}))
    print(run({"action": "status"}))
    print(run({"action": "week"}))
    print(run({"action": "add", "habit": "pushups", "count": 20}))
    print(run({"action": "log", "habit": "pushups", "count": 20}))
    print(run({"action": "status"}))
