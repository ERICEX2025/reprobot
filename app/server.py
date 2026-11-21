"""Web server for ReproBot. Run: uvicorn app.server:app --port 8000 (from the repo root, with GEMINI_API_KEY set)."""
import json
import pathlib
import sys
import threading
import uuid

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
import pipeline  # noqa: E402

app = FastAPI()
JOBS = {}  # job id -> {"status", "stage", "result", "error"}; in memory is enough for a demo
UPLOADS = ROOT / "data" / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)


def _work(jid, path, note):
    job = JOBS[jid]
    try:
        result = pipeline.run(path, note, rid=jid, on_progress=lambda s: job.update(stage=s))
        v = result.get("verdict") or {}
        job["screenshot"] = v.pop("screenshot_png", None)
        job.update(status="done", result=result)
    except Exception as e:
        job.update(status="error", error=str(e)[:500])


@app.post("/api/jobs")
async def create_job(video: UploadFile = File(...), url: str = Form(""), note: str = Form("")):
    jid = uuid.uuid4().hex[:8]
    ext = ".mp4" if (video.content_type or "").endswith("mp4") else ".webm"
    path = UPLOADS / f"{jid}{ext}"
    path.write_bytes(await video.read())
    full_note = f"Page URL: {url} . User says: {note}" if url else note
    JOBS[jid] = {"status": "running", "stage": "starting"}
    threading.Thread(target=_work, args=(jid, str(path), full_note), daemon=True).start()
    return {"id": jid}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    job = JOBS.get(jid)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return {k: v for k, v in job.items() if k != "screenshot"} | {"has_screenshot": bool(job.get("screenshot"))}


@app.get("/api/jobs/{jid}/screenshot")
def get_shot(jid: str):
    job = JOBS.get(jid)
    if not job or not job.get("screenshot"):
        return Response(status_code=404)
    return Response(job["screenshot"], media_type="image/png")


@app.get("/api/eval")
def get_eval():
    p = ROOT / "data" / "eval.json"
    return json.loads(p.read_text()) if p.exists() else {"n": 0, "rows": []}


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")
