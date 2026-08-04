import os, sys
print("PYTHON:", sys.executable, flush=True)
print("PWD:", os.getcwd(), flush=True)
print("PYTHONHOME:", os.environ.get("PYTHONHOME"), flush=True)
print("PATH:", os.environ.get("PATH"), flush=True)
try:
    import fastapi
    print("fastapi:", fastapi.__version__, fastapi.__file__, flush=True)
except Exception as e:
    print("fastapi FAIL:", e, flush=True)
try:
    import pydantic_settings
    print("pydantic_settings:", pydantic_settings.__version__, flush=True)
except Exception as e:
    print("pydantic_settings FAIL:", e, flush=True)
try:
    import openai
    print("openai:", openai.__version__, flush=True)
except Exception as e:
    print("openai FAIL:", e, flush=True)

from fastapi import FastAPI
app = FastAPI()
@app.get("/healthz")
def h():
    return {"ok": True, "py": sys.executable, "fastapi": fastapi.__file__}
