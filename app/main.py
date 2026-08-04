from fastapi import FastAPI
app = FastAPI()
@app.get("/healthz")
def h():
    try:
        import pydantic_settings
        return {"ok": True, "version": pydantic_settings.__version__, "path": pydantic_settings.__file__}
    except Exception as e:
        import traceback
        return {"ok": False, "err": str(e), "trace": traceback.format_exc()[:300]}
