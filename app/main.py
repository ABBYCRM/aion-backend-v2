import os
from fastapi import FastAPI
app = FastAPI()
@app.get("/healthz")
def h():
    try:
        from . import settings as s
        return {"ok": True, "imported": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}
@app.get("/test_settings")
def t():
    try:
        import os
        os.environ.setdefault("APP_NAME", "AION")
        os.environ.setdefault("OPENROUTER_API_KEY", "test123")
        from .settings import settings
        return {"ok": True, "name": settings.app_name, "key_len": len(settings.openrouter_api_key)}
    except Exception as e:
        import traceback
        return {"ok": False, "err": str(e), "trace": traceback.format_exc()[:500]}
