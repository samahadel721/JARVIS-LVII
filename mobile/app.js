// Mobile PWA — fetch only, no agent logic
async function api(path, opts={}){ const r=await fetch(path, opts); return r.json().catch(()=>r.text()); }
