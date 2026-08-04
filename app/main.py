from fastapi import FastAPI
from .llm import AllProvidersFailed, probe, stream_chat
from .audit import audit
from .settings import settings

app = FastAPI()
@app.get("/healthz")
def h():
    return {"ok": True, "service": settings.app_name, "version": settings.app_version}
