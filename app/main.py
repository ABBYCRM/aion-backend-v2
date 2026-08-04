from fastapi import FastAPI
app = FastAPI()
@app.get("/healthz")
def h():
    return {"ok": True, "msg": "aion-2 running"}
