# Mobile API — LVII
الكمبيوتر يشغّل الثقيل، الموبايل واجهة تحكم فقط. افتراضي 127.0.0.1:8790 (آمن) — لا 0.0.0.0 إلا allow_lan=yes.
- POST /api/auth/pair {code, device_name} → access_token (24h) + refresh_token (30d)
- POST /api/auth/refresh, GET /api/devices, DELETE /api/devices
- GET/POST /api/tasks, POST /api/tasks/{id}/approve, GET /api/sync
- POST /api/files/request → pending → approve → upload_token (مرة واحدة 10د) → POST /api/files/upload
راجع mobile_sync plugin للواجهة على /mobile
