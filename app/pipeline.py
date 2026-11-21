"""The ReproBot pipeline: screen recording in, verified bug report out.

Step 1  watch    Gemini 4 Neon watches the recording (agentic video) and writes a structured report.
Step 2  gate     Gemini Decision 1 scores the report: can it be reproduced from text alone? how severe?
Step 3  repro    A Gemini managed agent (Antigravity, Linux sandbox) follows the report in a real
                 browser with Playwright and decides whether the bug really happens.
Step 4  verdict  Gemini 4 Hydrogen turns the agent's free-form log into a strict JSON verdict.
Step 5  fix      Optional, when the site's source repo is given: the agent (Neon) clones it, confirms the bug
                 locally, patches the cause and re-runs its own reproduction until it prints BUG_ABSENT.

Repo mode (run_repo, reproduce_repo, fix_repo) is for bugs with no live buggy site: the agent checks out the
project at a given commit in its sandbox, starts it there, reproduces against localhost and then fixes.

The baseline used in the evaluation is one Hydrogen call on the video with no verification.
"""
import json
import os
import re
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


def watch(video_file, note, model=NEON, agentic=True, timeout=600):
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
    it = _wait_interaction(it, timeout=timeout)
    if getattr(it, "status", None) in ("in_progress", "queued"):
        try:
            beta.interactions.cancel(id=it.id)
        except Exception:
            pass
        raise TimeoutError("watch timed out")
    return json.loads(it.output_text)


def _looks_clean(report):
    """Guard: seen once in testing, the model leaked its own reasoning into a string field."""
    texts = [report.get("title", ""), report.get("expected", ""), report.get("actual", ""), report.get("check", "")]
    return len(report.get("title", "")) <= 160 and not any("```" in t for t in texts)


def watch_checked(video_file, note):
    try:
        report = watch(video_file, note, timeout=90)
    except TimeoutError:  # agentic scrubbing can run long on videos where nothing happens
        report = watch(video_file, note, agentic=False, timeout=120)
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


def activity(it, limit=8):
    """Short, human-readable lines of what a managed agent is doing, from its step log."""
    out = []
    for st in getattr(it, "steps", None) or []:
        d = st.model_dump() if hasattr(st, "model_dump") else dict(st)
        if d.get("type") == "thought":
            for part in d.get("summary") or []:
                m = re.search(r"\*\*(.+?)\*\*", part.get("text") or "")
                if m:
                    out.append({"kind": "thought", "text": m.group(1)})
        elif d.get("type") == "code_execution_call":
            code = ((d.get("arguments") or {}).get("code") or "").strip().splitlines()
            if code:
                out.append({"kind": "run", "text": code[0][:110]})
        elif d.get("type") == "code_execution_result":
            res = d.get("result") or ""
            for mark in ("BUG_PRESENT", "BUG_ABSENT"):
                if mark in res:
                    out.append({"kind": "mark", "text": f"{mark} x{res.count(mark)}"})
    return out[-limit:]


def follow(interaction_id, on_activity, limit=8):
    """Stream a running agent's steps and report short readable lines as they happen."""
    def go():
        lines, thought = [], ""
        try:
            for ev in beta.interactions.get(id=interaction_id, stream=True):
                d = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
                kind, st = d.get("event_type"), d.get("step") or {}
                if kind == "step.start" and st.get("type") == "code_execution_call":
                    code = ((st.get("arguments") or {}).get("code") or "").strip().splitlines()
                    if code:
                        lines.append({"kind": "run", "text": code[0][:110]})
                elif kind == "step.start" and st.get("type") == "code_execution_result":
                    res = st.get("result") or ""
                    for mark in ("BUG_PRESENT", "BUG_ABSENT"):
                        if mark in res:
                            lines.append({"kind": "mark", "text": f"{mark} x{res.count(mark)}"})
                elif kind == "step.start" and st.get("type") == "thought":
                    thought = ""
                elif kind == "step.delta":
                    delta = d.get("delta") or {}
                    text = delta.get("text") or (delta.get("content") or {}).get("text") if isinstance(delta.get("content"), dict) else delta.get("text")
                    if text and delta.get("type") != "text":
                        thought += text
                        m = re.search(r"\*\*(.+?)\*\*", thought)
                        if m:
                            lines.append({"kind": "thought", "text": m.group(1)})
                            thought = ""
                else:
                    continue
                on_activity(lines[-limit:])
        except Exception:
            pass  # the feed is cosmetic; never let it break a run
    threading.Thread(target=go, daemon=True).start()


def reproduce(report, rid, model=HYDROGEN, on_progress=None, keep_env=False, on_activity=None):
    env = _checkout_env()
    try:
        it = beta.interactions.create(
            agent=AGENT,
            environment=env,
            agent_config={"type": "antigravity", "model": model, "max_total_tokens": 120000},
            input=REPRO_PROMPT.format(report=json.dumps(report, indent=1), rid=rid),
            background=True,
        )
        if on_activity:
            follow(it.id, on_activity)
        t0 = time.time()
        while getattr(it, "status", None) in ("in_progress", "queued") and time.time() - t0 < 480:
            time.sleep(4)
            it = beta.interactions.get(id=it.id)
            if on_progress:
                on_progress(f"agent working ({time.time() - t0:.0f}s)")
        env = getattr(it, "environment_id", None) or env
        raw = it.output_text or ""
        repro_id = it.id
    finally:
        if not keep_env:  # when a fix step follows, the sandbox stays ours until fix() returns it
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
    verdict["interaction_id"] = repro_id

    shot = verdict.get("screenshot_path") or f"/workspace/repro_{rid}.png"
    verdict["screenshot_png"] = _download(shot, env, _is_png)  # nice to have, not required
    return verdict


def _download(path, env, ok, tries=3):
    """Sandbox downloads occasionally return wrong bytes; check the content and retry."""
    for _ in range(tries):
        try:
            data = beta.environments.files.download(path=path, environment_id=env)
            if ok(data):
                return data
        except Exception:
            pass
        time.sleep(1)
    return None


def _is_png(b):
    return bool(b) and b[:8] == b"\x89PNG\r\n\x1a\n"


def _is_diff(b):
    return bool(b) and b.lstrip()[:10] == b"diff --git"


def prewarm(n=2):
    """Start n sandboxes with Playwright already installed, so the first live run skips the install."""
    def one():
        try:
            it = beta.interactions.create(
                agent=AGENT, environment="remote",
                agent_config={"type": "antigravity", "model": HYDROGEN, "max_total_tokens": 40000},
                input="Install Python Playwright with pip. Then run a 3-line script that launches /usr/bin/google-chrome "
                "headless through Playwright (executable_path), opens about:blank and prints READY. Reply with READY only.",
                background=True,
            )
            it = _wait_interaction(it, timeout=300)
            _return_env(getattr(it, "environment_id", None))
        except Exception as e:
            print("prewarm failed:", str(e)[:200])
    for _ in range(n):
        threading.Thread(target=one, daemon=True).start()


def _take_env(env_id):
    with _env_lock:
        if env_id in _env_pool:
            _env_pool.remove(env_id)


# ---------- step 5 (optional): prove a fix against the site's source ----------

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["fixed", "not_fixed", "cannot_confirm_locally"]},
        "root_cause": {"type": "string", "description": "One or two sentences, naming the file and line"},
        "runs_before": {"type": "string", "description": "What the local reproduction printed before the change"},
        "runs_after": {"type": "string", "description": "What it printed after the change, for each run"},
    },
    "required": ["status", "root_cause", "runs_before", "runs_after"],
}

FIX_PROMPT = """You reproduced this bug on the live site with /workspace/repro_{rid}.py. Now prove a fix. Stay inside the sandbox.

Source: {repo_url} (the site's files are in the subfolder "{subdir}"; that subfolder is what is served at {site_root}).

1. git clone --depth 1 the repo to /workspace/src_{rid}.
2. Serve the subfolder locally in the background: python3 -m http.server {port}.
3. Copy your reproduction script to /workspace/repro_local_{rid}.py and change ONLY the origin, so {site_root} becomes
   http://localhost:{port} . Run it. It must print BUG_PRESENT. If it does not, stop and answer cannot_confirm_locally.
4. Find the root cause in the source. Make the smallest change that fixes it. Do not touch unrelated code and do not
   weaken or edit the check in the reproduction script.
5. Run the local reproduction 3 more times. Every run must print BUG_ABSENT. If the bug was intermittent, run it 8 times.
6. Save `git diff` (run inside the clone) to /workspace/fix_{rid}.diff and a full-page screenshot of the fixed page to
   /workspace/fixed_{rid}.png.
Final answer: fixed, not_fixed or cannot_confirm_locally; the root cause (file and line); what the runs printed before
and after; and the diff verbatim. Do not push, open pull requests or change anything outside the sandbox."""


def fix(report, verdict, repo_url, subdir, site_root, rid, on_progress=None, on_activity=None):
    env = verdict["environment_id"]
    _take_env(env)
    try:
        it = beta.interactions.create(
            agent=AGENT,
            environment=env,
            previous_interaction_id=verdict["interaction_id"],
            agent_config={"type": "antigravity", "model": NEON, "max_total_tokens": 200000},
            input=FIX_PROMPT.format(rid=rid, repo_url=repo_url, subdir=subdir, site_root=site_root.rstrip("/"),
                                    port=8700 + sum(map(ord, rid)) % 200),
            background=True,
        )
        if on_activity:
            follow(it.id, on_activity)
        t0 = time.time()
        while getattr(it, "status", None) in ("in_progress", "queued") and time.time() - t0 < 600:
            time.sleep(4)
            it = beta.interactions.get(id=it.id)
            if on_progress:
                on_progress(f"fix agent working ({time.time() - t0:.0f}s)")
        raw = it.output_text or ""
    except Exception:
        _return_env(env)
        raise
    x = beta.interactions.create(
        model=HYDROGEN,
        input="Extract the result from this engineer's log. Use only what the log says.\n\n" + raw,
        response_format={"type": "text", "mime_type": "application/json", "schema": FIX_SCHEMA},
        generation_config={"thinking_level": "low"},
    )
    out = json.loads(_wait_interaction(x).output_text)
    out["agent_log"] = raw
    diff = _download(f"/workspace/fix_{rid}.diff", env, _is_diff)
    if diff:
        out["diff"] = diff.decode("utf-8", "replace")
    else:  # fall back to the diff the agent printed in its log
        i = raw.find("diff --git")
        out["diff"] = raw[i:].split("```")[0].strip() if i >= 0 else None
    out["fixed_png"] = _download(f"/workspace/fixed_{rid}.png", env, _is_png)
    _return_env(env)
    return out


# ---------- repo mode: reproduce and fix a bug from a repository at a given commit (no live site) ----------
# Used for real, already-fixed issues: the agent gets the report and the source at the PRE-FIX commit, nothing else.
# These are additions; the live-URL functions above are unchanged.


def _trace(it):
    """Compact list of what the agent did (commands, tool calls, results), for auditing afterwards."""
    out = []
    for s in getattr(it, "steps", None) or []:
        t = getattr(s, "type", "")
        if t == "code_execution_call":
            out.append({"type": "run", "code": getattr(s.arguments, "code", str(s.arguments))})
        elif t == "function_call":
            out.append({"type": "call", "name": getattr(s, "name", ""), "args": json.dumps(s.arguments, default=str)[:6000]})
        elif t in ("code_execution_result", "function_result"):
            r = str(getattr(s, "result", ""))
            out.append({"type": "result", "error": bool(getattr(s, "is_error", False)),
                        "text": r if len(r) <= 3000 else r[:1500] + "\n...[cut]...\n" + r[-1500:]})
        elif t in ("thought", "model_output"):
            continue
        else:
            out.append({"type": t, "text": str(s)[:2000]})
    return out


def _run_agent(prompt, env, model, timeout, max_tokens, previous=None, on_progress=None, label="agent"):
    """Start a managed-agent task and wait up to `timeout` seconds. On timeout, cancel it and say so."""
    kw = {"previous_interaction_id": previous} if previous else {}
    it = beta.interactions.create(
        agent=AGENT, environment=env,
        agent_config={"type": "antigravity", "model": model, "max_total_tokens": max_tokens},
        input=prompt, background=True, **kw)
    t0 = time.time()
    while getattr(it, "status", None) in ("in_progress", "queued") and time.time() - t0 < timeout:
        time.sleep(5)
        try:
            it = beta.interactions.get(id=it.id)
        except Exception:
            continue  # transient poll error; the deadline still applies
        if on_progress:
            on_progress(f"{label} working ({time.time() - t0:.0f}s)")
    timed_out = getattr(it, "status", None) in ("in_progress", "queued")
    if timed_out:
        try:
            beta.interactions.cancel(id=it.id)
            time.sleep(3)
            it = beta.interactions.get(id=it.id)
        except Exception:
            pass
    return it, timed_out, round(time.time() - t0)


REPO_REPRO_PROMPT = """You are a QA engineer verifying a bug report against a project's source code. Work only inside your sandbox.

BUG REPORT (written from the reporter's screen recording)
{report}

PROJECT
Repository: {clone_url}
Commit to test: {sha}
Install and start: {run_cmd}
The app should then be reachable at: {app_url}

RULES
- Work only from the bug report above and the source code at that commit.
- You must NOT browse or fetch GitHub issues, pull requests, commits, releases or changelogs, must NOT search the web
  for this bug, and must NOT look up or download any later version of this project or its upstream fix.
  The only network use allowed is the one-commit fetch below and installing the project's dependencies.

Do this:
1. Get exactly that one commit, with no later history:
     mkdir -p /workspace/src_{rid} && cd /workspace/src_{rid} && git init -q && git remote add origin {clone_url}
     git fetch -q --depth 1 origin {sha} && git checkout -q FETCH_HEAD && git remote remove origin
2. Install and start the app in the background with the command above (adapt it only if it fails, and say what you
   changed). Confirm {app_url} responds. If the project cannot be installed or started after reasonable effort, stop
   and give the final answer setup_failed with the error.
3. If Python Playwright is not installed, install it (pip install playwright) and use the system Chrome at
   /usr/bin/google-chrome via executable_path (headless).
4. Write ONE Playwright script at /workspace/repro_{rid}.py (outside the clone) that follows the report's steps in
   the locally running app and then evaluates the report's check (use page.evaluate when useful). You may read the
   source to find selectors and to understand how to drive the app, but the script must test the behaviour the
   reporter saw, through the page, not just assert something about the source text.
5. Run it up to 3 times (the bug may be intermittent). It must print BUG_PRESENT or BUG_ABSENT each run, and save a
   screenshot to /workspace/repro_{rid}.png on the last run.
6. Final answer: reproduced if BUG_PRESENT appeared in any run; not_reproduced if every run printed BUG_ABSENT;
   blocked if the app runs but you could not carry out the steps; setup_failed if the app would not install or start.
   Include the number of runs, what you observed, the exact commands used to install and start the app, the
   screenshot path and the final script verbatim.
Do not push, open pull requests, comment, or change anything outside the sandbox."""

REPO_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["reproduced", "not_reproduced", "blocked", "setup_failed"]},
        "attempts": {"type": "integer"},
        "summary": {"type": "string", "description": "One or two sentences on what the agent observed"},
        "setup": {"type": "string", "description": "Commands used to install and start the app, and any setup error"},
        "screenshot_path": {"type": "string", "description": "Absolute path in the sandbox, or empty"},
        "script": {"type": "string", "description": "The final Playwright script, verbatim"},
    },
    "required": ["verdict", "attempts", "summary", "setup", "screenshot_path", "script"],
}


def _is_text(b):
    return bool(b) and b"\x00" not in b[:2000]


def reproduce_repo(report, rid, clone_url, sha, run_cmd, app_url, model=HYDROGEN, timeout=720,
                   max_tokens=400000, on_progress=None):
    """Repo mode of step 3. Always uses a fresh sandbox (never the warm pool) and keeps it for fix_repo().
    Returns the verdict dict; verdict is 'timeout' if the agent did not finish in time."""
    prompt = REPO_REPRO_PROMPT.format(report=json.dumps(report, indent=1), rid=rid, clone_url=clone_url, sha=sha,
                                      run_cmd=run_cmd, app_url=app_url)
    it, timed_out, secs = _run_agent(prompt, "remote", model, timeout, max_tokens, on_progress=on_progress)
    env = getattr(it, "environment_id", None)
    raw = getattr(it, "output_text", None) or ""
    base = {"agent_log": raw, "trace": _trace(it), "environment_id": env, "interaction_id": it.id,
            "agent_status": getattr(it, "status", None), "agent_seconds": secs,
            "total_tokens": getattr(getattr(it, "usage", None), "total_tokens", None)}
    if timed_out or not raw.strip():
        return {"verdict": "timeout" if timed_out else "no_output", "attempts": 0, "summary": "", "setup": "",
                "screenshot_path": "", "script": "", "screenshot_png": None, **base}
    v = beta.interactions.create(
        model=HYDROGEN,
        input="Extract the verification result from this QA agent log. Use only what the log says.\n\n" + raw,
        response_format={"type": "text", "mime_type": "application/json", "schema": REPO_VERDICT_SCHEMA},
        generation_config={"thinking_level": "low"},
    )
    verdict = json.loads(_wait_interaction(v).output_text)
    verdict.update(base)
    verdict["screenshot_png"] = _download(verdict.get("screenshot_path") or f"/workspace/repro_{rid}.png", env, _is_png)
    script = _download(f"/workspace/repro_{rid}.py", env, _is_text)
    if script:
        verdict["script_file"] = script.decode("utf-8", "replace")
    return verdict


REPO_FIX_PROMPT = """You reproduced this bug locally with /workspace/repro_{rid}.py against the clone in /workspace/src_{rid}.
Now prove a fix. Stay inside the sandbox. The same rules apply: work only from the bug report and the source at this
commit; do NOT browse GitHub issues, pull requests or later commits, do NOT search the web for this bug, and do NOT
look at any later version of this project.

{tests_before}2. Find the root cause in the source. Make the smallest change that fixes it. Do not touch unrelated code and do not
   weaken or edit the check in the reproduction script (keep /workspace/repro_{rid}.py byte-for-byte as it is).
3. If the running app needs a rebuild or restart to pick up the change, do that. Then run the reproduction 3 more
   times. Every run must print BUG_ABSENT. If the bug was intermittent, run it 8 times.
{tests_after}5. Save `git diff` (run inside the clone; source changes only, no build output or lockfiles) to
   /workspace/fix_{rid}.diff and a screenshot of the fixed page to /workspace/fixed_{rid}.png.
Final answer: fixed or not_fixed; the root cause (file and line); what the reproduction printed before and after;
the project's own test results before and after the change (or "no test command given"); and the diff verbatim.
Do not push, open pull requests or change anything outside the sandbox."""

_TESTS_BEFORE = """1. Before changing anything, run the project's own tests from the clone: {test_cmd}
   Save the last 60 lines of output to /workspace/tests_before_{rid}.txt and note the pass/fail counts. Some tests
   may already fail at this commit; that is fine, just record it. If the command runs longer than 4 minutes, stop it
   and record "timed out".
"""
_TESTS_AFTER = """4. Run the project's own tests again with the same command. Save the last 60 lines to
   /workspace/tests_after_{rid}.txt and note the pass/fail counts. Your change must not make any test fail that passed
   before. Do not edit or delete existing tests to make them pass.
"""

REPO_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["fixed", "not_fixed"]},
        "root_cause": {"type": "string", "description": "One or two sentences, naming the file and line"},
        "runs_before": {"type": "string", "description": "What the reproduction printed before the change"},
        "runs_after": {"type": "string", "description": "What it printed after the change, for each run"},
        "tests_before": {"type": "string", "description": "Project's own test result before the change (counts), or 'not run'"},
        "tests_after": {"type": "string", "description": "Project's own test result after the change (counts), or 'not run'"},
    },
    "required": ["status", "root_cause", "runs_before", "runs_after", "tests_before", "tests_after"],
}


def fix_repo(verdict, rid, test_cmd=None, model=NEON, timeout=480, max_tokens=400000, on_progress=None):
    """Repo mode of step 5. Continues in the sandbox and conversation left by reproduce_repo()."""
    env = verdict["environment_id"]
    prompt = REPO_FIX_PROMPT.format(
        rid=rid,
        tests_before=_TESTS_BEFORE.format(test_cmd=test_cmd, rid=rid) if test_cmd else "1. (No project test command was given.)\n",
        tests_after=_TESTS_AFTER.format(rid=rid) if test_cmd else "4. (No project test command was given.)\n")
    it, timed_out, secs = _run_agent(prompt, env, model, timeout, max_tokens, previous=verdict["interaction_id"],
                                     on_progress=on_progress, label="fix agent")
    raw = getattr(it, "output_text", None) or ""
    out = {"status": "timeout" if timed_out else "no_output", "root_cause": "", "runs_before": "", "runs_after": "",
           "tests_before": "", "tests_after": ""}
    if raw.strip() and not timed_out:
        x = beta.interactions.create(
            model=HYDROGEN,
            input="Extract the result from this engineer's log. Use only what the log says.\n\n" + raw,
            response_format={"type": "text", "mime_type": "application/json", "schema": REPO_FIX_SCHEMA},
            generation_config={"thinking_level": "low"},
        )
        out = json.loads(_wait_interaction(x).output_text)
    out.update(agent_log=raw, trace=_trace(it), interaction_id=it.id, agent_status=getattr(it, "status", None),
               agent_seconds=secs, total_tokens=getattr(getattr(it, "usage", None), "total_tokens", None))
    diff = _download(f"/workspace/fix_{rid}.diff", env, _is_diff)
    if diff:
        out["diff"], out["diff_source"] = diff.decode("utf-8", "replace"), "file"
    else:  # fall back to the diff the agent printed in its log
        i = raw.find("diff --git")
        out["diff"], out["diff_source"] = (raw[i:].split("```")[0].strip() if i >= 0 else None), "log"
    for k in ("tests_before", "tests_after"):
        b = _download(f"/workspace/{k}_{rid}.txt", env, _is_text) if test_cmd else None
        out[k + "_output"] = b.decode("utf-8", "replace")[-4000:] if b else None
    out["fixed_png"] = _download(f"/workspace/fixed_{rid}.png", env, _is_png)
    return out


def run_repo(video_path, note, rid, clone_url, sha, run_cmd, app_url, test_cmd=None, on_progress=lambda s: None,
             deadline_s=1200):
    """Whole pipeline in repo mode: watch, gate, reproduce at the given commit, fix. Same stop rules as run()."""
    timings = {}
    t_start = t = time.time()
    on_progress("uploading video")
    vf = upload_video(video_path)
    timings["upload"] = time.time() - t

    t = time.time()
    on_progress("Neon is watching the recording")
    report = watch_checked(vf, note)
    timings["watch"] = time.time() - t

    t = time.time()
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
    on_progress("managed agent is setting up the project and reproducing the bug")
    v = reproduce_repo(report, rid, clone_url, sha, run_cmd, app_url, on_progress=on_progress)
    timings["reproduce"] = time.time() - t
    result["verdict"] = v
    result["final"] = v["verdict"]

    left = deadline_s - (time.time() - t_start)
    if v["verdict"] == "reproduced":
        if left < 120:
            result["fix"] = {"status": "timeout", "note": "no time left for the fix step inside the case deadline"}
        else:
            t = time.time()
            on_progress("fix agent is patching the source and re-running the reproduction")
            result["fix"] = fix_repo(v, rid, test_cmd=test_cmd, timeout=left, on_progress=on_progress)
            timings["fix"] = time.time() - t
    return result


# ---------- whole pipeline ----------


def run(video_path, note="", rid="x", on_progress=lambda s: None, source=None, on_partial=None, on_activity=None):
    part = on_partial or (lambda k, v: None)
    timings = {}
    t = time.time()
    on_progress("uploading video")
    vf = upload_video(video_path)
    timings["upload"] = time.time() - t

    t = time.time()
    on_progress("Neon is watching the recording")
    report = watch_checked(vf, note)
    timings["watch"] = time.time() - t
    part("report", report)

    t = time.time()
    on_progress("Decision 1 is checking the report")
    g = gate(report)
    timings["gate"] = time.time() - t
    part("gate", g)

    result = {"report": report, "gate": g, "timings": timings}
    if not report.get("is_bug"):
        result["final"] = "not_a_bug"
        return result
    if not g["ready"]:
        result["final"] = "needs_info"
        return result

    t = time.time()
    on_progress("managed agent is reproducing it in a sandbox browser")
    v = reproduce(report, rid, on_progress=on_progress, keep_env=bool(source), on_activity=on_activity)
    timings["reproduce"] = time.time() - t
    if source and v["verdict"] != "reproduced":
        _return_env(v["environment_id"])
    result["verdict"] = v
    result["final"] = v["verdict"]
    part("verdict", {k: v.get(k) for k in ("verdict", "attempts", "summary")})

    if source and v["verdict"] == "reproduced":
        t = time.time()
        on_progress("fix agent is patching the source and re-running the reproduction")
        if on_activity:
            on_activity([])
        result["fix"] = fix(report, v, source["repo_url"], source["subdir"], source["site_root"], rid,
                            on_progress=on_progress, on_activity=on_activity)
        timings["fix"] = time.time() - t
    return result


def baseline(video_path, note=""):
    """One Hydrogen call, static video sampling, no gate, no verification."""
    vf = upload_video(video_path)
    return watch(vf, note, model=HYDROGEN, agentic=False)
