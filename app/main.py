from fastapi import FastAPI
app = FastAPI()
@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "AION"}
@app.get("/readyz")
def readyz():
    return {"ok": True}
