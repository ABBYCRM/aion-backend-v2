import sys
print("BOOT: importing settings", flush=True)
from .settings import settings
print(f"BOOT: settings.app_name={settings.app_name}", flush=True)
from fastapi import FastAPI
app = FastAPI()
@app.get("/healthz")
def h():
    return {"ok": True, "service": settings.app_name, "version": settings.app_version, "env": settings.environment}
