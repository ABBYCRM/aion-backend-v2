"""AION test - just CP import."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .kernel import AION_CONTINUITY_PACK

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/healthz")
def h():
    return {"ok": True}

@app.get("/api/continuity-pack")
def cp():
    return AION_CONTINUITY_PACK
