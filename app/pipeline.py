"""The ReproBot pipeline: screen recording in, verified bug report out.

Step 1  watch    Gemini 4 Neon watches the recording (agentic video) and writes a structured report.
Step 2  gate     Gemini Decision 1 scores the report: can it be reproduced from text alone? how severe?
Step 3  repro    A Gemini managed agent (Antigravity, Linux sandbox) follows the report in a real
                 browser with Playwright and decides whether the bug really happens.
Step 4  verdict  Gemini 4 Hydrogen turns the agent's free-form log into a strict JSON verdict.

The baseline used in the evaluation is one Hydrogen call on the video with no verification.
"""
import json
import os
import threading
import time
from google import genai

NEON = "gemini-4-neon"
HYDROGEN = "gemini-4-hydrogen"
DECISION = "gemini-decision-1"
AGENT = "antigravity-preview-09-2026"

client = genai.Client()
beta = genai.Client(http_options={"api_version": "v1beta"})

# ---------- schemas ----------

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_bug": {"type": "boolean", "description": "False if the recording shows expected behavior (e.g. user error)"},
        "title": {"type": "string"},
        "start_url": {"type": "string", "description": "Exact URL from the address bar or page, or empty if not visible"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "t": {"type": "number", "description": "seconds into the video"},
                    "action": {"type": "string", "description": "navigate, click, type, scroll, wait, observe"},
                    "target": {"type": "string", "description": "visible label, text or description of the element"},
                    "value": {"type": "string", "description": "text typed or URL, else empty"},
                },
                "required": ["t", "action", "target", "value"],
            },
        },
        "expected": {"type": "string"},
        "actual": {"type": "string"},
        "check": {
            "type": "string",
            "description": "One concrete, automatable check that is TRUE when the bug is present, "
            "e.g. 'the second image on the page has naturalWidth 0'",
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t": {"type": "number"}, "note": {"type": "string"}},
                "required": ["t", "note"],
            },
        },
    },
    "required": ["is_bug", "title", "start_url", "steps", "expected", "actual", "check", "evidence"],
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["reproduced", "not_reproduced", "blocked"]},
        "attempts": {"type": "integer"},
        "summary": {"type": "string", "description": "One or two sentences on what the agent observed"},
        "screenshot_path": {"type": "string", "description": "Absolute path in the sandbox, or empty"},
        "script": {"type": "string", "description": "The final Playwright script, verbatim"},
    },
    "required": ["verdict", "attempts", "summary", "screenshot_path", "script"],
}

# ---------- helpers ----------


def _wait_interaction(it, timeout=600):
    t0 = time.time()
    while getattr(it, "status", None) in ("in_progress", "queued") and time.time() - t0 < timeout:
        time.sleep(3)
        it = beta.interactions.get(id=it.id)
    return it


def upload_video(path):
    f = client.files.upload(file=path)
    while not f.state or f.state.name != "ACTIVE":
        if f.state and f.state.name == "FAILED":
            raise RuntimeError("video processing failed")
        time.sleep(2)
        f = client.files.get(name=f.name)
    return f


# ---------- step 1: watch ----------

WATCH_PROMPT = """You are a senior QA engineer. Watch this screen recording of a user reporting a web bug.
User's note (may be empty or vague): "{note}"

Write a bug report that another engineer could reproduce WITHOUT watching the video:
- start_url: read the exact URL if it is visible anywhere.
- steps: every navigation, click and typed value, in order, with timestamps.
- check: one concrete condition, checkable by a script, that is true only when the bug is present.
- If what you see is expected behavior (for example a login error after a wrong password), set is_bug to false
  and explain why in actual.
Only describe what is visible in the recording. Do not invent URLs, labels or values."""


def watch(video_file, note, model=NEON, agentic=True):
    item = {"type": "video", "uri": video_file.uri, "mime_type": video_file.mime_type}
    if agentic:
        item["processing"] = "agentic"
    it = beta.interactions.create(
        model=model,
        input=[item, {"type": "text", "text": WATCH_PROMPT.format(note=note or "")}],
        response_format={"type": "text", "mime_type": "application/json", "schema": REPORT_SCHEMA},
        generation_config={"thinking_level": "high"},
        background=True,
    )
    it = _wait_interaction(it)
    return json.loads(it.output_text)


def _looks_clean(report):
    """Guard: seen once in testing, the model leaked its own reasoning into a string field."""
    texts = [report.get("title", ""), report.get("expected", ""), report.get("actual", ""), report.get("check", "")]
    return len(report.get("title", "")) <= 160 and not any("```" in t for t in texts)


def watch_checked(video_file, note):
    report = watch(video_file, note)
    if not _looks_clean(report):
        report = watch(video_file, note)  # one retry
    return report


# ---------- step 2: gate ----------


def gate(report):
    d = beta.decisions.create(
        model=DECISION,
        input=json.dumps(report, indent=1),
        questions={
            "reproducible": {
                "type": "noul",
                "instructions": "Could an engineer reproduce this bug from the report alone: is there a usable "
                "starting URL and concrete, ordered steps?",
            },
            "checkable": {
                "type": "noul",
                "instructions": "Does the report give a concrete, automatable check that tells buggy from correct behavior?",
            },
            "severity": {
                "type": "choice",
                "instructions": "How severe is this bug for the site's users?",
                "criteria": {
                    "cosmetic": "Visual only, nothing stops the user.",
                    "minor": "Annoying but there is an easy workaround.",
                    "major": "A main feature is broken or content is missing.",
                    "critical": "Site unusable, data loss, or security impact.",
                    "other": "Not a bug, or cannot tell.",
                },
            },
        },
    )
    a = d.answers
    out = {
        "reproducible": round(a["reproducible"].noul, 2),
        "checkable": round(a["checkable"].noul, 2),
        "severity": a["severity"].choice,
        "severity_confidence": round(a["severity"].confidence, 2),
    }
    missing = []
    if out["reproducible"] < 0.5:
        missing.append("a starting URL or clearer steps")
    if out["checkable"] < 0.5:
        missing.append("what exactly goes wrong on screen")
    out["missing"] = missing
    out["ready"] = not missing
    return out


# ---------- step 3: reproduce in a managed agent sandbox ----------

_env_lock = threading.Lock()
_env_pool = []  # warm sandboxes with Playwright already installed


def _checkout_env():
    with _env_lock:
        return _env_pool.pop() if _env_pool else "remote"


def _return_env(env_id):
    if env_id and env_id != "remote":
        with _env_lock:
            _env_pool.append(env_id)


REPRO_PROMPT = """You are a QA engineer verifying a bug report. Work only inside your sandbox.

BUG REPORT
{report}

Do this:
1. If Python Playwright is not installed, install it (pip install playwright) and use the system
   Chrome at /usr/bin/google-chrome via executable_path (headless).
2. Write ONE Playwright script at /workspace/repro_{rid}.py that follows the steps from start_url and then
   evaluates the report's check in the page (use page.evaluate when useful).
3. Run it up to 3 times (the bug may be intermittent). It must print BUG_PRESENT or BUG_ABSENT each run,
   and save a full-page screenshot to /workspace/repro_{rid}.png on the last run.
4. Final answer: say reproduced if BUG_PRESENT appeared in any run, not_reproduced if every run printed
   BUG_ABSENT, or blocked if you could not run the steps (site down, login needed, steps unclear).
   Include the number of runs, what you observed, the screenshot path and the final script verbatim.
Never submit forms with real personal data, make purchases or change anything outside the sandbox."""


def reproduce(report, rid, model=HYDROGEN, on_progress=None):
    env = _checkout_env()
    try:
        it = beta.interactions.create(
            agent=AGENT,
            environment=env,
            agent_config={"type": "antigravity", "model": model, "max_total_tokens": 120000},
            input=REPRO_PROMPT.format(report=json.dumps(report, indent=1), rid=rid),
            background=True,
        )
        t0 = time.time()
        while getattr(it, "status", None) in ("in_progress", "queued") and time.time() - t0 < 480:
            time.sleep(4)
            it = beta.interactions.get(id=it.id)
            if on_progress:
                on_progress(f"agent working ({time.time() - t0:.0f}s)")
        env = getattr(it, "environment_id", None) or env
        raw = it.output_text or ""
    finally:
        _return_env(env)

    # Step 4: strict verdict extraction with a fast model
    v = beta.interactions.create(
        model=HYDROGEN,
        input="Extract the verification result from this QA agent log. Use only what the log says.\n\n" + raw,
        response_format={"type": "text", "mime_type": "application/json", "schema": VERDICT_SCHEMA},
        generation_config={"thinking_level": "low"},
    )
    verdict = json.loads(_wait_interaction(v).output_text)
    verdict["agent_log"] = raw
    verdict["environment_id"] = env

    shot = verdict.get("screenshot_path") or f"/workspace/repro_{rid}.png"
    try:
        verdict["screenshot_png"] = beta.environments.files.download(path=shot, environment_id=env)
    except Exception as e:  # screenshot is nice to have, not required
        verdict["screenshot_png"] = None
        verdict["screenshot_error"] = str(e)[:200]
    return verdict


# ---------- whole pipeline ----------


def run(video_path, note="", rid="x", on_progress=lambda s: None):
    timings = {}
    t = time.time()
    on_progress("uploading video")
    vf = upload_video(video_path)
    timings["upload"] = time.time() - t

    t = time.time()
    on_progress("Neon is watching the recording")
    report = watch_checked(vf, note)
    timings["watch"] = time.time() - t

    t = time.time()
    on_progress("Decision 1 is checking the report")
    g = gate(report)
    timings["gate"] = time.time() - t

    result = {"report": report, "gate": g, "timings": timings}
    if not report.get("is_bug"):
        result["final"] = "not_a_bug"
        return result
    if not g["ready"]:
        result["final"] = "needs_info"
        return result

    t = time.time()
    on_progress("managed agent is reproducing it in a sandbox browser")
    v = reproduce(report, rid, on_progress=on_progress)
    timings["reproduce"] = time.time() - t
    result["verdict"] = v
    result["final"] = v["verdict"]
    return result


def baseline(video_path, note=""):
    """One Hydrogen call, static video sampling, no gate, no verification."""
    vf = upload_video(video_path)
    return watch(vf, note, model=HYDROGEN, agentic=False)
