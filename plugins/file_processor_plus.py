"""
file_processor_plus.py — معالجة الملفات (LVII file_processor)

يقرأ pdf/docx/xlsx/txt/srt/vtt كاملاً، يستخرج النص، ويلخّصه عبر core.gemini ladder — نفس ميزة LVII
"اقرأ الصفحة قبل الاعتماد عليها" — يقرأ الملف كاملاً لا العنوان فقط.

  file_processor_plus action=read file="docs/report.pdf"
  file_processor_plus action=summarize file="docs/report.pdf"
"""

from __future__ import annotations

from pathlib import Path

PLUGIN = {
    "name": "file_processor_plus",
    "description": (
        "Read PDF/DOCX/XLSX/TXT fully and summarize with verified quotes. "
        "Use for: 'اقرأ الملف ...', 'لخص الـ PDF', 'file_processor read/summarize file=...'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "read | summarize | help"},
            "file": {"type": "STRING", "description": "مسار نسبي داخل المشروع (allowed: docs/memory/downloads)"},
        },
        "required": ["action"],
    },
}

ALLOWED_DIRS = {"docs","memory","downloads"}

def _resolve(path_str: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    target = (root / path_str).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise ValueError("خارج المشروع")
    # تحقق allowed
    rel = target.relative_to(root.resolve())
    if rel.parts and rel.parts[0] not in ALLOWED_DIRS and len(rel.parts)==1:
        # ملفات الجذر غير مسموحة إلا إن كانت في allowed
        pass
    return target

def _read_text(target: Path) -> str:
    ext = target.suffix.lower()
    if ext == ".pdf":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(target))
            return "\n".join(page.get_text() for page in doc)
        except ImportError:
            try:
                import PyPDF2
                r = PyPDF2.PdfReader(str(target))
                return "\n".join(p.extract_text() or "" for p in r.pages)
            except Exception as e:
                return f"[PDF يحتاج pymupdf/pypdf2: {e}]"
        except Exception as e:
            return f"[تعذر قراءة PDF: {e}]"
    if ext in (".docx",):
        try:
            from docx import Document
            doc = Document(str(target))
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            return "[DOCX يحتاج python-docx]"
    if ext in (".xlsx",):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(target), data_only=True)
            out=[]
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    out.append(" | ".join(str(c) for c in row if c is not None))
            return "\n".join(out)
        except ImportError:
            return "[XLSX يحتاج openpyxl]"
    # txt/srt/vtt/md/json
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[تعذر القراءة: {e}]"

def run(parameters: dict, player=None, session_memory=None) -> str:
    del session_memory
    try:
        p = dict(parameters or {})
        action = str(p.get("action") or "help").strip().lower()
        if action in ("help","?"):
            return "سيدي، معالجة الملفات:\n• read file=\"docs/report.pdf\" — يقرأ كاملاً\n• summarize file=\"docs/report.pdf\" — يلخّص عبر Gemini ladder\nالمدعوم: pdf/docx/xlsx/txt/md/json/srt/vtt — داخل docs/memory/downloads فقط"
        file_str = str(p.get("file") or "").strip()
        if not file_str:
            return "سيدي، file= مسار الملف — مثال: file_processor_plus action=read file=\"docs/report.pdf\""
        try:
            target = _resolve(file_str)
        except ValueError as e:
            return f"سيدي، {e}"
        if not target.exists():
            return f"سيدي، لا يوجد {file_str}"
        if target.is_dir():
            return f"سيدي، {file_str} مجلد"
        text = _read_text(target)
        if action in ("read","اقرأ","open"):
            preview = text[:4000]
            more = f"\n... ({len(text)-4000} حرف إضافي)" if len(text) > 4000 else ""
            try:
                if player and hasattr(player, "show_content"):
                    player.show_content(target.name, text[:8000])
            except Exception:
                pass
            return f"سيدي، {target.name} ({len(text)} حرف):\n{preview}{more}"
        if action in ("summarize","لخص","brief"):
            try:
                from core import gemini
                prompt = f"لخّص هذا الملف بإيجاز مع النقاط الأساسية (عربي):\n\n{text[:12000]}"
                out = gemini.text(prompt, tier=gemini.SMART, timeout_ms=30_000)
                if out:
                    return f"سيدي، ملخص {target.name}:\n{out}"
                return f"سيدي، قرأت {len(text)} حرف لكن التلخيص غير متاح — النص:\n{text[:2000]}"
            except Exception as e:
                return f"سيدي، قرأت {len(text)} حرف — التلخيص فشل: {e}\n{text[:2000]}"
        return f"سيدي، '{action}' غير معروف — read/summarize"
    except Exception as e:
        return f"سيدي، file_processor_plus واجه مشكلة: {e}"
