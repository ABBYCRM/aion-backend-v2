"""AION test - probe + cp."""
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .kernel import AION_CONTINUITY_PACK
from .llm import probe
from .audit import audit
from .settings import settings

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/healthz")
def h():
    return {"ok": True, "service": settings.app_name, "version": settings.app_version}

@app.get("/api/continuity-pack")
def cp():
    return AION_CONTINUITY_PACK

@app.get("/readyz")
async def r():
    return {"ok": True, "env": settings.environment}

@app.on_event("startup")
async def on_startup():
    audit.record("aion.startup", {"v": settings.app_version})
    async def _bg():
        try:
            providers = await asyncio.wait_for(probe(), timeout=20.0)
            for p, info in providers.items():
                print(f"[AION] provider={p} ok={info.get('ok')}")
        except Exception as e:
            print(f"[AION] probe failed: {e}")
    asyncio.create_task(_bg())
