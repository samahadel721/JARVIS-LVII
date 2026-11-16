"""
face_auth.py — التحقق من الوجه + إيفيكت EDITH (شبكة على الوجه أثناء المسح).

مبنيّة على مكتبة مشتركة (`_face_core.py`) وبلا لمس أي ملف من ملفات مشروعك:

• **تدريب أولًا، ثم تحقّق.** تجلس أمام الكاميرا، تظهر الشبكة مع عدّاد لقطات، وكل
  لقطة تمرّ بستّ بوّابات (حجم الوجه · الوجه الأكبر · ثقة الكشف · مواجهتك ·
  الإضاءة · الوضوح)، والرفض يقول السبب بصيغة أمر: «اقترب من الكاميرا» · «ثبّت رأسك»…
  وبعد ٤ لقطات مقبولة: العتبة تُعاير من بياناتك، وتُحفظ بصمتك في ملف بصلاحية 600.

• **التحقق:** الشبكة + المسح من الجبهة إلى الذقن + قراءات حيّة + الأخضر عند القبول
  و«WELCOME BACK, SIR» — والعتبة المعايَرة وحدها هي التي تقرّر (بوّابات التدريب
  لا تُطبَّق هنا، وهذا درسٌ دفعنا ثمنه في التجربة).

• **فحص الحياة:** رمشة أو حركة رأس خفيفة (اختياري لكنه موصى به).

• **باب احتياطي لا يُغلق عليك أبدًا:** كلمة مرور تُحفظ بصمتها (scrypt) وحدها.

• **بوابة الأوامر الخطيرة:** عند تفعيلها، فتح البوّابة يمرّ عبر `core/confirm`
  الحقيقي في مشروعك — فلا يُنفَّذ أمر مؤثر إلا وأنت أمام الشاشة.

🔴 ثلاث حقائق معلَنة لا تُخفي: هذا **قفل راحة لا خزنة** · الكاميرا العادية **تُخدع
بصورة مطبوعة** إن لم يكن فحص الحياة مفعّلًا · و**كلمة المرور الاحتياطية إلزامية**.

لا سطر يرمي استثناءً للخارج: كل ردّ نصّ، وكل فشل يُعلن السبب.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

PLUGIN = {
    "name": "face_auth",
    "description": (
        "Face verification for the assistant with the EDITH mesh effect: train your face from the "
        "camera (four accepted shots, quality-gated, threshold calibrated from your own data), then "
        "verify yourself — a 225-point wire mesh warps onto your features with a scan line from "
        "forehead to chin, and it turns green with WELCOME BACK, SIR when you are recognised. Use "
        "for: 'train my face', 'register my face', 'scan my face', 'is it me', 'open the face "
        "verification', 'watch my face', 'who is this', 'set a fallback password', 'change the "
        "face threshold', 'lock the screen now', 'watch the camera for my absence' (the guard), 'delete my face profile'. Locks are comfort, not a vault: a printed photo "
        "can fool a plain camera, so liveness (a blink or a small head movement) is available and a "
        "fallback password always exists."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("status | setup | train | verify | watch | password_set | password_check | "
                                "schedule | confirm_mode | liveness | lock | guard | clear | help"),
            },
            "mode": {"type": "STRING", "description": "verify/watch: gallery (same window, one pace) or video."},
            "timeout_s": {"type": "STRING", "description": "Seconds allowed before giving up (default 45)."},
            "password": {"type": "STRING", "description": "Fallback password (password_set, or the escape hatch during verify)."},
            "on": {"type": "STRING", "description": "yes/no for confirm_mode and liveness."},
            "minutes_before": {"type": "STRING", "description": "For schedule: reminder lead time (default 5)."},
            "test_image": {"type": "STRING", "description": "Path to an image file to test detection on, instead of the camera."},
            "absent_s": {"type": "STRING", "description": "Guard: seconds without your face before it acts (default 180)."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "face_auth",
    "title": "Face verification (EDITH) / التحقق من الوجه",
    "fields": [
        {"key": "camera_index", "type": "text", "label": "Camera number (0 = first)", "default": "0"},
        {"key": "backend", "type": "choice", "label": "Camera backend", "default": "auto", "choices": ["auto", "dshow"]},
        {"key": "det_size", "type": "text", "label": "Detector input size (320 is what we calibrated)", "default": "320"},
        {"key": "gate_min_face", "type": "text", "label": "Smallest accepted face side, pixels (training)", "default": "140"},
        {"key": "shots_needed", "type": "text", "label": "Accepted shots needed to train", "default": "4"},
        {"key": "shot_interval_s", "type": "text", "label": "Seconds between accepted shots (so you see the counter)", "default": "1"},
        {"key": "threshold_override", "type": "text", "label": "Force a threshold (blank = calibrated from your data)", "default": ""},
        {"key": "liveness", "type": "choice", "label": "Liveness check", "default": "motion",
         "choices": ["motion", "blink", "either", "off"]},
        {"key": "liveness_s", "type": "text", "label": "Seconds allowed for the liveness gesture", "default": "12"},
        {"key": "ok_hold_s", "type": "text", "label": "How long the green accepted frame stays", "default": "1.5"},
        {"key": "confirm_gate", "type": "choice", "label": "Open through the project's confirm gate (core/confirm)", "default": "on",
         "choices": ["on", "off"]},
        {"key": "watch_mode", "type": "choice", "label": "After a good verify, keep watching (q quits)", "default": "off",
         "choices": ["off", "on"]},
    ],
}

_C = None
F = None


def _boot():
    """يحمّل النواة المشتركة للحزمة والمكتبة المشتركة للوجه — مرة واحدة."""
    global _C, F
    if F is not None:
        return
    core_name = "_jarvis_orch_core"
    if core_name in sys.modules:
        _C = sys.modules[core_name]
    else:
        path = Path(__file__).resolve().parent / "_orch_core.py"
        if path.exists():
            spec = importlib.util.spec_from_file_location(core_name, path)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                sys.modules[core_name] = module
                try:
                    spec.loader.exec_module(module)
                    _C = module
                except Exception:
                    sys.modules.pop(core_name, None)
                    _C = None
    face_name = "_jarvis_face_core"
    if face_name in sys.modules:
        F = sys.modules[face_name]
        return
    path = Path(__file__).resolve().parent / "_face_core.py"
    if not path.exists():
        return
    spec = importlib.util.spec_from_file_location(face_name, path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[face_name] = module
    try:
        spec.loader.exec_module(module)
        F = module
    except Exception:
        sys.modules.pop(face_name, None)
        F = None


_boot()

_WINDOW = "JARVIS // EDITH FACE AUTH"


def _cfg() -> dict:
    defaults = {f["key"]: f.get("default") for f in PLUGIN_SETTINGS["fields"]}
    if _C is None:
        return defaults
    return _C.cfg("face_auth", PLUGIN_SETTINGS["fields"])


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(value).strip())))
    except Exception:
        return default


def _float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(str(value).strip())))
    except Exception:
        return default


def _yes(value: Any, default: bool) -> bool:
    if _C is not None:
        return _C.parse_bool(value, default)
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "on", "نعم", "شغل", "شغّل")


def _log(player, text: str) -> None:
    try:
        if player is not None and hasattr(player, "write_log"):
            player.write_log(text)
    except Exception:
        pass


def _say(text: str) -> str:
    return text


# ── الحالة ────────────────────────────────────────────────────────────────────
def _act_status(params: dict, player) -> str:
    del params
    lines = ["سيدي، حالة التحقق من الوجه:"]
    if F is None:
        lines.append("  ✗ مكتبة الوجه `_face_core.py` غير موجودة بجوار هذا الملف — انسخها معه.")
        return "\n".join(lines)
    ok, why = F._cv2_status()
    lines.append(f"  البيئة: {'opencv و numpy جاهزتان' if ok else why}")
    ready, note = F.models_ready()
    lines.append(f"  النماذج: {'جاهزة في config/models' if ready else note}"
                 + ("" if ready else "  →  face_auth action=setup"))
    profile, vector, why_profile = F.profile_load()
    if profile is None:
        lines.append(f"  البصمة: {why_profile}")
    else:
        lines.append(f"  البصمة: موجودة — عتبتك {profile.get('threshold')} "
                     f"(أدنى تشابه لك {profile.get('min_self')} · جودة التسجيل {profile.get('quality')}٪ · "
                     f"{profile.get('samples')} لقطة · {profile.get('model')})")
    lines.append(f"  كلمة المرور الاحتياطية: {'محفوظة ✓ (وهذا الباب لا يُغلق عليك)' if F.password_exists() else 'غير محفوظة ← اضبطها: action=password_set password=…'}")
    lines.append(f"  فحص الحياة: {_cfg().get('liveness')} · مدة السماح {_cfg().get('liveness_s')}s")
    lines.append(f"  بوابة التأكيد (core/confirm): {'مفعّلة' if _yes(_cfg().get('confirm_gate'), True) else 'مطفأة'}"
                 + ("" if _C is not None else " — ونواة الحزمة غائبة، فالفتح سيكون مباشرًا مع تنبيه"))
    lines.append("  الكاميرا: رقم " + str(_cfg().get('camera_index')) + " · خلفية " + str(_cfg().get('backend')))
    lines.append("  🔴 تذكير: قفل راحة لا خزنة · صورة مطبوعة تخدع كاميرا بلا فحص حياة · وكلمة المرور هي بابك الأخير دائمًا.")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_setup(params: dict, player) -> str:
    del params
    if F is None:
        return "سيدي، مكتبة الوجه غير موجودة."
    ok, why = F._cv2_status()
    if not ok:
        return f"سيدي، {why}\n  (المشروع يثبّت opencv-python أصلًا ضمن متطلباته، فأعد تشغيل التثبيت إن نقصت.)"
    done, note = F.download_models()
    if not done:
        return f"سيدي، تعذّر تجهيز النماذج: {note}"
    out = (f"سيدي، النماذج جاهزة: {note}\n"
           f"  المكان: {F.MODELS_DIR}\n"
           f"  الخطوة التالية: action=train — اجلس أمام الكاميرا وسأعدّ اللقطات المقبولة.")
    _log(player, out)
    return out


# ── حلقة الكاميرا: تدريب ──────────────────────────────────────────────────────
def _camera():
    index = _int(_cfg().get("camera_index"), 0, 0, 20)
    backend = str(_cfg().get("backend") or "auto").lower()
    return F.open_camera(index, backend)


def _det_size() -> tuple[int, int]:
    size = _int(_cfg().get("det_size"), 320, 160, 1280)
    return (size, size)


def _train_loop(player=None, *, test_image: str = "") -> tuple[bool, str]:
    """يجمع لقطات مقبولة ويحفظ البصمة. يعيد (نجح، رسالة)."""
    cv2 = F._cv2()
    shots_needed = _int(_cfg().get("shots_needed"), 4, 2, 8)
    det = _det_size()
    vectors: list[Any] = []
    say_count: dict[str, int] = {}
    cap = None
    static = None
    if test_image:
        static = cv2.imread(str(test_image))
        if static is None:
            return False, f"تعذّرت قراءة الصورة: {test_image}"
    else:
        cap, why = _camera()
        if cap is None:
            return False, why
    started = time.time()
    last_say = ""
    shot_interval = _float(_cfg().get("shot_interval_s"), 1.0, 0.2, 10.0)   # لقطة كل ثانية
    last_shot_at = 0.0
    gate_logged: dict[str, float] = {}
    last_face: Any = None
    last_frame: Any = None
    try:
        while len(vectors) < shots_needed and time.time() - started < 120:
            if static is not None:
                frame = static
            else:
                frame, why = F.read_frame(cap)
                if frame is None:
                    return False, why
            faces, why = F.detect(frame, det)
            if why:
                return False, why
            # اللقطة تُقبل مباشرة إن مرّت البوّابات — بلا انتظار مفتاح
            gate = F.evaluate_shot(frame, faces, {"min_face_px": _int(_cfg().get("gate_min_face"), 140, 60, 600)})
            face = None
            if faces:
                face = max(faces, key=lambda f: float(f["box"][2]) * float(f["box"][3]))
            progress = min(1.0, len(vectors) / float(shots_needed))
            status = "ok" if gate["ok"] else "scan"
            canvas = F.render_effect(frame, face, progress=min(1.0, progress + (time.time() % 1) * 0.05),
                                     status=status, match=progress, threshold=1.0,
                                     phase=time.time(), liveness=f"{len(vectors)}/{shots_needed}")
            cv2.putText(canvas, f"SHOTS {len(vectors)}/{shots_needed}  (Q = abort)", (28, canvas.shape[0] - 22),
                        cv2.FONT_HERSHEY_PLAIN, 1.2, (150, 170, 190), 1, cv2.LINE_AA)
            if not gate["ok"]:
                cv2.putText(canvas, gate["key"].upper(), (28, canvas.shape[0] - 48),
                            cv2.FONT_HERSHEY_PLAIN, 1.2, (70, 70, 235), 1, cv2.LINE_AA)
                # الخطوط لا ترسم العربية، فالرسالة العربية بصيغة الأمر تصل عبر سجل جارفس نفسه
                now_stamp = time.time()
                if now_stamp - gate_logged.get(gate["key"], -99.0) > 1.5:
                    gate_logged[gate["key"]] = now_stamp
                    _log(player, "  • " + gate["say"])
            elif static is None:
                wait_left = shot_interval - (time.time() - last_shot_at)
                if wait_left > 0.05:
                    cv2.putText(canvas, f"NEXT {wait_left:.1f}s", (28, canvas.shape[0] - 48),
                                cv2.FONT_HERSHEY_PLAIN, 1.2, (120, 220, 120), 1, cv2.LINE_AA)
            if static is None:
                try:
                    cv2.imshow(_WINDOW, canvas)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (27, ord("q"), ord("Q")):
                        return False, f"ألغيت التسجيل — بقيت {len(vectors)} لقطة من {shots_needed}، ولم أُغيّر بصمة قديمة."
                except Exception as exc:  # noqa: BLE001
                    return False, f"تعذّر عرض نافذة الكاميرا ({type(exc).__name__}) — شغّل من جلسة سطح مكتب."
            last_face, last_frame = face, frame
            if gate["ok"] and face is not None and (static is not None or time.time() - last_shot_at >= shot_interval):
                vector, why = F.embed(frame, face)
                if vector is not None:
                    vectors.append(vector)
                    last_shot_at = time.time()
                    _log(player, f"  ✓ لقطة {len(vectors)} من {shots_needed} مقبولة.")
                    if static is None:
                        try:
                            cv2.waitKey(60)
                        except Exception:
                            pass
                else:
                    last_say = why
            elif not gate["ok"]:
                last_say = gate["say"]
                say_count[gate["key"]] = say_count.get(gate["key"], 0) + 1
            if static is not None:
                # على صورة ثابتة: نأخذ البصمة مرة واحدة فقط
                if vectors:
                    break
        # وميض أخير: ترى العدّاد وصل 4/4 ويخضرّ المشهد قبل أن تُغلق النافذة
        if static is None and len(vectors) >= shots_needed and last_frame is not None:
            try:
                done = F.render_effect(last_frame, last_face, progress=1.0, status="ok", match=1.0,
                                       threshold=1.0, phase=time.time(), liveness=f"{len(vectors)}/{shots_needed}")
                cv2.putText(done, f"SHOTS {len(vectors)}/{shots_needed}  —  DONE", (28, done.shape[0] - 22),
                            cv2.FONT_HERSHEY_PLAIN, 1.3, (120, 235, 120), 1, cv2.LINE_AA)
                cv2.imshow(_WINDOW, done)
                cv2.waitKey(800)
            except Exception:
                pass
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        if static is None:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
    if len(vectors) < shots_needed:
        reason = ""
        if say_count:
            top = max(say_count, key=lambda key: say_count[key])
            reason = " آخر سبب للرفض: " + F.GATE_SAY.get(top, top)
        else:
            reason = f" ({last_say})" if last_say else ""
        return False, (f"لم تكتمل اللقطات: {len(vectors)} من {shots_needed}." + reason
                       + "\n  حسّن الإضاءة واجلس بحيث يملأ وجهك وسط الإطار، ثم أعد: action=train")
    self_scores = [F.cosine(vectors[i], vectors[j])
                   for i in range(len(vectors)) for j in range(i + 1, len(vectors))]
    # إذا كانت اللقطات نفسها متباعدة (إضاءة/زوايا سيئة) نقولها بصراحة بدل حفظ بصمة رخوة
    if self_scores and min(self_scores) < 0.45:
        return False, (f"لقطاتك متفاوتة كثيرًا (أدنى تطابق بينها {min(self_scores):.2f}) — "
                       f"لم أحفظ شيئًا. أعد التسجيل بإضاءة ثابتة ومواجهة مباشرة.")
    profile = F.calibrate(self_scores, [], safety_frac=F.SAFETY_FRAC)
    override = str(_cfg().get("threshold_override") or "").strip()
    if override:
        try:
            forced = float(override)
            if 0.1 <= forced <= 0.99:
                profile["threshold"] = round(forced, 3)
                profile["forced"] = True
        except Exception:
            pass
    import numpy as _np  # noqa: PLC0415
    stack = _np.stack(vectors, axis=0).astype(_np.float32)
    mean_vector = F.normalize(stack.mean(axis=0))
    if mean_vector is None:
        return False, "تعذّر تكوين المتجه الوسطي."
    profile.update({
        "model": "sface_2021dec", "dim": F.DIM, "det_size": det[0],
        "samples": len(vectors),          # عدد اللقطات فعلًا (المعايرة تحسب الأزواج، وهذا أوضح لك)
        "trained": time.strftime("%Y-%m-%d %H:%M:%S"),
        "min_pair": round(min(self_scores) if self_scores else 0.0, 3),
    })
    saved, why = F.profile_save(profile, mean_vector)
    if not saved:
        return False, why
    out = (f"تمّ التسجيل، سيدي — الجودة {profile['quality']}٪ (من {len(vectors)} لقطات)\n"
           f"  العتبة المعايَرة من بياناتك: {profile['threshold']} "
           f"(الأرضية العامة {F.GENERAL_THRESHOLD}، وأدنى تطابق بين لقطاتك {profile['min_pair']})\n"
           f"  اللقطات المقبولة: {len(vectors)} · القيمة محفوظة في {F.EMBED_FILE.name} بصلاحية 600\n"
           f"  للتحقق: قل «افحص وجهي» أو action=verify")
    F.log("train", "ok", detail={"shots": len(vectors), "threshold": profile["threshold"],
                                 "quality": profile["quality"]})
    _log(player, out)
    return True, out


def _act_train(params: dict, player) -> str:
    if F is None:
        return "سيدي، مكتبة الوجه غير موجودة."
    if not F.password_exists():
        return ("سيدي، لا أدرّبك وأنت بلا باب احتياطي: اضبط كلمة المرور أولًا "
                "action=password_set password=… — لو رفضك وجهك يومًا لن أُغلقك خارج جارفس.")
    ready, why = F.models_ready()
    if not ready:
        return f"سيدي، {why}\n  قل: face_auth action=setup"
    test_image = str(params.get("test_image") or "").strip()
    mode = "صورة ثابتة" if test_image else "الكاميرا"
    _log(player, f"سيدي، أفتح {mode} — اجلس أمامها، ستّ بوّابات تمرّ كل لقطة، وQ للإلغاء.")
    ok, out = _train_loop(player, test_image=test_image)
    return out


# ── حلقة الكاميرا: تحقق ───────────────────────────────────────────────────────
def _verify_loop(player=None, *, timeout_s: float = 45.0, password: str = "",
                 test_image: str = "") -> tuple[bool, str]:
    """يفتح الكاميرا، يطلب فحص الحياة، يقارن بالعتبة، ويُرسم المشهد. يعيد (نجح، رسالة)."""
    cv2 = F._cv2()
    profile, embedding, why = F.profile_load()
    if profile is None or embedding is None:
        return False, why
    threshold = float(profile.get("threshold") or F.GENERAL_THRESHOLD)
    det = _det_size()
    liveness_mode = str(_cfg().get("liveness") or "motion").lower()
    liveness_s = _float(_cfg().get("liveness_s"), 12.0, 4.0, 60.0)
    hold = _float(_cfg().get("ok_hold_s"), 1.5, 0.4, 5.0)
    cap = None
    static = None
    if test_image:
        static = cv2.imread(str(test_image))
        if static is None:
            return False, f"تعذّرت قراءة الصورة: {test_image}"
    else:
        cap, why = _camera()
        if cap is None:
            return False, why
    blink, motion = F.BlinkTracker(), F.MotionTracker()
    started = time.time()
    liveness_started: Optional[float] = None
    best = 0.0
    outcome = ""
    message = ""
    try:
        while time.time() - started < max(5.0, timeout_s):
            if static is not None:
                frame = static
            else:
                frame, why = F.read_frame(cap)
                if frame is None:
                    return False, why
            faces, why = F.detect(frame, det)
            if why:
                return False, why
            face = None
            if faces:
                # عند التحقق: أكبر وجه يكفي — لا بوّابات تدريب هنا (العتبة وحدها تقرّر)
                face = max(faces, key=lambda f: float(f["box"][2]) * float(f["box"][3]))
            other = None
            if faces and face is not None:
                frame_area = float(frame.shape[0] * frame.shape[1])
                biggest_area = float(face["box"][2]) * float(face["box"][3])
                for candidate in faces:
                    if candidate is face:
                        continue
                    area = float(candidate["box"][2]) * float(candidate["box"][3])
                    if area / frame_area >= float(F.DEFAULTS["min_area_frac"]) and area >= 0.8 * biggest_area:
                        other = candidate
                        break
            liveness_state = "waiting"
            live_ok = liveness_mode == "off"
            similarity = 0.0
            if face is not None and other is None:
                if liveness_mode != "off":
                    if liveness_started is None:
                        liveness_started = time.time()
                    if liveness_mode in ("motion", "either"):
                        live_ok = motion.update(frame, face)
                        liveness_state = "move head" if not live_ok else "ok"
                    if not live_ok and liveness_mode in ("blink", "either"):
                        live_ok = blink.update(frame, face)
                        liveness_state = "blink" if not live_ok else "ok"
                    if not live_ok and (time.time() - liveness_started) > liveness_s:
                        outcome = "lost"
                        message = ("لم أرَ لك حركة ولا رمشة — أعد المحاولة وتحرّك قليلًا أو ارمش مرة، "
                                   "أو أطفئ فحص الحياة إن كنت واثقًا من مكانك")
                        break
                if live_ok:
                    vector, why = F.embed(frame, face)
                    if vector is not None:
                        similarity = F.cosine(vector, embedding)
                        best = max(best, similarity)
                        decision = F.decide(similarity, profile)
                        if decision["ok"]:
                            outcome = "ok"
                        else:
                            outcome = "not_me"
            canvas = F.render_effect(frame, face, progress=min(1.0, (time.time() - started) / 2.5),
                                     status=("ok" if outcome == "ok" else ("fail" if outcome == "not_me" else "scan")),
                                     match=max(similarity, best), threshold=threshold,
                                     phase=time.time(), liveness=liveness_state)
            if static is None:
                try:
                    cv2.imshow(_WINDOW, canvas)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (27, ord("q"), ord("Q")):
                        outcome = "cancelled"
                        break
                except Exception as exc:  # noqa: BLE001
                    return False, f"تعذّر عرض نافذة الكاميرا ({type(exc).__name__}) — شغّل من جلسة سطح مكتب."
            if outcome == "ok":
                if static is None:
                    end = time.time() + hold
                    while time.time() < end:
                        F.read_frame(cap)
                        try:
                            cv2.waitKey(30)
                        except Exception:
                            break
                break
            if static is not None:
                break
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        if static is None:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
    if outcome == "ok":
        F.log("verify", "ok", detail={"similarity": round(best, 3), "threshold": threshold})
        return True, ("مرحبًا بعودتك، سيدي — وجهك مطابق "
                      f"(تشابه {best:.3f} فوق عتبتك {threshold:.3f}).")
    if outcome == "cancelled":
        return False, "ألغيت المسح، سيدي. لم يُفتح شيء ولم يتغير شيء."
    if outcome == "not_me":
        F.log("verify", "denied", detail={"best": round(best, 3), "threshold": threshold})
        text = (f"سيدي، الوجه غير مطابق — أعلى تشابه {best:.3f} وعتبتك {threshold:.3f}. "
                f"لم يُفتح شيء.")
    elif outcome == "lost":
        F.log("verify", "failed", error=message)
        text = f"سيدي، {message}."
    else:
        F.log("verify", "failed", error="no face seen in time")
        text = f"سيدي، لم أرَ وجهًا واضحًا في {int(timeout_s)} ثانية. اقترب من الكاميرا وأضِف ضوءًا."
    if password and F.password_exists() and F.password_check(password):
        F.log("verify", "ok", error="", detail={"via": "fallback password"})
        return True, "سيدي، فتحتُ بالباب الاحتياطي (كلمة المرور) لأن وجهك لم يمرّ — تفضّل."
    if F.password_exists() and not password:
        text += "\n  وإن كنت أنت: قل كلمة المرور الاحتياطية وأفتح لك فورًا."
    return False, text


def _ensure_open(mode: str, *, timeout_s: float, password: str, player=None) -> str:
    """يقرّر: تدريب أم تحقق، ويمرّ عبر بوابة المشروع إن كانت مفعّلة. يعيد نصًّا للمستخدم."""
    if F is None:
        return "سيدي، مكتبة الوجه `_face_core.py` غير موجودة بجوار هذا الملف."
    ready, why = F.models_ready()
    if not ready:
        return f"سيدي، {why}\n  قل: face_auth action=setup (تنزيل مرة واحدة ~38 م.ج)."
    profile, vector, why = F.profile_load()
    if profile is None or vector is None:
        return (f"سيدي، {why}\n  ولن يُقفلك هذا أبدًا: الباب الاحتياطي هو كلمة المرور، "
                f"والتحقق كله اختياري.")
    gate_on = _yes(_cfg().get("confirm_gate"), True) and _C is not None
    if gate_on:
        return _C.confirm_request(
            key=f"face_verify_{int(time.time())}",
            title="التحقق من الوجه — EDITH",
            detail=("ستُفتح نافذة الكاميرا للتحقق من وجهك، وتُقارن البصمة محليًا على جهازك.\n"
                    f"العتبة: {profile.get('threshold')} · فحص الحياة: {_cfg().get('liveness')}\n"
                    "لا شيء يُرسل إلى أي شبكة. وافق لتُفتح النافذة."),
            run=lambda: _open_and_report(mode, timeout_s=timeout_s, password=password, player=player),
            player=player,
        )
    return _open_and_report(mode, timeout_s=timeout_s, password=password, player=player)


def _open_and_report(mode: str, *, timeout_s: float, password: str, player=None) -> str:
    """الفتح الفعلي: تحقق، وإن نجح ووُجد جدول قريب (ذكر فيه 'وجه') نُذكّر."""
    ok, text = _verify_loop(player, timeout_s=timeout_s, password=password)
    if ok:
        if _yes(_cfg().get("watch_mode"), False) and mode != "video" and _C is not None:
            return text
        upcoming = _upcoming_face_events()
        if upcoming:
            text += "\n  وللعلم: " + upcoming
    _log(player, text)
    return text


def _act_verify(params: dict, player) -> str:
    mode = str(params.get("mode") or "gallery").strip().lower()
    if mode in ("غرفة", "معرض", "تفعيل"):
        mode = "gallery"
    if mode not in ("gallery", "video", "watch"):
        mode = "gallery"
    timeout_s = _float(params.get("timeout_s"), 45.0, 5.0, 300.0)
    password = str(params.get("password") or "")
    test_image = str(params.get("test_image") or "").strip()
    if test_image:
        ok, out = _verify_loop(player, timeout_s=timeout_s, password=password, test_image=test_image)
        _log(player, out)
        return out
    if mode == "video":
        return _ensure_open("video", timeout_s=timeout_s, password=password, player=player)
    return _ensure_open("gallery", timeout_s=timeout_s, password=password, player=player)


def _watch_only(player=None, *, timeout_s: float = 0) -> str:
    """عرض حيّ للشبكة بلا جمع ولا قرار (Q للخروج)."""
    cv2 = F._cv2()
    cap, why = _camera()
    if cap is None:
        return why
    profile, _vector, _why = F.profile_load()
    threshold = float((profile or {}).get("threshold") or F.GENERAL_THRESHOLD)
    det = _det_size()
    deadline = time.time() + timeout_s if timeout_s else 0
    try:
        while True:
            frame, why = F.read_frame(cap)
            if frame is None:
                return why
            faces, _ = F.detect(frame, det)
            face = max(faces, key=lambda f: float(f["box"][2]) * float(f["box"][3])) if faces else None
            canvas = F.render_effect(frame, face, progress=(time.time() * 0.7) % 1.0, status="scan",
                                     match=0.0, threshold=threshold, phase=time.time(), liveness="watching")
            try:
                cv2.imshow(_WINDOW, canvas)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q"), ord("Q")):
                    break
            except Exception:
                break
            if deadline and time.time() > deadline:
                break
    finally:
        try:
            cap.release()
            cv2.destroyAllWindows()
        except Exception:
            pass
    del player
    return "سيدي، أوقفت العرض." if timeout_s else "سيدي، أوقفت العرض المباشر."


def _act_watch(params: dict, player) -> str:
    test_image = str(params.get("test_image") or "").strip()
    if test_image:
        return _act_verify({"test_image": test_image, "mode": "video"}, player)
    if F is None:
        return "سيدي، مكتبة الوجه غير موجودة."
    profile, vector, why = F.profile_load()
    if profile is None or vector is None:
        return _act_train(params, player)      # غير مدرَّب → التدريب أولًا
    timeout_s = _float(params.get("timeout_s"), 45.0, 5.0, 300.0)
    return _ensure_open("watch", timeout_s=timeout_s, password=str(params.get("password") or ""), player=player)


# ── كلمة المرور الاحتياطية ────────────────────────────────────────────────────
def _act_password_set(params: dict, player) -> str:
    if F is None:
        return "سيدي، مكتبة الوجه غير موجودة."
    password = str(params.get("password") or "").strip()
    if not password:
        return "سيدي، قل كلمة المرور بعد action=password_set password=… وستُحفظ بصمتها فقط، لا الكلمة."
    ok, out = F.password_set(password)
    if not ok:
        return f"سيدي، {out}"
    text = (f"سيدي، {out}\n"
            f"  الباب الاحتياطي صار عندك — وهذا لا يُغلق عليك أبدًا حتى لو رفضك الضوء أو اللحية أو النظارة.\n"
            f"  ⚠️ لا أكرر الكلمة هنا، ولا تُكتب في أي سجل: إن نسيتها اضبطها من جديد.")
    _log(player, text)
    return text


def _act_password_check(params: dict, player) -> str:
    if F is None:
        return "سيدي، مكتبة الوجه غير موجودة."
    if not F.password_exists():
        return "سيدي، لا كلمة مرور محفوظة بعد."
    given = str(params.get("password") or "")
    ok = F.password_check(given)
    out = "سيدي، الكلمة صحيحة — الباب الاحتياطي يعمل." if ok else "سيدي، الكلمة غير صحيحة."
    del player
    return out


# ── الجدولة · فحص الحياة · بوابة التأكيد ──────────────────────────────────────
def _upcoming_face_events() -> str:
    """يقرأ تقويمك المحلي: أي حدث عنوانه يذكر 'وجه' خلال الأيام القادمة.

    صراحةً: الجدولة **بالعنوان فقط** — لا قفل على حدث لم يُعلن، ولا تخمين.
    """
    if _C is None:
        return ""
    try:
        data = _C.load_json(_C.CALENDAR_FILE, {"items": {}})
        items = data.get("items", {}) if isinstance(data, dict) else {}
        now = _C.now()
        hits = []
        for event in items.values():
            title = str(event.get("title", ""))
            if not any(word in title for word in ("وجه", "face", "Face", "FACE")):
                continue
            start = _C.from_iso(event.get("start"))
            if start and now <= start <= now + __import__("datetime").timedelta(days=7):
                hits.append(f"{title} — {event.get('start')}")
        return "أجد مواعيد في تقويمك عنوانها يخصّ الوجه: " + "، ".join(hits[:4]) if hits else ""
    except Exception:
        return ""


def _act_schedule(params: dict, player) -> str:
    """تذكير تقويمي قبل موعد عنوانه يذكر «وجه» — يُنشأ كأتمتة حقيقية واحدة لكل موعد."""
    if F is None or _C is None:
        return "سيدي، هذا الفعل يحتاج نواة الحزمة (`_orch_core.py`) بجوار الملف."
    minutes = _int(params.get("minutes_before"), 5, 1, 240)
    upcoming = _upcoming_face_events()
    if not upcoming:
        return ("سيدي، لا أحداث في تقويمك خلال أسبوع تحمل كلمة «وجه» في عنوانها. "
                "أضِف الموعد أولًا (calendar_events action=add title='فتح الجلسة وجه' …) ثم أعِد الطلب. "
                "ولن أجدول شيئًا بالظنّ.")
    if _C.resolve_plugin("automation_engine") is None:
        return "سيدي، محرّك الأتمتة غير موجود، فلا أستطيع خلق تنبيه حقيقي."
    events = []
    try:
        data = _C.load_json(_C.CALENDAR_FILE, {"items": {}})
        now = _C.now()
        for key, event in (data.get("items", {}) or {}).items():
            title = str(event.get("title", ""))
            if not any(word in title for word in ("وجه", "face", "Face", "FACE")):
                continue
            start = _C.from_iso(event.get("start"))
            if start and now <= start <= now + __import__("datetime").timedelta(days=7):
                events.append((key, event, start))
    except Exception:
        events = []
    created, failed = [], []
    for key, event, start in events:
        at = start - __import__("datetime").timedelta(minutes=minutes)
        if at <= _C.now():
            continue
        steps = json.dumps([{
            "name": "تنبيه", "plugin": "notification_center",
            "parameters": {"action": "notify", "title": "تذكير: التحقق من الوجه",
                           "body": f"{event.get('title')} بعد {minutes} دقيقة",
                           "category": "face_auth", "priority": "info"}}], ensure_ascii=False)
        ok, out = _C.call_plugin("automation_engine", {
            "action": "create", "name": f"تذكير وجه: {event.get('title')}",
            "trigger": json.dumps({"kind": "once", "at": at.strftime("%Y-%m-%d %H:%M")}),
            "steps": steps})
        (created if ok else failed).append(f"{event.get('title')} ({at.strftime('%H:%M')})" if ok else str(out)[:100])
    lines = ["سيدي، جدولة التحقق من الوجه — بالعنوان فقط، بلا تخمين:"]
    if created:
        lines.append("  ✔ أُنشئت أتمتات تنبيه قبل: " + "، ".join(created) + f" بـ{minutes} دقيقة.")
        lines.append("  يشغّلها المجدول — تأكّد أنه يعمل: workflow_scheduler action=start")
    if failed:
        lines.append("  ✗ تعذّر: " + "، ".join(failed))
    if not created and not failed:
        lines.append("  كل المواعيد صارت قريبة (التنبيه يحتاج وقتًا قبلها) — لم أُنشئ شيئًا.")
    out = "\n".join(lines)
    _log(player, out)
    return out


def _act_confirm_mode(params: dict, player) -> str:
    on = _yes(params.get("on"), True)
    if _C is None:
        return "سيدي، نواة الحزمة غير موجودة، فلا أستطيع تغيير الإعداد هنا."
    ok = _C.store_settings("face_auth", {"confirm_gate": "on" if on else "off"})
    if not ok:
        return "سيدي، تعذّر حفظ الإعداد."
    text = ("سيدي، بوابة التأكيد صارت مفعّلة: أي «افتح بالوجه» سيمرّ أولًا عبر بوابة "
            "core/confirm الحقيقية في مشروعك — لن يُفتح شيء إلا بضغطتك على الشاشة."
            if on else "سيدي، أوقفت بوابة التأكيد — الفتح سيكون مباشرًا (مع كل التذكيرات القائمة).")
    _log(player, text)
    return text


def _act_liveness(params: dict, player) -> str:
    if _C is None:
        return "سيدي، نواة الحزمة غير موجودة."
    value = str(params.get("on") or "").strip().lower()
    if value in ("yes", "on", "1", "نعم", "مفعّل", "مفعل"):
        value = "motion"
    if value in ("no", "off", "0", "لا"):
        value = "off"
    if value not in ("motion", "blink", "either", "off"):
        return "سيدي، القيم: motion · blink · either · off"
    _C.store_settings("face_auth", {"liveness": value})
    meanings = {
        "motion": "حركة رأس خفيفة تكفي",
        "blink": "أطلب رمشة (بمراقبة إضاءة العين — قد تحتاج إضاءة جيدة)",
        "either": "أي منهما",
        "off": "بلا فحص حياة — أقولها بصراحة: كاميرا بلا فحص حياة تخدعها صورة مطبوعة",
    }
    out = f"سيدي، فحص الحياة: {value} — {meanings[value]}"
    _log(player, out)
    return out


def _act_clear(params: dict, player) -> str:
    return _clear_confirm(player)


def _clear_confirm(player=None) -> str:
    if F is None:
        return "سيدي، مكتبة الوجه غير موجودة."
    if _C is not None:
        return _C.confirm_request(
            key=f"face_clear_{int(time.time())}",
            title="حذف بصمة وجهي؟",
            detail=("سيُحذف المتجه والملف الشخصي (العتبة والأرقام). "
                    "كلمة المرور الاحتياطية تبقى ما لم تحذفها بنفسك. لا يُحذف أي شيء غير هذا."),
            run=lambda: F.profile_delete()[1],
            player=player,
        )
    return F.profile_delete()[1]


# ── الباب الثالث: قفل الشاشة ومراقب الغياب ────────────────────────────────────
_GUARD: dict[str, Any] = {"thread": None, "stop": False, "state": "off", "detail": ""}
_GUARD_LOCK = threading.Lock()


def _lock_workstation() -> tuple[bool, str]:
    """يقفل شاشة ويندوز عبر ctypes — بلا subprocess وبلا أي أمر خارجي، ولا يرمي أبدًا."""
    if not sys.platform.startswith("win"):
        return False, "قفل الشاشة مدعوم على ويندوز فقط — هذه البيئة ليست ويندوز، فلم أفعل شيئًا."
    try:
        import ctypes  # noqa: PLC0415
        if ctypes.windll.user32.LockWorkStation():
            return True, "قُفلت شاشة ويندوز."
        return False, "ويندوز رفض أمر القفل."
    except Exception as exc:  # noqa: BLE001
        return False, f"تعذّر قفل الشاشة ({type(exc).__name__})."


def _notify(title: str, body: str, priority: str = "info") -> None:
    """يُنشئ إشعارًا حقيقيًا عبر notification_center إن وُجد — وبصمت إن لم يوجد."""
    if _C is None:
        return
    try:
        if _C.resolve_plugin("notification_center") is None:
            return
        _C.call_plugin("notification_center", {"action": "notify", "title": title, "body": body,
                                               "category": "face_auth", "priority": priority})
    except Exception:
        pass


def _do_lock() -> str:
    ok, note = _lock_workstation()
    F.log("lock", "ok" if ok else "failed", error="" if ok else note)
    return f"سيدي، {note}"


def _act_lock(params: dict, player) -> str:
    del params
    if F is None:
        return "سيدي، مكتبة الوجه غير موجودة."
    if _C is not None:
        return _C.confirm_request(
            key=f"face_lock_{int(time.time())}",
            title="قفل الشاشة؟",
            detail=("سأقفل شاشة ويندوز الآن. لا سلطة لي على شاشة الدخول: "
                    "فتحها من عند ويندوز نفسه، لا بوجهي — وأقولها بصراحة."),
            run=_do_lock, player=player)
    return _do_lock()


def _guard_loop(absent_s: float, lock_on: bool) -> None:
    """خيط المراقبة: يرصد وجودك ويقفل عند غيابك الطويل. لا يرمي أبدًا ولا يعطّل جارفس."""
    try:
        profile, embedding, _why = F.profile_load()
        threshold = float((profile or {}).get("threshold") or F.GENERAL_THRESHOLD)
        cap, why = _camera()
        if cap is None:
            _GUARD["state"] = "camera_error"
            _GUARD["detail"] = why
            F.log("guard", "failed", error=why)
            return
        det = _det_size()
        absent_since = time.time()
        present = False
        welcomed = False
        locked = False
        try:
            while not _GUARD["stop"]:
                frame, _why = F.read_frame(cap)
                if frame is None:
                    time.sleep(0.8)
                    continue
                faces, _e = F.detect(frame, det)
                now = time.time()
                if faces:
                    absent_since = now
                    if not present:
                        present = True
                        who = "أرى وجهًا أمام الشاشة."
                        if embedding is not None:
                            biggest = max(faces, key=lambda f: float(f["box"][2]) * float(f["box"][3]))
                            vector, _err = F.embed(frame, biggest)
                            if vector is not None:
                                score = F.cosine(vector, embedding)
                                if F.decide(score, profile or {"threshold": threshold})["ok"]:
                                    who = "مرحبًا بعودتك، سيدي."
                                    if not welcomed:
                                        welcomed = True
                                        _notify("عاد سيدي", "الوجه مطابق — الجلسة تحت أمرك.", "info")
                                        F.log("guard", "ok", detail={"event": "return", "score": round(score, 3)})
                                else:
                                    F.log("guard", "ok", detail={"event": "stranger", "score": round(score, 3)})
                        _GUARD["state"] = "present"
                        _GUARD["detail"] = who
                    locked = False
                else:
                    present = False
                    welcomed = False
                    _GUARD["state"] = "away" if (now - absent_since) >= absent_s else "watching"
                    if (now - absent_since) >= absent_s and not locked:
                        locked = True
                        if lock_on:
                            ok, note = _lock_workstation()
                            F.log("guard", "ok" if ok else "failed", error="" if ok else note)
                            _GUARD["detail"] = ("لا وجه أمام الشاشة منذ " + str(int(absent_s)) + " ثانية — " + note)
                            if ok:
                                _notify("قفلتُ الشاشة", note + " فتحها من ويندوز عند عودتك.", "info")
                        else:
                            F.log("guard", "ok", detail={"event": "away", "absent_s": int(absent_s)})
                            _GUARD["detail"] = f"لا وجه أمام الشاشة منذ {int(absent_s)} ثانية (القفل مطفأ)."
                time.sleep(0.6)
        finally:
            try:
                cap.release()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        _GUARD["state"] = "error"
        _GUARD["detail"] = str(exc)[:160]
        try:
            F.log("guard", "failed", error=str(exc)[:160])
        except Exception:
            pass


def _act_guard(params: dict, player) -> str:
    """ثلاث حالات: بلا on = أخبرني بالحالة · on=yes = شغّل · on=no = أوقف."""
    raw_on = params.get("on")
    absent_s = _float(params.get("absent_s"), 180.0, 1.0, 3600.0)
    lock_on = _yes(params.get("lock"), True)
    thread = None
    with _GUARD_LOCK:
        running = _GUARD["thread"] is not None and _GUARD["thread"].is_alive()
        if raw_on is None or str(raw_on).strip() == "":
            if not running:
                out = ("سيدي، المراقب متوقف الآن (لم أفعل شيئًا في الخلفية). "
                       "لتشغيله: action=guard on=yes absent_s=180 lock=yes")
            else:
                out = (f"سيدي، المراقب يعمل — حالته: {_GUARD['state']}"
                       + (f" ({_GUARD['detail']})" if _GUARD["detail"] else "")
                       + "\n  لإيقافه: action=guard on=no")
            _log(player, out)
            return out
        if not _yes(raw_on, True):
            if not running:
                return "سيدي، المراقب متوقف أصلًا."
            _GUARD["stop"] = True
            thread = _GUARD["thread"]
            _GUARD["thread"] = None
            _GUARD["state"] = "off"
        else:
            if running:
                return (f"سيدي، المراقب يعمل بالفعل — حالته الآن: {_GUARD['state']}"
                        + (f" ({_GUARD['detail']})" if _GUARD["detail"] else "")
                        + "\n  لإيقافه: action=guard on=no")
            if F is None:
                return "سيدي، مكتبة الوجه غير موجودة."
            _GUARD["stop"] = False
            _GUARD["state"] = "starting"
            _GUARD["detail"] = ""
            thread = threading.Thread(target=_guard_loop, args=(absent_s, lock_on),
                                      name="jarvis_face_guard", daemon=True)
            _GUARD["thread"] = thread
            thread.start()
    if thread is None:
        return "سيدي، لم أغيّر شيئًا."
    if not _yes(raw_on, True):
        try:
            thread.join(timeout=3.0)
        except Exception:
            pass
        out = "سيدي، أوقفت المراقب وأغلقت الكاميرا معه. لا شيء يعمل في الخلفية الآن."
        F.log("guard", "ok", detail={"event": "stopped"})
        _log(player, out)
        return out
    out = (f"سيدي، المراقب يعمل الآن: أراقب الكاميرا، وإن غاب وجهك {int(absent_s)} ثانية "
           f"{'أقفل شاشة ويندوز' if lock_on else 'أُسجّل الغياب بلا قفل'}.\n"
           f"  ⚠️ بصراحة: المراقب يعمل فقط بينما جارفس مفتوح · وفتح شاشة ويندوز بعد القفل "
           f"من ويندوز نفسه لا بوجهي · وكلمة المرور الاحتياطية تبقى بابك.\n"
           f"  لإيقافه: action=guard on=no · وللاستفسار: action=guard (بلا on).")
    _log(player, out)
    return out


_HELP = (
    "التحقق من الوجه (إيفيكت EDITH)، سيدي:\n"
    "• status — الحالة والجاهزية والأرقام · setup — تنزيل النموذجين مرة واحدة (~38 م.ج)\n"
    "• password_set password=… — الباب الاحتياطي (إلزامي قبل التدريب) · password_check\n"
    "• train — التسجيل من الكاميرا (٤ لقطات مقبولة، ستّ بوّابات، و«اقترب»/«ثبّت رأسك» تقال لك)\n"
    "• verify — التحقق: الشبكة والمسح من الجبهة إلى الذقن، والأخضر عند القبول\n"
    "   mode=gallery (نافذة واحدة بهدوء) أو video (تملأ الشاشة) · password=… للباب الاحتياطي\n"
    "• watch — عرض مباشر للشبكة، ويسجّل من جديد إن لم تكن مدرَّبًا\n"
    "• liveness on=motion|blink|either|off · confirm_mode on=yes|no\n"
    "• schedule minutes_before=5 — تنبيه قبل أي موعد عنوانه يذكر «وجه» (بالعنوان فقط، لا تخمين)\n"
    "• lock — قفل شاشة ويندوز الآن (بتأكيد) · guard on=yes absent_s=180 lock=yes — مراقب الغياب\n"
    "   (المراقب يعمل فقط بينما جارفس مفتوح، وفتح شاشة ويندوز بعد القفل من ويندوز نفسه)\n"
    "• clear — حذف بصمة وجهي (بتأكيد)\n"
    "🔴 قفل راحة لا خزنة · صورة مطبوعة تخدع كاميرا بلا فحص حياة · وكلمة المرور بابك الأخير دائمًا."
)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """نقطة الدخول. لا ترمي أبدًا."""
    del session_memory
    _boot()
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "status").strip().lower()
        if F is None and action not in ("help", "?"):
            return ("سيدي، مكتبة الوجه `_face_core.py` غير موجودة بجوار `face_auth.py` — "
                    "انسخها معه من الحزمة.")
        if action in ("status", "حالة"):
            return _act_status(p, player)
        if action in ("setup", "download", "جهّز"):
            return _act_setup(p, player)
        if action in ("train", "enroll", "درّب", "سجّل"):
            return _act_train(p, player)
        if action in ("verify", "check", "افحص", "امسح"):
            return _act_verify(p, player)
        if action in ("watch", "live", "راقب"):
            return _act_watch(p, player)
        if action in ("password_set", "set_password", "كلمة_المرور"):
            return _act_password_set(p, player)
        if action in ("password_check",):
            return _act_password_check(p, player)
        if action in ("schedule", "جدولة"):
            return _act_schedule(p, player)
        if action in ("confirm_mode", "بوابة"):
            return _act_confirm_mode(p, player)
        if action in ("liveness", "فحص_الحياة"):
            return _act_liveness(p, player)
        if action in ("lock", "اقفل"):
            return _act_lock(p, player)
        if action in ("guard", "مراقب", "راقب_الغياب"):
            return _act_guard(p, player)
        if action in ("clear", "delete", "احذف"):
            return _act_clear(p, player)
        if action in ("help", "?"):
            return _HELP
        return f"سيدي، '{action}' ليس فعلًا أعرفه في التحقق من الوجه. قل 'help'."
    except Exception as exc:  # noqa: BLE001 — لا ينهار المساعد أبدًا
        return f"سيدي، التحقق من الوجه واجه مشكلة: {str(exc)[:180]}"


# ── فحص ذاتي: python face_auth.py ─────────────────────────────────────────────
def _selftest() -> int:
    """الفحص الذاتي — معزولًا تمامًا عن مخزن المشروع: لا إعدادات ولا سجلات ولا أتمتات حقيقية."""
    restorer = None
    try:
        if _C is not None and hasattr(_C, "isolate_for_tests"):
            restorer = _C.isolate_for_tests()
    except Exception:
        restorer = None
    try:
        return _selftest_body()
    finally:
        if restorer is not None:
            try:
                restorer()
            except Exception:
                pass


def _selftest_body() -> int:
    print("face_auth — self-test")
    problems = 0

    def check(label: str, ok: bool, extra: str = "") -> None:
        nonlocal problems
        print(("  ✓ " if ok else "  ✗ ") + label + (f"  [{extra}]" if extra and not ok else ""))
        if not ok:
            problems += 1

    if F is None:
        print("  ✗ مكتبة الوجه `_face_core.py` غير موجودة")
        return 1
    if _C is None:
        print("  ✗ نواة الحزمة `_orch_core.py` غير موجودة")
        return 1

    import tempfile
    cv2, np = F._cv2(), F._np()
    if cv2 is None or np is None:
        print("  ✗ opencv/numpy غير متاحتين في هذه البيئة")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="face_test_"))
    real_models, real_face = F.MODELS_DIR, F.FACE_DIR
    real_profile, real_embed, real_password = F.PROFILE_FILE, F.EMBED_FILE, F.PASSWORD_FILE
    real_camera, real_logs = _C.CALENDAR_FILE, _C.LOG_DIR
    real_stamp, real_data = _C._SCAN_CACHE.get("stamp"), _C._SCAN_CACHE.get("data")
    base = _C.CONFIG_DIR / "api_keys.json"
    backup = base.read_text(encoding="utf-8") if base.exists() else None
    try:
        F.MODELS_DIR = tmp / "models"
        F.FACE_DIR = tmp / "face"
        F.FACE_DIR.mkdir(parents=True, exist_ok=True)
        F.PROFILE_FILE = F.FACE_DIR / "profile.json"
        F.EMBED_FILE = F.FACE_DIR / "embedding.npy"
        F.PASSWORD_FILE = F.FACE_DIR / "password.json"
        _C.CALENDAR_FILE = tmp / "calendar.json"
        _C.LOG_DIR = tmp / "logs"
        _C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        _C.save_json(_C.CALENDAR_FILE, {"version": 1, "items": {}})
        _C.store_settings("face_auth", {"camera_index": "9", "backend": "auto", "det_size": "320",
                                        "gate_min_face": "140", "shots_needed": "4", "liveness": "off",
                                        "confirm_gate": "off", "watch_mode": "off"})

        # 1) الحالة قبل النماذج: تقول الحقيقة بلا انهيار
        out = run({"action": "status"})
        check("status يعلن نقص النماذج بصراحة", "setup" in out and "ناقص" in out, out[:300])
        check("status يذكّر بالحقائق", "قفل راحة لا خزنة" in out, out[:400])

        # 2) المعايرة من أرقامنا المسجّلة تُعيد 0.526 بالضبط
        cal = F.calibrate([0.740], [0.265])
        check("المعايرة تُعيد عتبتنا 0.526", abs(cal["threshold"] - 0.526) < 1e-6, str(cal))
        check("الجودة 93٪ من متوسط 0.79", F.quality_percent(0.79) == 93, str(F.quality_percent(0.79)))
        check("الهامش يُحسب", cal["margin"] == 0.475, str(cal["margin"]))

        # 3) البوّابات الستّ على إطار اصطناعي — بالترتيب الصحيح
        frame = (np.ones((720, 1280, 3), dtype=np.uint8) * 130)
        frame[:, :] = 130
        rs = np.random.RandomState(3)
        frame[100:700, 300:900] = np.clip(frame[100:700, 300:900] + rs.randint(-40, 40, (600, 600, 3)), 0, 255)
        good_face = {"box": [500.0, 120.0, 380.0, 480.0], "score": 0.95,
                     "landmarks": [[560.0, 300.0], [820.0, 300.0], [690.0, 400.0], [600.0, 470.0], [780.0, 470.0]],
                     "raw": [0.0] * 15}
        gate = F.evaluate_shot(frame, [good_face])
        check("إطار سليم يمرّ", gate["ok"], str(gate))
        small = dict(good_face); small["box"] = [600.0, 200.0, 90.0, 100.0]
        check("وجه صغير → «اقترب»", F.evaluate_shot(frame, [small])["say"] == F.GATE_SAY["too_small"])
        rival = {"box": [900.0, 150.0, 340.0, 430.0], "score": 0.93, "landmarks": good_face["landmarks"], "raw": [0.0] * 15}
        check("وجه ثانٍ مسيطر → رفض", F.evaluate_shot(frame, [good_face, rival])["key"] == "many_faces")
        tiny = {"box": [20.0, 20.0, 30.0, 30.0], "score": 0.9, "landmarks": good_face["landmarks"], "raw": [0.0] * 15}
        check("وجه صغير جدًا مع الكبير لا يرفض (ضجيج يُتجاهل)",
              F.evaluate_shot(frame, [good_face, tiny])["ok"], str(F.evaluate_shot(frame, [good_face, tiny])))
        off = dict(good_face); off["landmarks"] = [[560.0, 300.0], [820.0, 300.0], [850.0, 400.0], [600.0, 470.0], [780.0, 470.0]]
        check("سحب الرأس → «انظر إلى الكاميرا»", F.evaluate_shot(frame, [off])["say"] == F.GATE_SAY["off_axis"],
              str(F.evaluate_shot(frame, [off])["metrics"]))
        dark = (np.ones((720, 1280, 3), dtype=np.uint8) * 20)
        check("إضاءة ضعيفة → «أضِف ضوءًا»", F.evaluate_shot(dark, [good_face])["say"] == F.GATE_SAY["dark"])
        smooth = (np.ones((720, 1280, 3), dtype=np.uint8) * 130)
        flat = F.evaluate_shot(smooth, [good_face])
        check("صورة باهتة → «ثبّت»", flat["say"] == F.GATE_SAY["blur"], str(flat["metrics"]))
        low = dict(good_face); low["score"] = 0.4
        check("ثقة منخفضة → «ثبّت رأسك»", F.evaluate_shot(frame, [low])["key"] == "low_conf")

        # 4) الإيفيكت يرسم فعلًا: 225 نقطة + مسح + لوحة
        points = F._mesh_points(good_face["box"], good_face["landmarks"])
        check("الشبكة 225 نقطة فعلًا", len(points) == 225, str(len(points)))
        out_frame = F.render_effect(frame, good_face, progress=0.5, status="scan", match=0.71,
                                    threshold=0.526, phase=1.0, liveness="waiting")
        check("الإيفيكت يغيّر الصورة وبنفس الأبعاد",
              out_frame.shape == frame.shape and bool((out_frame != frame).any()))
        out_frame = F.render_effect(frame, good_face, progress=1.0, status="ok", match=0.9, threshold=0.526, phase=2.0)
        check("لحظة القبول تُرسم", out_frame is not None and out_frame.shape == frame.shape)

        # 5) كلمة المرور الاحتياطية
        ok, note = F.password_set("كلمة-سرّية-تجريبية-١٢")
        check("حفظ كلمة المرور", ok and F.password_exists(), note)
        check("الكلمة الصحيحة تمرّ", F.password_check("كلمة-سرّية-تجريبية-١٢"))
        check("الكلمة الخطأ تُرفض", not F.password_check("غلط"))
        check("الكلمة القصيرة تُرفض", not F.password_set("123")[0])
        raw_file = (tmp / "face" / "password.json").read_text(encoding="utf-8")
        check("لا تُحفظ الكلمة نفسها في الملف", "كلمة-سرّية-تجريبية-١٢" not in raw_file, raw_file[:80])

        # 6) البصمة: حفظ · قراءة · قرار بالعتبة وحدها
        vector = F.normalize(np.array([1.0] + [0.0] * 127, dtype=np.float32))
        profile = {"threshold": 0.526, "min_self": 0.740, "quality": 93, "samples": 4, "model": "sface_2021dec", "dim": 128}
        saved, why = F.profile_save(profile, vector)
        check("حفظ البصمة", saved, why)
        loaded, loaded_vector, why2 = F.profile_load()
        check("قراءة البصمة", loaded is not None and loaded_vector is not None, why2)
        check("نفس المتجه يعود", loaded_vector is not None and abs(float(loaded_vector[0]) - 1.0) < 1e-4)
        check("قرار 0.9 مع عتبة 0.526 = قبول", F.decide(0.9, profile)["ok"])
        check("قرار 0.4 مع عتبة 0.526 = رفض", not F.decide(0.4, profile)["ok"])
        perms = oct((tmp / "face" / "embedding.npy").stat().st_mode & 0o777)
        check("صلاحية ملف المتجه 600", perms == "0o600", perms)

        # 7) الرفض بسبب عدم التطابق (بدون كاميرا: نُمرّر صورة اختبارية ونمنع النجاح)
        #    عارض وهمي: لا شاشة، وبلا جمع — نختبر منطق الدورة عبر استبدال دالتي العرض
        real_imshow, real_wait, real_destroy = cv2.imshow, cv2.waitKey, cv2.destroyAllWindows
        cv2.imshow = lambda *a, **k: None                     # noqa: E731
        cv2.waitKey = lambda *a, **k: 0xFF                     # noqa: E731
        cv2.destroyAllWindows = lambda *a, **k: None           # noqa: E731
        try:
            real_camera_fn = F.open_camera
            class _FakeCap:
                def isOpened(self): return True
                def set(self, *a): return True
                def read(self): return True, frame.copy()
                def release(self): return None
            F.open_camera = lambda *a, **k: (_FakeCap(), "")  # noqa: E731
            real_detect, real_embed, real_cosine = F.detect, F.embed, F.cosine
            F.detect = lambda fr, *a, **k: ([good_face], "")  # noqa: E731
            F.embed = lambda fr, fc, *a, **k: (vector, "")   # noqa: E731
            F.cosine = lambda a, b: 0.95                      # noqa: E731
            ok, text = _verify_loop(None, timeout_s=5)
            check("دورة تحقق ناجحة تعطي ترحيبًا", ok and "مرحبًا بعودتك" in text, text[:200])
            F.cosine = lambda a, b: 0.05                      # noqa: E731
            ok, text = _verify_loop(None, timeout_s=5)
            check("وجه غريب يُرفض بلا فتح", (not ok) and "غير مطابق" in text, text[:200])
            ok, text = _verify_loop(None, timeout_s=5, password="كلمة-سرّية-تجريبية-١٢")
            check("الباب الاحتياطي يفتح عند الرفض", ok and "الاحتياطي" in text, text[:200])
            ok, text = _verify_loop(None, timeout_s=5, password="كلمة-خطأ")
            check("كلمة مرور خاطئة لا تفتح", not ok and "الاحتياطي" not in text, text[:200])
            F.detect = lambda fr, *a, **k: ([], "")           # noqa: E731
            ok, text = _verify_loop(None, timeout_s=5)
            check("لا وجه في المهلة → رسالة", not ok and "لم أرَ" in text, text[:200])
            F.detect, F.embed, F.cosine = real_detect, real_embed, real_cosine
            F.open_camera = real_camera_fn
        finally:
            cv2.imshow, cv2.waitKey, cv2.destroyAllWindows = real_imshow, real_wait, real_destroy

        # 8) الجدولة بالعنوان فقط
        _C.save_json(_C.CALENDAR_FILE, {"version": 1, "items": {"e1": {
            "id": "e1", "title": "فتح الجلسة بالوجه", "start": _C.to_iso(_C.now() + __import__("datetime").timedelta(hours=6)),
            "duration_min": 30, "recurrence": {"kind": "none"}, "reminders": []}}})
        out = run({"action": "schedule", "minutes_before": "5"})
        check("schedule يجد الحدث بالعنوان ويُنشئ أتمتة", "أُنشئت أتمتات تنبيه" in out, out[:300])
        autos = _C.load_json(_C.AUTOMATIONS_FILE, {}).get("items", {})
        check("الأتمتة محفوظة فعلًا", any("وجه" in str(a.get("name")) for a in autos.values()), str(list(autos))[:120])
        out = run({"action": "schedule"})
        check("إعادة الجدولة لا تكرّر بصمت", "أُنشئت" in out or "صارت قريبة" in out or "تعذّر" in out, out[:200])

        # 9) الأوامر النصّية والحمايات
        check("help يعمل", "EDITH" in run({"action": "help"}))
        check("فعل مجهول", "ليس فعلًا" in run({"action": "zzz"}))
        out = run({"action": "password_check", "password": "غلط"})
        check("password_check يرفض الخطأ", "غير صحيحة" in out, out[:120])
        out = run({"action": "liveness", "on": "blink"})
        check("liveness يضبط النمط", "blink" in out, out[:160])
        out = run({"action": "liveness", "on": "off"})
        check("liveness يقبل الإطفاء مع تحذيره", "صورة مطبوعة" in out, out[:200])
        out = run({"action": "confirm_mode", "on": "no"})
        check("confirm_mode يعمل", "أوقفت" in out, out[:160])
        # التدريب: الباب الاحتياطي أولًا، ثم النماذج
        saved_password = F.PASSWORD_FILE.read_text(encoding="utf-8")
        F.PASSWORD_FILE.unlink()
        out = run({"action": "train"})
        check("لا تدريب بلا باب احتياطي", "لا أدرّبك" in out, out[:200])
        F.PASSWORD_FILE.write_text(saved_password, encoding="utf-8")
        out = run({"action": "train", "test_image": "/no/such/file.png"})
        check("بلا نماذج → يدلّ على setup", "setup" in out or "ناقص" in out, out[:220])
        # 10) الباب الثالث: القفل يمرّ عبر بوابة المشروع، والمراقب يبدأ ويتوقف بنظافة
        out = run({"action": "lock"})
        check("lock يمرّ عبر بوابة التأكيد (وإن لم تكن الواجهة جاهزة لا ينفّذ)",
              ("cannot confirm" in out) or ("تأكيد" in out) or ("مدعوم على ويندوز" in out), out[:200])
        check("هذه البيئة ليست ويندوز فلا يُقفل فعلًا", not sys.platform.startswith("win"))
        out = run({"action": "guard"})
        check("guard بلا on يعلن حالته", "المراقب" in out, out[:200])
        out = run({"action": "guard", "on": "no"})
        check("إيقاف المراقب غير العامل رسالة واضحة", "متوقف أصلًا" in out, out[:160])
        guard_cap_closed = {"n": 0}
        real_camera_fn2 = F.open_camera
        class _GuardCap:
            def isOpened(self): return True
            def set(self, *a): return True
            def read(self): return True, frame.copy()
            def release(self): guard_cap_closed["n"] += 1
        F.open_camera = lambda *a, **k: (_GuardCap(), "")  # noqa: E731
        F.detect = lambda fr, *a, **k: ([], "")            # noqa: E731  # لا وجه: نختبر الغياب
        try:
            out = run({"action": "guard", "on": "yes", "absent_s": "1", "lock": "no"})
            check("تشغيل المراقب", "يعمل الآن" in out, out[:200])
            time.sleep(2.4)
            state = _GUARD["state"]
            check("المراقب رصد الغياب فعلًا", state == "away", f"state={state} detail={_GUARD['detail']}")
            check("الغياب مسجَّل", _GUARD["detail"] != "", str(_GUARD["detail"])[:120])
            out = run({"action": "guard", "on": "no"})
            check("إيقاف المراقب", "أوقفت" in out and _GUARD["thread"] is None and _GUARD["state"] == "off",
                  f"{out[:120]} state={_GUARD['state']}")
            check("الكاميرا أُغلقت عند الإيقاف", guard_cap_closed["n"] >= 1, str(guard_cap_closed))
        finally:
            _GUARD["stop"] = True
            F.open_camera = real_camera_fn2
            F.detect = real_detect
            thread = _GUARD.get("thread")
            if thread is not None:
                try:
                    thread.join(timeout=2.0)
                except Exception:
                    pass
            _GUARD["thread"] = None
            _GUARD["state"] = "off"

        # 11) لا أسرار في السجل
        raw = ""
        for f in _C.LOG_DIR.glob("*.jsonl"):
            raw += f.read_text(encoding="utf-8")
        check("كلمة المرور لا تظهر في أي سجل", "كلمة-سرّية-تجريبية-١٢" not in raw)
        check("السجل يحوي أحداث الوجه", "face_auth" in raw or "denied" in raw, raw[:120])
    finally:
        F.MODELS_DIR, F.FACE_DIR = real_models, real_face
        F.PROFILE_FILE, F.EMBED_FILE, F.PASSWORD_FILE = real_profile, real_embed, real_password
        _C.CALENDAR_FILE, _C.LOG_DIR = real_camera, real_logs
        _C._SCAN_CACHE["stamp"], _C._SCAN_CACHE["data"] = real_stamp, real_data
        if backup is not None:
            base.write_text(backup, encoding="utf-8")
        elif base.exists():
            base.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            _C.scan_plugins(force=True)
        except Exception:
            pass

    print(f"RESULT: {'PASS' if problems == 0 else str(problems) + ' problem(s)'}")
    return problems


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
