"""AION test - just imports."""
from fastapi import FastAPI
from .kernel import AION_CONTINUITY_PACK, MissionContext, DecisionState, build_system_prompt, resolve_decision
from .llm import AllProvidersFailed, probe, stream_chat
from .audit import audit
from .settings import settings

app = FastAPI()
@app.get("/healthz")
def h():
    return {"ok": True, "service": settings.app_name, "version": settings.app_version}
