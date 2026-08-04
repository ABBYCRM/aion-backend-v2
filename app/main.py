from fastapi import FastAPI
print("=== BOOT START ===", flush=True)
app = FastAPI()
print("=== APP CREATED ===", flush=True)
@app.get("/healthz")
def h():
    print("=== HANDLER ===", flush=True)
    return {"ok": True}
print("=== BOOT END ===", flush=True)
