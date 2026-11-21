"""Evaluate ReproBot against a single-call baseline on the recorded cases.
Usage: python eval/run.py [case_id ...]   Results: data/eval.json"""
import json, pathlib, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor
ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app")); sys.path.insert(0, str(ROOT / "eval"))
import pipeline
from cases import CASES

def one(case):
    video = str(ROOT / "data" / "videos" / f"{case['id']}.webm")
    note = f"Page URL: {case['url']} . User says: {case['note']}"
    row = {"id": case["id"], "truth": case["truth"], "note": case["note"]}
    try:
        t = time.time()
        b = pipeline.baseline(video, note)
        row["baseline_is_bug"] = b["is_bug"]; row["baseline_title"] = b["title"]; row["baseline_s"] = round(time.time() - t)
    except Exception as e:
        row["baseline_error"] = str(e)[:300]
    try:
        t = time.time()
        r = pipeline.run(video, note, rid=case["id"])
        v = r.get("verdict") or {}
        png = v.pop("screenshot_png", None)
        if png: (ROOT / "data" / "shots").mkdir(exist_ok=True); (ROOT / "data" / "shots" / f"{case['id']}.png").write_bytes(png)
        row.update(final=r["final"], title=r["report"]["title"], is_bug_claimed=r["report"]["is_bug"], gate=r["gate"],
                   summary=v.get("summary"), attempts=v.get("attempts"), system_s=round(time.time() - t), timings=r["timings"])
    except Exception as e:
        row["system_error"] = str(e)[:300]; traceback.print_exc()
    print("done", case["id"], row.get("final"), flush=True)
    return row

if __name__ == "__main__":
    want = set(sys.argv[1:])
    cases = [c for c in CASES if not want or c["id"] in want]
    with ThreadPoolExecutor(5) as ex:
        rows = list(ex.map(one, cases))
    def sys_pred(r):  # reproduced = confirmed real; anything else = not confirmed
        return r.get("final") == "reproduced"
    n = len(rows)
    out = {
        "n": n,
        "system_correct": sum(sys_pred(r) == r["truth"] for r in rows),
        "baseline_correct": sum(r.get("baseline_is_bug") == r["truth"] for r in rows),
        "system_false_alarms": sum(sys_pred(r) and not r["truth"] for r in rows),
        "baseline_false_alarms": sum(bool(r.get("baseline_is_bug")) and not r["truth"] for r in rows),
        "rows": rows,
    }
    (ROOT / "data" / "eval.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}))
