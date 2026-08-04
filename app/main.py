from fastapi import FastAPI
from .settings import settings
app = FastAPI()
@app.get("/healthz")
def h():
    return {"ok": True, "service": settings.app_name, "version": settings.app_version, "env": settings.environment}
