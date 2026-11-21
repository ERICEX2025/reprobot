"""Run the full ReproBot pipeline (report, gate, reproduce, fix) on the Corner Diner cases.
Usage: python eval/run_diner.py [-j N] [case_id ...]
Each case is saved to data/diner_runs/<id>.json as soon as it finishes; data/eval_diner.json is rebuilt from
all saved cases at the end (an existing "summary" key is kept). The pipeline gets only: video, start URL, note."""
import json, pathlib, shutil, sys, tempfile, time, traceback
from concurrent.futures import ThreadPoolExecutor
ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app")); sys.path.insert(0, str(ROOT / "eval"))
import pipeline
from diner_cases import CASES

SOURCE = {"repo_url": "https://github.com/ERICEX2025/reprobot", "subdir": "docs", "site_root": "https://ericko.dev/reprobot"}
RUNS = ROOT / "data" / "diner_runs"; RUNS.mkdir(parents=True, exist_ok=True)
SHOTS = ROOT / "data" / "shots"; SHOTS.mkdir(parents=True, exist_ok=True)
TMP = pathlib.Path(tempfile.mkdtemp(prefix="diner_"))


def one(case):
    row = {"id": case["id"], "rid": case["rid"], "truth": case["truth"], "symptom": case["symptom"], "note": case["note"],
           "url": case["url"], "started": time.strftime("%H:%M:%S")}
    t = time.time()
    try:
        # neutral file name, so nothing but the pixels and the note reaches the model
        video = TMP / f"rec_{case['rid']}.webm"
        shutil.copy(ROOT / "data" / "diner" / f"{case['id']}.webm", video)
        note = f"Page URL: {case['url']} . User says: {case['note']}"
        r = pipeline.run(str(video), note, rid=case["rid"], source=SOURCE)
        v = r.get("verdict") or {}
        f = r.get("fix") or {}
        for d, key, name in ((v, "screenshot_png", f"diner_{case['id']}.png"), (f, "fixed_png", f"diner_{case['id']}_fixed.png")):
            png = d.pop(key, None)
            if png: (SHOTS / name).write_bytes(png)
        row.update(final=r["final"], report=r["report"], gate=r["gate"], verdict=v or None, fix=f or None, timings=r["timings"])
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {str(e)[:1500]}"
        row["traceback"] = traceback.format_exc()[-3000:]
    row["total_s"] = round(time.time() - t)
    (RUNS / f"{case['id']}.json").write_text(json.dumps(row, indent=1, default=str))
    print(time.strftime("%H:%M:%S"), "done", case["id"], row.get("final"), (row.get("fix") or {}).get("status"),
          row.get("error", "")[:200], f"{row['total_s']}s", flush=True)
    return row


def collect():
    rows = [json.loads((RUNS / f"{c['id']}.json").read_text()) for c in CASES if (RUNS / f"{c['id']}.json").exists()]
    out_path = ROOT / "data" / "eval_diner.json"
    out = {"n": len(rows), "source": SOURCE, "rows": rows}
    if out_path.exists():
        old = json.loads(out_path.read_text())
        if "summary" in old: out["summary"] = old["summary"]
    out_path.write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    jobs = 3
    if args and args[0] == "-j": jobs = int(args[1]); args = args[2:]
    jobs = min(jobs, 3)
    want = set(args)
    cases = [c for c in CASES if not want or c["id"] in want]
    with ThreadPoolExecutor(jobs) as ex:
        list(ex.map(one, cases))
    out = collect()
    print("saved", out["n"], "cases to data/eval_diner.json")
