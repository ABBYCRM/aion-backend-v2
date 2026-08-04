from fastapi import FastAPI
import os
app = FastAPI()
@app.get("/healthz")
def h():
    return {"ok": True, "openrouter_key_len": len(os.environ.get("OPENROUTER_API_KEY", ""))}
@app.get("/debug")
def d():
    return {k: (v[:30] + "...") if len(v) > 30 else v for k, v in os.environ.items() if k in ["OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "APP_NAME", "AUDIT_LOG_DIR"]}
