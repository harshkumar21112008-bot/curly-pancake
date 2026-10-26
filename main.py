import asyncio
import os
import threading
import time

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import db, pipeline, scraper  # noqa: E402

app = FastAPI(title="Competitor Monitor")

@app.exception_handler(Exception)
async def on_error(request, exc):
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)



class CheckReq(BaseModel):
    domain: str
    slack_webhook: str | None = None
    email: str | None = None


@app.post("/api/check")
def check(req: CheckReq):
    try:
        return pipeline.run_check(req.domain, (req.slack_webhook or "").strip() or None,
                                  (req.email or "").strip() or None)
    except scraper.InvalidDomain as e:
        raise HTTPException(400, str(e))


@app.get("/api/posts")
def posts(domain: str):
    return db.recent_posts(scraper.normalize_domain(domain))


@app.get("/api/domains")
def domains():
    return db.list_domains()


@app.get("/api/health")
def health():
    return {"ok": True}


def _scheduler(hours):
    while True:
        time.sleep(hours * 3600)
        for d in db.list_domains():
            try:
                pipeline.run_check(d["domain"])
            except Exception as e:
                print("scheduled check failed", d["domain"], e)


@app.on_event("startup")
def start():
    if os.getenv("AUTO_CHECK_HOURS"):
        threading.Thread(target=_scheduler, args=(float(os.environ["AUTO_CHECK_HOURS"]),), daemon=True).start()


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True))
