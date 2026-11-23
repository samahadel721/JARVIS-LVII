JARVIS LVII — 100 Plugins / نواة AGI العملية
Mark LVI → LVII + Torvis parity + AGI Core واجهة صوتية عربية "سيدي" — 100 إضافة نشطة — محلي أولاً، آمن افتراضياً
الوصف القصير لـ GitHub (انسخه في About):
JARVIS LVII — مساعد صوتي عربي 100 إضافة | Mark-LVII HUD + Reactor 3D | مهام خلفية مستأنفة | بحث محقق بالكود | Screen Agent | Video Summarizer | AGI Core يخطط ويتعلم | Gmail/Drive متصل | محلي 127.0.0.1 آمن
README — ما هذا؟
JARVIS هو مساعد صوتي محلي مبني على FatihMakes/Mark-LVI ومرقّى إلى معمارية Mark-LVII (HUD + Background Tasks + Research + Routines). كل شيء يعمل على جهازك — لا 0.0.0.0 مفتوح، لا مفاتيح في الكود.
القدرات (100 plugin)
Command Center LVII
🖥️ HUD ثلاثي الأبعاد + Reactor 5.4ms/frame + لوحات TASKS/ROUTINES/LIVE
🔄 مهام خلفية: checkpoint كل جولة، استئناف بعد إعادة التشغيل، cancel/steer بالصوت
🔍 بحث عميق محقّق: يقرأ المصدر كاملاً، يتحقق من الاقتباس بالكود، [?] لغير الموثّق
Torvis parity
👁️ Screen Agent — SEE→THINK→ACT→VERIFY (accessibility tree حقيقي)
🎥 Video Summarizer — transcript يوتيوب → تلخيص + quiz
🤚 Face Lock + Gesture + Holo Desk
🌐 Conversation Translator — ترجمة/دبلجة (لا انتحال صوت)
AGI Core (جديد)
🧠 agi_core think goal="..." → يفكك هدفاً كبيراً إلى شجرة 3-5 مهام ويُشغّلها في الخلفية
📚 agi_learn log/recall → ذاكرة دائمة للدروس (0600 محلية)
🔄 reflect/learn → مراجعة ذاتية وتحسين
Google Workspace (متصل)
📧 Gmail — triage بدون إرسال تلقائي
📁 Drive — gallery + preview في الهود
📊 Sheets/Docs — تصدير التقارير
أمان
127.0.0.1 افتراضي، اقتران 8 أحرف، Bearer 24س، موافقة صريحة للملفات، لا رفع تلقائي
تثبيت
unzip JARVIS_LVII_جاهز.zip
pip install -r JARVIS_LVII/requirements.txt
python JARVIS_LVII/main.py
الاستخدام
يا فالح — فكّر في نظام حجز مواعيد
يا لذيذ — اعرض نبض المفاعل
screen_agent action=teach name="gmail_search"
video_summarizer summarize source="https://youtube.com/watch?v=..."
agi_core action=think goal="صمم متجر كتب"
بنية المجلدات
JARVIS_LVII/
├── agents/ (general.md, researcher.md)
├── core/tasks/ + core/routines/ + gui/hud.py + gui/reactor.py
├── plugins/ (100)
├── mobile/ (PWA)
└── docs/mobile_api.md
MIT • مبني فوق Mark-LVI/LVII • لا مفاتيح ثابتة • كل الإضافات مستقلة
