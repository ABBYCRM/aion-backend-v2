import os
import sys
from fastapi import FastAPI
app = FastAPI()
@app.get("/healthz")
def h():
    keys = sorted(os.environ.keys())
    return {"env_count": len(keys), "env_keys": keys[:50]}
