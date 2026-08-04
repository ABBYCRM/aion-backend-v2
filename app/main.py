import os
from fastapi import FastAPI
app = FastAPI()
@app.get("/healthz")
def h():
    return {
        "gunicorn_cmd": os.environ.get("GUNICORN_CMD_ARGS"),
        "pythonpath": os.environ.get("PYTHONPATH"),
        "pythonhome": os.environ.get("PYTHONHOME"),
        "pydantic": True,
    }
@app.get("/env")
def e():
    return {k: v for k, v in sorted(os.environ.items())}
