"""
download_manager.py — fetching a file from a link, with the brakes on.

WHAT IT DOWNLOADS
    A file from an http or https address, into one folder you choose, with the
    progress kept on disk so a download can be paused and picked up again
    instead of started over. Resume works when the server offers it (a Range
    request — most file hosts do); when the server refuses, the plugin says so
    and restarts from the beginning rather than producing a half-file with a
    plausible size.

WHAT IT REFUSES
    Addresses with a username and password inside them. A link written as
    https://user:pass@host/file is a credential in a string, and strings end up
    in logs and in chat histories. If a site needs a login, download it in your
    browser — this plugin will not carry your password around in a URL.

    It also does not touch sites that are designed to stop automation: no
    solving of challenges, no mimicry to get past a wall, no bypassing of access
    controls. If a server answers 403 or 429, that is a no, and the answer is
    reported as it came.

    And it never overwrites. A second file with the same name gets " (2)".

SETTINGS (Plugin Manager → Downloads)
    download_dir   blank = Downloads/JARVIS_Downloads
    max_mb         2048 — a ceiling per file, so a wrong link cannot fill the disk
    timeout_s      30 — silence from the server for this long ends the attempt
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

PLUGIN = {
    "name": "download_manager",
    "description": (
        "Downloads a file from a link with progress, pause and resume: 'download this file', "
        "'what is downloading', 'pause that download', 'cancel it'. Files go to one folder, "
        "nothing is overwritten, and it refuses to fetch anything that needs a password or "
        "gets past a site's protections."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | list | status | pause | resume | cancel"},
            "url": {"type": "STRING", "description": "The address to download from"},
            "filename": {"type": "STRING", "description": "Optional name to save as"},
            "id": {"type": "STRING", "description": "Download id, for pause/resume/cancel"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "download_manager",
    "title": "Downloads / التنزيلات",
    "fields": [
        {"key": "download_dir", "type": "text", "label": "Save folder (blank = Downloads/JARVIS_Downloads)",
         "default": ""},
        {"key": "max_mb", "type": "text", "label": "Largest file, MB", "default": "2048"},
        {"key": "timeout_s", "type": "text", "label": "Server silence timeout, seconds",
         "default": "30"},
    ],
}

STATE = Path(__file__).resolve().parent.parent / "config" / "downloads.json"
_jobs: dict[str, dict[str, Any]] = {}
_control: dict[str, threading.Event] = {}
_lock = threading.Lock()


def _cfg() -> dict[str, Any]:
    out = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    try:
        from memory.config_manager import get_plugin_config
        out.update({k: v for k, v in (get_plugin_config("download_manager") or {}).items()
                    if v not in (None, "")})
    except Exception:
        pass
    return out


def _int_cfg(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(_cfg().get(key) or default))))
    except Exception:
        return default


def download_dir() -> Path:
    raw = str(_cfg().get("download_dir") or "").strip()
    base = Path(raw).expanduser() if raw else (Path.home() / "Downloads" / "JARVIS_Downloads")
    try:
        return base.resolve()
    except Exception:
        return Path.home() / "Downloads" / "JARVIS_Downloads"


def validate_url(url: str) -> tuple[bool, str]:
    """
    An address this plugin is willing to fetch. The credential check is first
    because it is the one that leaks something if it is missed.
    """
    raw = str(url or "").strip()
    if not raw:
        return False, "there is no address"
    try:
        parsed = urlparse(raw)
    except Exception:
        return False, "that is not a readable address"
    if parsed.scheme not in ("http", "https"):
        return False, (f"'{parsed.scheme or 'no scheme'}' is not a scheme I download from — "
                       f"http and https only")
    if parsed.username or parsed.password:
        return False, ("that address carries a username and password inside it. I will not "
                       "fetch it — a password in a URL ends up in logs and histories. If the "
                       "site needs a login, download it in your browser")
    if not parsed.netloc or "." not in parsed.netloc:
        return False, f"'{parsed.netloc or raw}' does not look like a host"
    return True, ""


def safe_filename(name: str, fallback: str = "download") -> str:
    """A file name that cannot escape the folder or upset Windows."""
    name = unquote(str(name or "")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    name = re.sub(r"\s+", "_", name)
    if not name:
        name = fallback
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if name.split(".")[0].upper() in reserved:
        name = "_" + name
    return name[:120]


def _unique(path: Path) -> Path:
    """Never overwrite: report (2), report (3), and say which one was used."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(2, 500):
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem} ({int(time.time())}){suffix}")


def _name_from(url: str) -> str:
    tail = Path(urlparse(url).path).name
    return safe_filename(tail or "download")


def _save_state() -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        rows = [{k: v for k, v in job.items() if k != "stop_event"} for job in _jobs.values()]
        fd, tmp = tempfile.mkstemp(dir=str(STATE.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)
    except Exception:
        pass


def _load_state() -> dict[str, Any]:
    try:
        rows = json.loads(STATE.read_text(encoding="utf-8"))
        return {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")}
    except Exception:
        return {}


def _notify(title: str, body: str, priority: str = "normal") -> None:
    """Tell the Notification Center, whose job it is to tell you."""
    try:
        import importlib.util
        import sys
        name = "_jarvis_notification_center"
        module = sys.modules.get(name)
        if module is None:
            path = Path(__file__).resolve().parent / "notification_center.py"
            if not path.exists():
                return
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        if hasattr(module, "publish"):
            module.publish(title, body, category="download", priority=priority, source="download")
    except Exception:
        pass


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


# ── the download itself ───────────────────────────────────────────────────────

def _worker(job_id: str) -> None:
    """One download, on its own thread, with the brakes reachable from outside."""
    job = _jobs[job_id]
    stop: threading.Event = _control[job_id]
    target = Path(job["path"])
    part = target.with_suffix(target.suffix + ".part")
    cap = _int_cfg("max_mb", 2048, 1, 100_000) * 1024 * 1024
    timeout = _int_cfg("timeout_s", 30, 5, 600)

    try:
        import requests
    except Exception:
        job.update(state="failed", error="the requests library is not installed")
        _save_state()
        return

    resume_at = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "Mark-LV download"}

    try:
        with requests.get(job["url"], stream=True, timeout=timeout,
                          headers=headers, allow_redirects=True) as response:
            if response.status_code in (401, 403):
                job.update(state="failed", error=("the server refused (401/403) — that needs a "
                                                  "login or it blocks this kind of request, and I "
                                                  "do not get past either"))
                _notify("Download failed", f"{job['filename']} — the server refused", "high")
                return
            if response.status_code == 404:
                job.update(state="failed", error="the server says that address does not exist (404)")
                _notify("Download failed", f"{job['filename']} — 404", "high")
                return
            if response.status_code == 429:
                job.update(state="failed", error="the server is rate-limiting me (429) — try later")
                _notify("Download failed", f"{job['filename']} — rate limited", "high")
                return
            if response.status_code >= 400:
                job.update(state="failed", error=f"the server answered {response.status_code}")
                _notify("Download failed", f"{job['filename']} — {response.status_code}", "high")
                return

            accepts_ranges = response.headers.get("Accept-Ranges", "").lower() == "bytes"
            total = 0
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            if resume_at and (accepts_ranges or response.status_code == 206):
                headers_range = {"Range": f"bytes={resume_at}-"}
            else:
                resume_at = 0
                headers_range = {}

        if headers_range:                    # the Range request, second attempt
            response = requests.get(job["url"], stream=True, timeout=timeout,
                                    headers={**headers, **headers_range}, allow_redirects=True)
            mode = "ab"
        else:
            response = requests.get(job["url"], stream=True, timeout=timeout,
                                    headers=headers, allow_redirects=True)
            mode = "wb"

        job.update(state="downloading" if job.get("state") != "paused" else "paused",
                   started=job.get("started") or time.time(),
                   total=total + resume_at if total else 0,
                   resumed_from=resume_at)
        _save_state()

        done = resume_at
        window_start, window_bytes = time.time(), 0
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(part, mode) as fh:
            for chunk in response.iter_content(chunk_size=65536):
                if stop.is_set() or job.get("state") == "cancelled":
                    job.update(state="cancelled", error="")
                    response.close()
                    _save_state()
                    return
                if job.get("state") == "paused":
                    fh.flush()
                    response.close()
                    job.update(state="paused", done=done)
                    _save_state()
                    return
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                window_bytes += len(chunk)
                if done > cap:
                    fh.close()
                    job.update(state="failed",
                               error=f"it went past the {cap // (1024*1024)} MB ceiling I allow")
                    _notify("Download stopped", f"{job['filename']} — over the size ceiling", "high")
                    _save_state()
                    return
                elapsed = time.time() - window_start
                if elapsed >= 0.5:
                    job["speed"] = round(window_bytes / elapsed, 0)
                    window_start, window_bytes = time.time(), 0
                job["done"] = done
        response.close()

        os.replace(part, target)             # the file appears only when it is whole
        job.update(state="done", done=done, finished=time.time(), path=str(target), error="")
        _save_state()
        _notify("Download complete", f"{target.name} ({human(done)})", "normal")
    except Exception as e:
        message = str(e)[:120]
        if "timed out" in message.lower() or "timeout" in message.lower():
            message = "the server stopped answering — try again, and I will resume from the part"
        elif "getaddrinfo" in message or "Name or service" in message:
            message = "that address could not be found — check the link"
        job.update(state="failed", error=message)
        _notify("Download failed", f"{job['filename']} — {message[:60]}", "high")
        _save_state()


def start(url: str, filename: str = "", folder: str = "") -> dict[str, Any]:
    """
    Begin a download. Returns {ok, id, path} or {ok: False, reason} — never a
    claim that something is downloading when it is not.
    """
    ok, why = validate_url(url)
    if not ok:
        return {"ok": False, "reason": why}
    job_id = f"d{int(time.time())}{len(_jobs)}"
    name = safe_filename(filename) if filename else _name_from(url)
    base = Path(folder).expanduser().resolve() if folder else download_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"ok": False, "reason": f"I could not use {base}: {str(e)[:60]}"}

    path = _unique(base / name)
    with _lock:
        _jobs[job_id] = {"id": job_id, "url": url, "filename": path.name, "path": str(path),
                         "state": "starting", "done": 0, "total": 0, "speed": 0,
                         "error": "", "started": time.time()}
        _control[job_id] = threading.Event()
    threading.Thread(target=_worker, args=(job_id,), name=f"Download-{job_id}",
                     daemon=True).start()
    renamed = path.name != name
    return {"ok": True, "id": job_id, "path": str(path), "dir": str(base), "renamed": renamed}


def _job(job_id: str) -> dict[str, Any] | None:
    job = _jobs.get(job_id)
    if job is None:
        job = _load_state().get(job_id)
    return job


# ── tool entry point ──────────────────────────────────────────────────────────

def _line(job: dict[str, Any]) -> str:
    state = str(job.get("state", "?"))
    done = int(job.get("done") or 0)
    total = int(job.get("total") or 0)
    percent = f"{done / total * 100:.0f}%" if total else "unknown"
    speed = f"{human(job['speed'])}/s" if job.get("speed") else "—"
    tail = f" — {job['error']}" if job.get("error") else ""
    return (f"  {job['id']}  {str(job.get('filename', '?'))[:32]:<32} {state:<12} "
            f"{human(done):>10} of {human(total) if total else '?':>10}  {percent:>5}  "
            f"{speed}{tail}")


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Dispatch one download action. Never raises."""
    try:
        p = parameters or {}
        action = str(p.get("action") or "list").strip().lower()

        if action in ("start", "download", "get", "fetch"):
            url = str(p.get("url") or p.get("link") or "").strip()
            if not url:
                return "Which address should I download from, sir?"
            result = start(url, str(p.get("filename") or ""), str(p.get("folder") or ""))
            if not result.get("ok"):
                return f"Sir, {result['reason']}."
            if player:
                try:
                    player.write_log(f"[Download] {Path(result['path']).name} starting")
                except Exception:
                    pass
            note = (" The name was already taken, so this is a new file." if result["renamed"]
                    else "")
            return (f"Started, sir — saving to {result['dir']} as "
                    f"{Path(result['path']).name} (id {result['id']}). Say 'downloads' to see how "
                    f"it is going.{note}")

        if action in ("list", "all", "downloads"):
            jobs = {**_load_state(), **_jobs}
            if not jobs:
                return "Nothing has been downloaded yet, sir."
            rows = list(jobs.values())[-10:]
            return f"{len(jobs)} download(s), newest last:\n" + "\n".join(_line(j) for j in rows)

        if action in ("status", "progress", "how"):
            job_id = str(p.get("id") or "").strip()
            if job_id:
                job = _job(job_id)
                if job is None:
                    return f"I have no download called '{job_id}', sir."
                return _line(job).strip()
            active = [j for j in _jobs.values() if j.get("state") == "downloading"]
            if not active:
                return "Nothing is downloading right now, sir. Say 'downloads' for the history."
            return "Downloading now:\n" + "\n".join(_line(j) for j in active)

        if action in ("pause", "hold"):
            job_id = str(p.get("id") or "").strip() or next(
                (j for j in _jobs.values() if j.get("state") == "downloading"), {}).get("id", "")
            if not job_id:
                return "Which download, sir?"
            job = _jobs.get(job_id)
            if job is None:
                return f"I have no active download called '{job_id}', sir."
            job["state"] = "paused"
            _save_state()
            return f"Paused {job['filename']}, sir — say 'resume' and I will pick it up where it stopped."

        if action in ("resume", "continue"):
            job_id = str(p.get("id") or "").strip()
            paused = [j for j in _jobs.values() if j.get("state") == "paused"]
            job = _jobs.get(job_id) if job_id else (paused[0] if paused else None)
            if job is None:
                return ("Nothing is paused, sir. If it was paused in an earlier session, say "
                        "'download <address>' again and I will resume from the part file.")
            _control[job["id"]] = threading.Event()
            job["state"] = "downloading"
            threading.Thread(target=_worker, args=(job["id"],), name=f"Download-{job['id']}",
                             daemon=True).start()
            return f"Resuming {job['filename']}, sir."

        if action in ("cancel", "stop", "abort"):
            job_id = str(p.get("id") or "").strip() or next(
                (j for j in _jobs.values()
                 if j.get("state") in ("downloading", "paused")), {}).get("id", "")
            job = _jobs.get(job_id) if job_id else None
            if job is None:
                return "Which download should I cancel, sir?"
            job["state"] = "cancelled"
            _control.setdefault(job_id, threading.Event()).set()
            _save_state()
            return (f"Cancelled {job['filename']}, sir. The part file stays on disk — delete it "
                    f"yourself if you want the space back.")

        if action in ("status_all", "info", "where"):
            return (f"Save folder : {download_dir()}\n"
                    f"Size ceiling: {_int_cfg('max_mb', 2048, 1, 100000)} MB per file\n"
                    f"State file  : {STATE}\n"
                    f"Refusals    : links carrying a username and password, sites that answer "
                    f"401/403/429 — I do not get past a site's protections\n"
                    f"Overwrites  : never; a second file gets ' (2)'")

        return (f"Sir, '{action}' is not a download action I have. I can start, list, check, "
                f"pause, resume, or cancel.")
    except Exception as e:                                   # noqa: BLE001
        return f"Sir, the download manager hit a problem: {str(e)[:150]}"


# ── self-test: python download_manager.py ───────────────────────────────────

def _selftest() -> int:
    print("download_manager — self-test")
    problems = 0

    for url, want_ok, label in (
        ("https://example.com/file.zip", True, "normal https"),
        ("http://example.com/a.pdf", True, "plain http"),
        ("ftp://example.com/a", False, "ftp scheme"),
        ("file:///etc/passwd", False, "local file scheme"),
        ("https://user:secret@example.com/a.zip", False, "credentials in the URL"),
        ("https://user@example.com/a.zip", False, "a bare username in the URL"),
        ("not a url", False, "not an address"),
        ("", False, "empty"),
    ):
        ok, why = validate_url(url)
        mark = "✓" if ok == want_ok else "✗"
        problems += 0 if ok == want_ok else 1
        print(f"  {label:<26} {'allowed' if ok else 'refused':<8} {mark}"
              + (f"  ({why[:44]})" if why else ""))

    for raw, want in (("../../etc/passwd", "passwd"), ("a/b\\c.txt", "abc.txt"),
                      ("CON", "_CON"), ("", "download"), ("file name.pdf", "file_name.pdf")):
        got = safe_filename(raw)
        mark = "✓" if (want in got or got == want) else "✗"
        problems += 0 if (want in got or got == want) else 1
        print(f"  name {raw!r:<20} → {got!r:<20} {mark}")
    problems += 0 if "/" not in safe_filename("../../etc/passwd") else 1

    root = Path(__file__).resolve().parent / "_dl_probe"
    root.mkdir(exist_ok=True)
    global _cfg
    real = _cfg
    _cfg = lambda: {**real(), "download_dir": str(root)}
    try:
        print(f"  save folder         : {download_dir()}")
        (root / "report.pdf").write_bytes(b"x")
        result = start("https://example.com/report.pdf")
        print(f"  never overwrites    : {Path(result.get('path', '?')).name} "
              f"(renamed={result.get('renamed')})")
        problems += 0 if result.get("ok") and result.get("renamed") else 1

        bad = start("https://user:pw@example.com/x.zip")
        print(f"  refuses credentials : {bad['reason'][:70]}")
        problems += 0 if not bad["ok"] else 1

        missing_scheme = start("not a url")
        print(f"  refuses nonsense    : {missing_scheme['reason'][:60]}")
        problems += 0 if not missing_scheme["ok"] else 1

        # A real download from a real (local) server: no internet needed.
        import http.server
        import socketserver
        import threading as _t
        served = Path(__file__).resolve().parent / "_dl_serve"
        served.mkdir(exist_ok=True)
        payload = b"hello from the test server\n" * 500
        (served / "sample.bin").write_bytes(payload)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(served), **k)

            def log_message(self, *a):
                pass

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
            port = httpd.server_address[1]
            _t.Thread(target=httpd.serve_forever, daemon=True).start()
            got = start(f"http://127.0.0.1:{port}/sample.bin")
            print(f"  real download       : ok={got.get('ok')} id={got.get('id')}")
            for _ in range(60):
                job = _jobs.get(got["id"], {})
                if job.get("state") in ("done", "failed"):
                    break
                time.sleep(0.1)
            job = _jobs.get(got["id"], {})
            print(f"  finished as         : {job.get('state')} — {job.get('done')} bytes "
                  f"(expected {len(payload)})")
            problems += 0 if job.get("state") == "done" and job.get("done") == len(payload) else 1
            on_disk = Path(got["path"]).exists() and Path(got["path"]).stat().st_size == len(payload)
            print(f"  file on disk whole  : {on_disk}")
            problems += 0 if on_disk else 1
            print(f"  list action         : {run({'action': 'list'}).splitlines()[-1].strip()[:76]}")
            not_found = start(f"http://127.0.0.1:{port}/nothing-here.bin")
            for _ in range(60):
                job2 = _jobs.get(not_found["id"], {})
                if job2.get("state") in ("done", "failed"):
                    break
                time.sleep(0.1)
            job2 = _jobs.get(not_found["id"], {})
            print(f"  404 handled         : {job2.get('state')} — {str(job2.get('error'))[:60]}")
            problems += 0 if job2.get("state") == "failed" else 1
            httpd.shutdown()
    finally:
        _cfg = real
        import shutil
        for d in (root, Path(__file__).resolve().parent / "_dl_serve"):
            shutil.rmtree(d, ignore_errors=True)
        STATE.unlink(missing_ok=True)

    print(f"  no url              : {run({'action': 'start'})}")
    problems += 0 if "Which address" in run({"action": "start"}) else 1
    print(f"  cancel nothing      : {run({'action': 'cancel'})[:60]}")
    problems += 0 if "Which download" in run({"action": "cancel"}) else 1
    print(f"  bad action          : {run({'action': 'sing'})[:66]}")
    problems += 0 if "not a download action" in run({"action": "sing"}) else 1
    print(f"  status              : {run({'action': 'status_all'}).splitlines()[0][:60]}")
    print("  RESULT:", "PASS" if problems == 0 else f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(_selftest())
