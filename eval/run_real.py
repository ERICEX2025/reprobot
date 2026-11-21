"""Run ReproBot in repo mode on real, already-fixed GitHub issues.

Usage:
  python eval/run_real.py run [-j N] case_id [case_id ...]   run cases (N <= 2), one fresh sandbox each
  python eval/run_real.py run --extended case_id ...         second pass for timed-out cases (longer limits, kept separate)
  python eval/run_real.py collect                            rebuild data/eval_real.json from saved runs + scores
  python eval/run_real.py list

What the pipeline is given per case: the reporter's video, the issue TITLE, and operator configuration (repo URL,
pre-fix commit, install/start command, local URL, test command). It is never given the issue body, the fix PR or
anything from the fix. Each case is saved to data/real_runs/<id>.json when it finishes. Scores in
data/real_scores.json are written by a person (or reviewing agent) after reading the outputs and the maintainers'
diff; `collect` only merges and counts them.

Nothing here clones or installs locally: all project work happens in the remote sandbox.
"""
import json, pathlib, re, subprocess, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
CANDIDATES = pathlib.Path.home() / "dev" / "bugrepro-real-candidates.json"
RUNS = ROOT / "data" / "real_runs"
VIDEOS = ROOT / "data" / "real"
SHOTS = ROOT / "data" / "shots"
SCORES = ROOT / "data" / "real_scores.json"
OUT = ROOT / "data" / "eval_real.json"
DEADLINE_S = 1200  # hard limit per case for reproduce + fix (watch and gate included)

CHOGAN = "python3 -m http.server -d www 8000"
DRAWDB = "npm install && npm run dev -- --port 5173 --strictPort"
WAVES = "npm install --legacy-peer-deps && npm run build && python3 -m http.server 9090"

# Operator configuration. `title` is the issue title exactly as on GitHub (the only issue text the pipeline sees).
# `tests` is the project's own test command, or None; `tests_note` says why when it is None.
CASES = [
    dict(id="chogan595", issue="https://github.com/choganhq/chogan/issues/595",
         title="Token skips a house in Snakes and Ladders for both players",
         run=CHOGAN, url="http://localhost:8000/games/snakes/index.html", tests="node tools/test.js"),
    dict(id="chogan506", issue="https://github.com/choganhq/chogan/issues/506",
         title="[Bug] Power bar gains power when dragged below its starting point",
         run=CHOGAN, url="http://localhost:8000/games/eightball/index.html", tests="node tools/test.js"),
    dict(id="drawdb858", issue="https://github.com/drawdb-io/drawdb/issues/858",
         title="[BUG] Table width modal applies the changes without user confirmation",
         run=DRAWDB, url="http://localhost:5173/editor", tests=None, tests_note="project has no tests"),
    dict(id="wavesurfer3742", issue="https://github.com/katspaugh/wavesurfer.js/issues/3742",
         title="TimelinePlugin not properly rerendered after zooming if not contained in Wavesurfer wrapper",
         run=WAVES, url="http://localhost:9090/#timeline.js", tests=None, tests_note="only Cypress e2e (heavy), not run"),
    dict(id="wavesurfer2963", issue="https://github.com/katspaugh/wavesurfer.js/issues/2963",
         title="After right-clicking, the playback section follows whenever the mouse pointer is moved.",
         run=WAVES, url="http://localhost:9090/#basic.js", tests=None, tests_note="only Cypress e2e (heavy), not run"),
    dict(id="panels650", issue="https://github.com/bvaughn/react-resizable-panels/issues/650",
         title="Separator jumping for collapsible panels",
         run="pnpm install && pnpm dev --port 5173", url="http://localhost:5173/", tests="pnpm test:ci"),
    # ---- "medium" candidates ----
    dict(id="wavesurfer3494", issue="https://github.com/katspaugh/wavesurfer.js/issues/3494",
         title="Adjusting region size while zoomed on part of region cause major render issues",
         run=WAVES, url="http://localhost:9090/#regions.js", tests=None, tests_note="only Cypress e2e (heavy), not run"),
    dict(id="drawdb687", issue="https://github.com/drawdb-io/drawdb/issues/687",
         title="[BUG] Back navigation from /editor skips or freezes homepage",
         run=DRAWDB, url="http://localhost:5173/", tests=None, tests_note="project has no tests"),
    dict(id="chogan491", issue="https://github.com/choganhq/chogan/issues/491",
         title="[Bug] Teammate card pops up for a split second in Hokm",
         run=CHOGAN, url="http://localhost:8000/games/hokm/index.html", tests="node tools/test.js"),
    dict(id="datepicker5573", issue="https://github.com/Hacker0x01/react-datepicker/issues/5573",
         title="All options are selected for year picker",
         run="yarn install && yarn build && yarn start   (docs site; Yarn 4)", url="http://localhost:5173/", tests="yarn test"),
    dict(id="recharts6618", issue="https://github.com/recharts/recharts/issues/6618",
         title="[Bug] page navigation is broken after first chart load",
         run="npm install && npm run build && cd www && npm install && npm start   (docs site in www/)",
         url="http://localhost:5173/", tests=None, tests_note="the docs site (www) has no tests"),
    dict(id="sonner512", issue="https://github.com/emilkowalski/sonner/issues/512",
         title="Bug: top positions have opposite swipe to dismiss directions",
         run="pnpm install && pnpm build && pnpm dev:test   (Next.js test app)", url="http://localhost:3000/",
         tests=None, tests_note="only Playwright e2e against the test app, not run"),
    dict(id="fortune500", issue="https://github.com/ruilisi/fortune-sheet/issues/500",
         title="Dragging cell values does not update linked formulae",
         run="yarn install && yarn storybook   (Storybook)", url="http://localhost:6006/", tests="yarn test"),
    dict(id="excalidraw8346", issue="https://github.com/excalidraw/excalidraw/issues/8346",
         title="BUG: line editor, each new point is duplicated when adding points with ALT/OPT drag",
         run="yarn install && yarn start   (Yarn 1.22, Node 18 to 20)", url="http://localhost:3000/",
         tests=None, tests_note="vitest suite is large, not run"),
]


def candidates():
    return {c["issue_url"]: c for c in json.loads(CANDIDATES.read_text())["candidates"]}


def download_video(case, cand):
    """Small reporter video only (a few MB). Returns the local path."""
    VIDEOS.mkdir(parents=True, exist_ok=True)
    m = cand["media"][0]
    ext = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}.get(m["type"], "mp4")
    path = VIDEOS / f"rec_{case['id']}.{ext}"
    if not path.exists() or path.stat().st_size != m["bytes"]:
        subprocess.run(["curl", "-sSL", "--max-time", "120", "-o", str(path), m["url"]], check=True)
    if path.stat().st_size < 10000:
        raise RuntimeError(f"video download too small: {path.stat().st_size} bytes")
    return path


# Things in the agent's commands that would mean it went looking for the answer. Reviewed by hand afterwards.
LEAK_PATTERNS = [
    (r"github\.com/[\w.-]+/[\w.-]+/(issues|pull|pulls|commit|commits|compare|releases|blob|tree|raw)", "github page"),
    (r"api\.github\.com|raw\.githubusercontent\.com|codeload\.github\.com", "github api/raw"),
    (r"(^|[\s;&|])gh\s+(pr|issue|api|search|repo)", "gh cli"),
    (r"git\s+(fetch|pull|clone|ls-remote|log\s+origin)", "git network"),
    (r"unpkg\.com|jsdelivr\.net|npm\s+(view|info|pack|show)|registry\.npmjs\.org", "published package lookup"),
    (r"google\.com/search|duckduckgo|bing\.com|stackoverflow\.com", "web search"),
]
LEAK_TOOLS = re.compile(r"search|browse|fetch_url|read_url|web", re.I)


def leak_scan(case, cand, *traces):
    sha = cand["pre_fix_sha"]
    hits = []
    for which, trace in traces:
        for i, s in enumerate(trace or []):
            if s["type"] == "run":
                text = s["code"]
            elif s["type"] == "call":
                text = s["name"] + " " + s["args"]
                if LEAK_TOOLS.search(s["name"]):
                    hits.append({"step": which, "i": i, "why": "tool: " + s["name"], "text": text[:400]})
            else:
                continue
            for pat, why in LEAK_PATTERNS:
                for m in re.finditer(pat, text):
                    line = text[max(0, m.start() - 120): m.end() + 200]
                    # the one allowed fetch: that exact commit, depth 1
                    if why == "git network" and sha in line and "--depth 1" in line:
                        continue
                    hits.append({"step": which, "i": i, "why": why, "text": line})
    # also look for the fix PR number or fix SHA appearing anywhere in what the agent saw or did
    pr = cand["fix_url"].rsplit("/", 1)[1]
    blob = json.dumps(traces)
    if cand["fix_sha"][:10] in blob:
        hits.append({"step": "any", "i": -1, "why": "fix commit SHA appears in trace", "text": cand["fix_sha"]})
    if re.search(r"(pull|pulls|issues)/" + pr + r"\b", blob):
        hits.append({"step": "any", "i": -1, "why": "fix PR number appears in a URL in trace", "text": pr})
    return hits


def one(case):
    import pipeline
    cand = candidates()[case["issue"]]
    rid = case["id"]
    row = {"id": rid, "repo": cand["repo"], "issue_url": case["issue"], "symptom": cand["symptom"],
           "fix_url": cand["fix_url"], "pre_fix_sha": cand["pre_fix_sha"],
           "given": {"note_title": case["title"], "run": case["run"], "url": case["url"], "tests": case["tests"]},
           "tests_note": case.get("tests_note"), "started": time.strftime("%H:%M:%S")}
    t = time.time()
    video = None
    try:
        video = download_video(case, cand)
        note = f"Page URL: {case['url']} . User says: {case['title']}"
        row["note"] = note
        RUNS.mkdir(parents=True, exist_ok=True)
        def progress(s):  # one line per case, overwritten, so a long run can be watched
            (RUNS / f".{rid}.stage").write_text(time.strftime("%H:%M:%S ") + s)
        r = pipeline.run_repo(str(video), note, rid, f"https://github.com/{cand['repo']}.git", cand["pre_fix_sha"],
                              case["run"], case["url"], test_cmd=case["tests"], deadline_s=DEADLINE_S,
                              on_progress=progress)
        v = r.get("verdict") or {}
        f = r.get("fix") or {}
        SHOTS.mkdir(parents=True, exist_ok=True)
        for d, key, name in ((v, "screenshot_png", f"real_{rid}.png"), (f, "fixed_png", f"real_{rid}_fixed.png")):
            png = d.pop(key, None)
            if png:
                (SHOTS / name).write_bytes(png)
        row.update(final=r["final"], report=r["report"], gate=r["gate"], verdict=v or None, fix=f or None,
                   timings={k: round(x, 1) for k, x in r["timings"].items()})
        row["leak_scan"] = leak_scan(case, cand, ("reproduce", v.get("trace")), ("fix", f.get("trace")))
    except Exception as e:
        row["final"] = "error"
        row["error"] = f"{type(e).__name__}: {str(e)[:1500]}"
        row["traceback"] = traceback.format_exc()[-3000:]
    row["total_s"] = round(time.time() - t)
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"{rid}.json").write_text(json.dumps(row, indent=1, default=str))
    if video and video.exists():
        video.unlink()  # this machine is short on disk
    print(time.strftime("%H:%M:%S"), "done", rid, row.get("final"), (row.get("fix") or {}).get("status"),
          "leak_hits=%d" % len(row.get("leak_scan") or []), row.get("error", "")[:200], f"{row['total_s']}s", flush=True)
    return row


def one_extended(case, repro_timeout=1080, deadline=1800):
    """Second pass for cases whose first run hit the reproduce time limit: same watcher report (not re-watched),
    fresh sandbox, longer limits. Saved separately as <id>__extended.json and never counted in the main summary."""
    import pipeline
    cand = candidates()[case["issue"]]
    rid = case["id"]
    first = json.loads((RUNS / f"{rid}.json").read_text())
    row = {"id": rid, "pass": "extended", "repro_timeout_s": repro_timeout, "deadline_s": deadline,
           "report_reused_from_first_run": True, "started": time.strftime("%H:%M:%S")}
    t = time.time()
    def progress(s):
        (RUNS / f".{rid}.stage").write_text(time.strftime("%H:%M:%S ") + "[extended] " + s)
    try:
        v = pipeline.reproduce_repo(first["report"], rid, f"https://github.com/{cand['repo']}.git", cand["pre_fix_sha"],
                                    case["run"], case["url"], timeout=repro_timeout, on_progress=progress)
        row["reproduce_s"] = round(time.time() - t)
        f = {}
        if v["verdict"] == "reproduced":
            left = deadline - (time.time() - t)
            t2 = time.time()
            f = pipeline.fix_repo(v, rid, test_cmd=case["tests"], timeout=max(left, 120), on_progress=progress)
            row["fix_s"] = round(time.time() - t2)
        for d, key, name in ((v, "screenshot_png", f"real_{rid}_ext.png"), (f, "fixed_png", f"real_{rid}_ext_fixed.png")):
            png = d.pop(key, None)
            if png:
                (SHOTS / name).write_bytes(png)
        row.update(final=v["verdict"], verdict=v, fix=f or None)
        row["leak_scan"] = leak_scan(case, cand, ("reproduce", v.get("trace")), ("fix", f.get("trace")))
    except Exception as e:
        row["final"] = "error"
        row["error"] = f"{type(e).__name__}: {str(e)[:1500]}"
    row["total_s"] = round(time.time() - t)
    (RUNS / f"{rid}__extended.json").write_text(json.dumps(row, indent=1, default=str))
    print(time.strftime("%H:%M:%S"), "done-extended", rid, row.get("final"), (row.get("fix") or {}).get("status"),
          "leak_hits=%d" % len(row.get("leak_scan") or []), row.get("error", "")[:200], f"{row['total_s']}s", flush=True)
    return row


def collect():
    """Merge saved runs with hand-written scores and count. Counting only; no judging happens here."""
    scores = json.loads(SCORES.read_text()) if SCORES.exists() else {}  # "_notes" and per-case "extended_review" live here too
    rows = []
    for c in CASES:
        p = RUNS / f"{c['id']}.json"
        if not p.exists():
            continue
        r = json.loads(p.read_text())
        v, f, s = r.get("verdict") or {}, r.get("fix") or {}, scores.get(c["id"], {})
        rows.append({
            "id": r["id"], "repo": r["repo"], "issue_url": r["issue_url"], "fix_url": r["fix_url"],
            "pre_fix_sha": r["pre_fix_sha"], "symptom": r["symptom"], "given": r["given"],
            "final_verdict": r.get("final"), "fix_status": f.get("status"),
            "watcher_report": r.get("report"), "gate": r.get("gate"),
            "repro_summary": v.get("summary"), "repro_setup": v.get("setup"), "repro_script": v.get("script_file") or v.get("script"),
            "root_cause_claimed": f.get("root_cause"), "runs_before": f.get("runs_before"), "runs_after": f.get("runs_after"),
            "own_tests": {"command": r["given"]["tests"], "note": r.get("tests_note"),
                          "before": f.get("tests_before"), "after": f.get("tests_after"),
                          "before_output_tail": (f.get("tests_before_output") or "")[-1500:] or None,
                          "after_output_tail": (f.get("tests_after_output") or "")[-1500:] or None},
            "diff": f.get("diff"), "diff_source": f.get("diff_source"),
            "timings_s": r.get("timings"), "total_s": r.get("total_s"),
            "tokens": {"reproduce": v.get("total_tokens"), "fix": f.get("total_tokens")},
            "leak_scan_hits": r.get("leak_scan"), "error": r.get("error"),
            "score": s,  # hand-written: see data/real_scores.json
        })
    def n(key, *vals):
        return sum(1 for r in rows if r["score"].get(key) in vals)
    summary = {
        "attempted": len(rows),
        "setup_failed": n("setup", "failed"),
        "watcher_understood_bug": n("watcher_understood", True),
        "watcher_partly_understood_bug": n("watcher_understood", "partly"),
        "reproduced_claimed_by_pipeline": sum(1 for r in rows if r["final_verdict"] == "reproduced"),
        "reproduced_real_bug_after_review": n("reproduced", True),
        "fixed_per_own_script": sum(1 for r in rows if r["fix_status"] == "fixed"),
        "fix_equivalent_to_maintainers": n("fix_vs_maintainers", "equivalent"),
        "fix_different_but_plausible": n("fix_vs_maintainers", "different_plausible"),
        "fix_partial_same_change_but_incomplete": n("fix_vs_maintainers", "partial"),
        "fix_wrong_or_cosmetic": n("fix_vs_maintainers", "wrong"),
        "fix_equivalent_or_as_good": n("fix_vs_maintainers", "equivalent", "different_plausible"),
        "own_tests_available": sum(1 for r in rows if r["own_tests"]["command"]),
        "own_tests_reached_fix_step": n("own_tests", "still_passing", "inconclusive", "regressed"),
        "own_tests_still_passing_after_fix": n("own_tests", "still_passing"),
        "own_tests_inconclusive": n("own_tests", "inconclusive"),
        "own_tests_regressed": n("own_tests", "regressed"),
        "failed_at": {k: n("failure_stage", k) for k in ("none", "gate", "setup", "reproduce", "fix")},
        "timeouts": sum(1 for r in rows if r["final_verdict"] == "timeout" or r["fix_status"] == "timeout"),
        "cases_with_possible_answer_lookup": n("leak", True),
        "unscored": [r["id"] for r in rows if not r["score"]],
    }
    extended = []
    for c in CASES:
        p = RUNS / f"{c['id']}__extended.json"
        if p.exists():
            e = json.loads(p.read_text())
            v, f = e.get("verdict") or {}, e.get("fix") or {}
            extended.append({"id": e["id"], "final_verdict": e.get("final"), "fix_status": f.get("status"),
                             "reproduce_s": e.get("reproduce_s"), "fix_s": e.get("fix_s"), "total_s": e.get("total_s"),
                             "limits": {"reproduce_s": e.get("repro_timeout_s"), "case_s": e.get("deadline_s")},
                             "repro_summary": v.get("summary"), "root_cause_claimed": f.get("root_cause"),
                             "own_tests": {"before": f.get("tests_before"), "after": f.get("tests_after")},
                             "diff": f.get("diff"), "leak_scan_hits": e.get("leak_scan"),
                             "review": scores.get(c["id"], {}).get("extended_review")})
    old = json.loads(OUT.read_text()) if OUT.exists() else {}
    out = {"what": "ReproBot repo mode on real, already-fixed GitHub issues. Scores are hand-read, not automatic.",
           "generated": time.strftime("%Y-%m-%d %H:%M"), "deadline_s_per_case": DEADLINE_S,
           "summary": summary, "notes": scores.get("_notes") or old.get("notes", []), "cases": rows,
           "extended_pass_not_counted": extended}
    OUT.write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args.pop(0) if args else "list"
    if cmd == "list":
        for c in CASES:
            print(c["id"], "done" if (RUNS / f"{c['id']}.json").exists() else "-", c["issue"])
    elif cmd == "collect":
        print(json.dumps(collect()["summary"], indent=1))
    elif cmd == "run":
        jobs = 2
        if args and args[0] == "-j":
            jobs = min(int(args[1]), 2); args = args[2:]
        by_id = {c["id"]: c for c in CASES}
        extended = "--extended" in args
        cases = [by_id[a] for a in args if a != "--extended"]
        with ThreadPoolExecutor(jobs) as ex:
            list(ex.map(one_extended if extended else one, cases))
    else:
        sys.exit(__doc__)
