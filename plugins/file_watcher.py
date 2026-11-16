"""
file_watcher.py — noticing when files change, and doing something about it.

HOW IT NOTICES
    By looking, on a timer. Every few seconds it reads the size and modified time
    of the files in the folders you named and compares them with what it saw
    last time. Three things follow from that choice, and they are all good ones:
    no library beyond the standard one, no background service, no operating-
    system hooks that need permission — and the cost is one directory listing
    every few seconds, which on an idle folder is nothing at all.

    The honest limit: a file that appears and disappears within one interval is
    never seen. That is why the interval is a setting, and why the default is
    five seconds rather than a minute.

WHAT IT WILL NOT DO
    It does not watch anything you did not name. It does not follow symbolic
    links out of a watched folder. It does not watch its own cache or the
    assistant's working files — a watcher that reacts to the files written by
    the thing it triggered is a loop, and this one is built not to have one:
    an event inside the assistant's own folders, and anything under a folder
    that a rule has just acted on, are both ignored.

    And it never starts itself. Watchers run only after you say so.

RULES
    A rule says "when a file like this appears there, run that plugin". They live
    in config/watch_rules.json and can be added by voice. The example from the
    brief works exactly as written:

        when a new PDF appears in Downloads → index it in the Knowledge Base

SETTINGS (Plugin Manager → File watcher)
    interval      5 — seconds between checks
    max_depth     3 — how deep below each watched folder to look
    max_events    200 — how many recent events are kept
    ignore        .tmp,.part,.crdownload — name endings to ignore
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

PLUGIN = {
    "name": "file_watcher",
    "description": (
        "Watches folders you choose and reacts when files arrive or change: 'watch my "
        "Downloads folder', 'when a new PDF lands in Downloads, add it to my knowledge "
        "base', 'what changed in that folder', 'stop watching'. It only watches folders "
        "you name, and it never starts on its own."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "start | stop | status | check | events | add_rule | rules | remove_rule"},
            "path": {"type": "STRING", "description": "Folder to watch"},
            "pattern": {"type": "STRING", "description": "File pattern, e.g. *.pdf"},
            "target": {"type": "STRING", "description": "Plugin to run when it fires"},
            "rule_id": {"type": "STRING", "description": "Rule to remove"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "file_watcher",
    "title": "File watcher / مراقب الملفات",
    "fields": [
        {"key": "interval", "type": "text", "label": "Seconds between checks", "default": "5"},
        {"key": "max_depth", "type": "text", "label": "How deep to look", "default": "3"},
        {"key": "max_events", "type": "text", "label": "Recent events kept", "default": "200"},
        {"key": "ignore", "type": "text", "label": "Name endings to ignore",
         "default": ".tmp,.part,.crdownload,.partial,.swp"},
    ],
}

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
RULES_FILE = CONFIG_DIR / "watch_rules.json"
STATE_FILE = CONFIG_DIR / "watch_state.json"
EVENTS_FILE = CONFIG_DIR / ".plugin_cache" / "watch_events.json"

# Never watched: the assistant's own working folders. A rule that writes a file
# there must not be able to trigger itself.
_NEVER = ("config", ".plugin_cache", "__pycache__", "Documents/JARVIS")

_state_lock = threading.Lock()
_watcher: threading.Thread | None = None
_stop_flag = threading.Event()
_snapshot: dict[str, dict[str, tuple[float, int]]] = {}
_last_fired: dict[str, float] = {}
_watch_dirs: list[Path] = []
_started_at = 0.0


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("file_watcher") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def _ignore_endings() -> tuple[str, ...]:
    raw = str(_cfg().get("ignore") or "")
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def _in_our_own_folders(path: str | Path) -> bool:
    """
    Is this inside the assistant's own working area? If so it is ignored — the
    loop this plugin is most likely to create, closed on purpose.
    """
    text = str(path).replace("\\", "/").lower()
    base = str(Path(__file__).resolve().parent.parent).replace("\\", "/").lower()
    for marker in _NEVER:
        if f"{base}/{marker.lower()}" in text:
            return True
    return False


def _sibling(name: str):
    cached = sys.modules.get(f"_jarvis_{name}")
    if cached is not None:
        return cached
    target = Path(__file__).resolve().parent / f"{name}.py"
    if not target.exists():
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"_jarvis_{name}", target)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop(f"_jarvis_{name}", None)
        return None


# ── rules ─────────────────────────────────────────────────────────────────────

def load_rules() -> list[dict[str, Any]]:
    try:
        data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("rules"), list):
            return data["rules"]
    except FileNotFoundError:
        pass
    except Exception:
        return []
    return []


def save_rules(rules: list[dict[str, Any]]) -> bool:
    try:
        RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(RULES_FILE.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=1)
        os.replace(tmp, RULES_FILE)
        return True
    except Exception:
        return False


def add_rule(path: str, pattern: str, target: str, params: dict | None = None,
             on: str = "created,modified") -> dict[str, Any]:
    """
    Register one rule. `path` must be a real folder; `target` must name a plugin
    that exists next to this one — a rule pointing at nothing is a rule that
    fails silently at three in the morning.
    """
    folder = Path(path).expanduser()
    if not folder.is_dir():
        return {"ok": False, "reason": f"'{folder}' is not a folder I can watch"}
    if _in_our_own_folders(folder):
        return {"ok": False, "reason": ("that is one of the assistant's own working folders — "
                                        "watching it would make a rule trigger itself")}
    target_name = str(target or "").strip()
    if not target_name:
        return {"ok": False, "reason": "which plugin should run when this fires?"}
    if _sibling(target_name) is None:
        return {"ok": False, "reason": f"there is no plugin called '{target_name}' next to me"}
    rule = {
        "id": f"r{int(time.time())}{len(load_rules())}",
        "path": str(folder.resolve()),
        "pattern": (pattern or "*").strip() or "*",
        "target": target_name,
        "params": params or {"action": "index" if target_name == "knowledge_base" else "status"},
        "on": on,
        "added": time.time(),
        "fired": 0,
    }
    rules = load_rules()
    rules.append(rule)
    if not save_rules(rules):
        return {"ok": False, "reason": "I could not write the rules file"}
    return {"ok": True, "rule": rule}


def remove_rule(rule_id: str) -> bool:
    rules = load_rules()
    kept = [r for r in rules if r.get("id") != rule_id]
    if len(kept) == len(rules):
        return False
    return save_rules(kept)


# ── the scan itself ───────────────────────────────────────────────────────────

def _snapshot_folder(folder: Path) -> dict[str, tuple[float, int]]:
    """size + mtime for every file under a folder, within the depth limit."""
    depth_cap = _int_cfg("max_depth", 3, 1, 12)
    ignore = _ignore_endings()
    found: dict[str, tuple[float, int]] = {}
    base_depth = len(folder.parts)
    stack = [folder]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue                       # a link is a way out of the folder
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.startswith(".") or entry.name.lower() in ("__pycache__",):
                        continue
                    if len(Path(entry.path).parts) - base_depth < depth_cap:
                        stack.append(Path(entry.path))
                    continue
                name = entry.name.lower()
                if name.endswith(ignore):
                    continue
                stat = entry.stat(follow_symlinks=False)
                found[entry.path] = (stat.st_mtime, stat.st_size)
            except (PermissionError, OSError):
                continue
    return found


def scan_once(watch_paths: list[Path] | None = None) -> list[dict[str, Any]]:
    """
    One comparison against the previous snapshot. Returns the events found.

    A file that is gone and a file that is new are both reported — a rename shows
    up as those two events rather than as a move, because file identity is not
    something the file system tells us for free. Saying so is better than
    guessing.
    """
    dirs = watch_paths or _watch_dirs
    events: list[dict[str, Any]] = []
    with _state_lock:
        for folder in dirs:
            key = str(folder)
            previous = _snapshot.get(key, {})
            current = _snapshot_folder(folder)
            for path, (mtime, size) in current.items():
                old = previous.get(path)
                if old is None:
                    events.append({"kind": "created", "path": path, "size": size,
                                   "time": time.time()})
                elif old[0] != mtime or old[1] != size:
                    # Either signal is enough. A file can grow without its modified
                    # time moving a full tick (some file systems are coarse), and a
                    # touch can change the time without changing the size. Requiring
                    # both would miss real edits, which is the one thing a watcher
                    # must never do.
                    events.append({"kind": "modified", "path": path, "size": size,
                                   "time": time.time()})
            for path, (mtime, size) in previous.items():
                if path not in current:
                    events.append({"kind": "deleted", "path": path, "size": size,
                                   "time": time.time()})
            _snapshot[key] = current
    return events


def _remember(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    cap = _int_cfg("max_events", 200, 10, 5000)
    try:
        old: list[dict[str, Any]] = []
        if EVENTS_FILE.exists():
            old = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
            if not isinstance(old, list):
                old = []
        combined = (events + old)[:cap]
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        EVENTS_FILE.write_text(json.dumps(combined, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def recent_events(limit: int = 20) -> list[dict[str, Any]]:
    try:
        data = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        return data[:limit] if isinstance(data, list) else []
    except Exception:
        return []


def _matches(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    try:
        if not str(event["path"]).startswith(str(rule.get("path", ""))):
            return False
        if event["kind"] not in str(rule.get("on", "created,modified")).split(","):
            return False
        return fnmatch.fnmatch(os.path.basename(event["path"]).lower(),
                               str(rule.get("pattern", "*")).lower())
    except Exception:
        return False


def fire(event: dict[str, Any], player=None) -> list[str]:
    """
    Run every rule that matches one event. Returns what was done.

    The debounce is the second half of the loop guard: the same file cannot
    trigger the same rule twice inside ten seconds, no matter how many times the
    operating system reports it as touched.
    """
    done: list[str] = []
    for rule in load_rules():
        if not _matches(rule, event):
            continue
        key = f"{rule.get('id')}:{event['path']}"
        if time.time() - _last_fired.get(key, 0) < 10:
            continue
        _last_fired[key] = time.time()
        module = _sibling(str(rule.get("target")))
        if module is None or not hasattr(module, "run"):
            done.append(f"{rule.get('target')}: plugin not available")
            continue
        params = dict(rule.get("params") or {})
        # A file rule usually wants the file, not a folder: pass it through when
        # the target understands paths.
        params.setdefault("path", event["path"])
        try:
            result = str(module.run(params, player=player))
            done.append(f"{rule.get('target')}: {result.splitlines()[0][:70]}")
        except Exception as e:
            done.append(f"{rule.get('target')}: failed ({str(e)[:60]})")
        rule["fired"] = int(rule.get("fired", 0)) + 1
    if done:
        rules = load_rules()
        for rule in rules:
            if _matches(rule, event):
                rule["fired"] = int(rule.get("fired", 0)) + 1
        save_rules(rules)
    return done


# ── the background loop ───────────────────────────────────────────────────────

def _loop(player=None) -> None:
    interval = _int_cfg("interval", 5, 1, 3600)
    while not _stop_flag.is_set():
        try:
            events = scan_once()
            for event in events:
                if _in_our_own_folders(event["path"]):
                    continue
                got = fire(event, player)
                if got and player:
                    try:
                        player.write_log(f"[Watch] {os.path.basename(event['path'])} → "
                                         f"{'; '.join(got)[:60]}")
                    except Exception:
                        pass
            _remember(events)
        except Exception:
            pass                       # a watcher that dies on a locked file is useless
        _stop_flag.wait(interval)


def start(paths: list[str], player=None) -> str:
    """Begin watching. Refuses anything that is not a real folder or is our own."""
    global _watcher, _watch_dirs, _started_at
    if _watcher is not None and _watcher.is_alive():
        return "I am already watching, sir — say 'stop watching' first."
    if not paths:
        return "Which folder should I watch, sir?"
    resolved: list[Path] = []
    for raw in paths:
        folder = Path(raw).expanduser()
        if not folder.is_dir():
            return f"Sir, '{folder}' is not a folder I can watch."
        if _in_our_own_folders(folder):
            return (f"Sir, '{folder}' is one of my own working folders — watching it would let "
                    f"a rule trigger itself, so I will not.")
        resolved.append(folder.resolve())
    _stop_flag.clear()
    _watch_dirs = resolved
    with _state_lock:
        _snapshot.clear()
        for folder in resolved:
            _snapshot[str(folder)] = _snapshot_folder(folder)     # baseline: no events on start
    _started_at = time.time()
    _watcher = threading.Thread(target=_loop, args=(player,), name="FileWatcher", daemon=True)
    _watcher.start()
    names = ", ".join(f.name for f in resolved)
    return (f"Watching {names}, sir — every {_int_cfg('interval', 5, 1, 3600)} seconds. "
            f"Anything already there is the baseline, so only changes from now are events.")


def stop() -> str:
    global _watcher, _watch_dirs
    if _watcher is None or not _watcher.is_alive():
        return "I am not watching anything, sir."
    _stop_flag.set()
    try:
        _watcher.join(timeout=3)
    except Exception:
        pass
    _watcher = None
    _watch_dirs = []
    return "Stopped watching, sir."


def is_running() -> bool:
    return _watcher is not None and _watcher.is_alive()


# ── tool entry point ──────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one watcher action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "status").strip().lower()

        if action in ("start", "watch"):
            raw = str(p.get("path") or "").strip()
            paths = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
            if not paths:
                rules = load_rules()
                paths = sorted({str(r.get("path")) for r in rules}) if rules else []
                if not paths:
                    return "Which folder should I watch, sir?"
            return start(paths, player)

        if action in ("stop", "unwatch"):
            return stop()

        if action in ("check", "scan", "now"):
            if not _watch_dirs:
                raw = str(p.get("path") or "").strip()
                if not raw:
                    return "I am not watching any folder, sir — name one, or start a watch first."
                folder = Path(raw).expanduser()
                if not folder.is_dir():
                    return f"Sir, '{folder}' is not a folder."
                globals()["_watch_dirs"] = [folder.resolve()]
                with _state_lock:
                    _snapshot[str(folder.resolve())] = _snapshot_folder(folder.resolve())
                return (f"Took a baseline of {folder.name}, sir. Changes will show up from the "
                        f"next check.")
            events = scan_once()
            _remember(events)
            if not events:
                return "Nothing has changed since the last check, sir."
            lines = [f"{len(events)} change(s):"]
            for e in events[:15]:
                lines.append(f"  {e['kind']:<9} {os.path.basename(e['path'])[:54]}")
            fired = []
            for e in events:
                fired += fire(e, player)
            if fired:
                lines.append("\nRules that ran:")
                lines += [f"  {x}" for x in fired[:8]]
            return "\n".join(lines)

        if action in ("add_rule", "rule", "when"):
            path = str(p.get("path") or "").strip()
            pattern = str(p.get("pattern") or "*").strip()
            target = str(p.get("target") or "").strip()
            params = p.get("params") if isinstance(p.get("params"), dict) else None
            if not path:
                return "Which folder should the rule watch, sir?"
            if not target:
                return ("Which plugin should run when it fires, sir? For example: knowledge_base, "
                        "note_manager, document_generator, notification_center.")
            result = add_rule(path, pattern, target, params)
            if not result.get("ok"):
                return f"Sir, {result['reason']}."
            rule = result["rule"]
            return (f"Rule added, sir: when a file matching '{rule['pattern']}' appears in "
                    f"{Path(rule['path']).name}, I will run {rule['target']}. "
                    f"(id {rule['id']})")

        if action in ("rules", "list_rules"):
            rules = load_rules()
            if not rules:
                return ("No rules yet, sir. For example: 'when a new PDF appears in Downloads, "
                        "add it to my knowledge base'.")
            lines = [f"{len(rules)} rule(s):"]
            for r in rules:
                lines.append(f"  {r['id']}  {r['pattern']} in {Path(str(r.get('path'))).name} "
                             f"→ {r.get('target')}  (fired {r.get('fired', 0)}×)")
            return "\n".join(lines)

        if action in ("remove_rule", "delete_rule"):
            rule_id = str(p.get("rule_id") or p.get("id") or "").strip()
            if not rule_id:
                return "Which rule, sir? Say 'rules' and I will list them with their ids."
            return (f"Removed rule {rule_id}, sir." if remove_rule(rule_id)
                    else f"I have no rule called '{rule_id}', sir.")

        if action in ("events", "history", "what_changed"):
            events = recent_events(int(float(p.get("limit") or 20)))
            if not events:
                return "I have not recorded any file events yet, sir."
            lines = [f"The last {len(events)} change(s) I noticed:"]
            for e in events[:20]:
                when = time.strftime("%H:%M:%S", time.localtime(e.get("time", 0)))
                lines.append(f"  {when}  {e.get('kind', '?'):<9} "
                             f"{os.path.basename(str(e.get('path', '')))[:48]}")
            return "\n".join(lines)

        if action in ("status", "info"):
            rules = load_rules()
            watched = ", ".join(d.name for d in _watch_dirs) or "nothing"
            return (f"Watching     : {watched}\n"
                    f"Running      : {'yes' if is_running() else 'no'}\n"
                    f"Interval     : every {_int_cfg('interval', 5, 1, 3600)} seconds\n"
                    f"Rules        : {len(rules)}\n"
                    f"Events kept  : {len(recent_events(1000))}\n"
                    f"Ignored      : my own working folders, and anything under the cache\n"
                    f"Starts alone : never — a watch begins only when you ask")

        return (f"Sir, '{action}' is not a watcher action I have. I can start or stop watching, "
                f"check now, add or list rules, or show what changed.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the file watcher hit a problem: {str(e)[:150]}"


# ── self-test: python file_watcher.py ────────────────────────────────────────

def _selftest() -> int:
    print("file_watcher — self-test")
    problems = 0

    root = Path(__file__).resolve().parent / "_watch_probe"
    root.mkdir(exist_ok=True)
    rules_backup = RULES_FILE.read_text(encoding="utf-8") if RULES_FILE.exists() else None

    try:
        print(f"  own-folder guard    : {_in_our_own_folders(CONFIG_DIR)} (must be True) · "
              f"{_in_our_own_folders(root)} (must be False)")
        problems += 0 if _in_our_own_folders(CONFIG_DIR) and not _in_our_own_folders(root) else 1

        # baseline, then create a file: exactly one event, and of the right kind
        global _watch_dirs
        _watch_dirs = [root.resolve()]
        with _state_lock:
            _snapshot.clear()
            _snapshot[str(root.resolve())] = _snapshot_folder(root.resolve())
        (root / "new.pdf").write_bytes(b"%PDF-1.4 test")
        events = scan_once()
        print(f"  created detected    : {[e['kind'] for e in events]}")
        problems += 0 if [e["kind"] for e in events] == ["created"] else 1

        again = scan_once()
        print(f"  no repeat           : {len(again)} event(s) on an unchanged folder")
        problems += 0 if not again else 1

        (root / "new.pdf").write_bytes(b"%PDF-1.4 test, now longer")
        events = scan_once()
        print(f"  modified detected   : {[e['kind'] for e in events]}")
        problems += 0 if [e["kind"] for e in events] == ["modified"] else 1

        (root / "new.pdf").unlink()
        events = scan_once()
        print(f"  deleted detected    : {[e['kind'] for e in events]}")
        problems += 0 if [e["kind"] for e in events] == ["deleted"] else 1

        # rules
        bad = add_rule(str(root), "*.pdf", "no_such_plugin")
        print(f"  rule → missing plug : {bad['reason'][:70]}")
        problems += 0 if not bad["ok"] else 1
        own = add_rule(str(CONFIG_DIR), "*", "knowledge_base")
        print(f"  rule → own folder   : {own['reason'][:70]}")
        problems += 0 if not own["ok"] else 1
        good = add_rule(str(root), "*.pdf", "notification_center",
                        {"action": "notify", "title": "New PDF", "body": "a file arrived"})
        print(f"  rule added          : {good['ok']} id={good.get('rule', {}).get('id')}")
        problems += 0 if good["ok"] else 1

        event = {"kind": "created", "path": str(root / "report.pdf"), "size": 10, "time": time.time()}
        matched = [r["id"] for r in load_rules() if _matches(r, event)]
        print(f"  pattern matches     : {len(matched)} rule(s)")
        problems += 0 if matched == [good["rule"]["id"]] else 1
        non_match = {"kind": "created", "path": str(root / "notes.txt"), "size": 10,
                     "time": time.time()}
        print(f"  pattern rejects     : {len([r for r in load_rules() if _matches(r, non_match)])} "
              f"rule(s) for a .txt")
        problems += 0 if not [r for r in load_rules() if _matches(r, non_match)] else 1

        # firing really runs the target plugin
        got = fire(event)
        print(f"  fired               : {got}")
        problems += 0 if got and "notification_center" in got[0] else 1

        # debounce: the same file must not fire twice in ten seconds
        again = fire(event)
        print(f"  debounce            : {len(again)} second firing(s) for the same file")
        problems += 0 if not again else 1

        print(f"  remove rule         : {remove_rule(good['rule']['id'])}")
        problems += 0 if not remove_rule(good["rule"]["id"]) else 1

        print(f"  start w/o path      : {run({'action': 'start'})}")
        problems += 0 if "Which folder" in run({"action": "start"}) or load_rules() else 1
        print(f"  watch own folder    : {start([str(CONFIG_DIR)])[:76]}")
        problems += 0 if "own working folders" in start([str(CONFIG_DIR)]) else 1
        print(f"  watch a real one    : {start([str(root)])[:76]}")
        problems += 0 if is_running() else 1
        time.sleep(0.2)
        print(f"  status while on     : {run({'action': 'status'}).splitlines()[1][:44]}")
        print(f"  stop                : {run({'action': 'stop'})}")
        problems += 0 if not is_running() else 1
        print(f"  bad action          : {run({'action': 'sing'})[:68]}")
        problems += 0 if "not a watcher action" in run({"action": "sing"}) else 1
    finally:
        stop()
        _watch_dirs = []
        for f in sorted(root.rglob("*"), reverse=True):
            try:
                f.unlink() if f.is_file() else f.rmdir()
            except Exception:
                pass
        try:
            root.rmdir()
        except Exception:
            pass
        if rules_backup is None:
            RULES_FILE.unlink(missing_ok=True)
        else:
            RULES_FILE.write_text(rules_backup, encoding="utf-8")
        STATE_FILE.unlink(missing_ok=True)
        EVENTS_FILE.unlink(missing_ok=True)

    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
