"""Reviewer: looks through screenshots or a screen recording and flags objective UI defects before a person has to.

  screen   Gemini Decision 1 answers a fixed list of yes/no questions about every frame (fast, cheap, calibrated).
  compare  Gemini Decision 1 compares neighbouring frames: did things move, did the main button jump?
  confirm  Gemini 4 Neon looks again at each suspect frame and must point at the defect, or the flag is dropped.
  numbers  Gemini 4 Hydrogen reads every quantity on every frame; plain code finds the same quantity with two values.

Taste ("too much text", "looks cramped") is deliberately out of scope: every question has a checkable answer.

Usage:  python app/review.py out_dir frame1.png frame2.png ...      (stills, in time order)
        python app/review.py out_dir recording.mov                   (frames are pulled with ffmpeg)
"""
import base64
import html
import json
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from google import genai

NEON, HYDROGEN, DECISION = "gemini-4-neon", "gemini-4-hydrogen", "gemini-decision-1"
beta = genai.Client(http_options={"api_version": "v1beta"})

# One screen at a time. Each question must be answerable from the pixels, with no opinion involved.
SCREEN_CHECKS = {
    "covered": ("Something is drawn on top of content",
                "Is any text, number or icon partly hidden because another element (a character, illustration, bubble, card, "
                "button or bar) is drawn on top of it?"),
    "clipped": ("Text is cut off",
                "Is any line of text cut off mid-line or mid-letter, or does it run underneath a button or bar so that it "
                "cannot be read in full?"),
    "cropped": ("A main element is cut by the screen edge",
                "Is a main visual element such as a chart, ring, card or image cut off by the top or bottom of the screen "
                "or by a navigation bar, as if the page were scrolled to an awkward position?"),
    "bleed": ("Content shows through a bar",
              "Is page content visible through a translucent tab bar or toolbar so that its text collides with the bar's "
              "own icons or labels?"),
    "unreachable": ("Choices are hidden behind a fixed button",
                    "Are selectable options or form fields hidden underneath or below a fixed bottom button, so that a person "
                    "would not know they exist without scrolling?"),
    "number_format": ("A number is formatted inconsistently",
                      "Is a number of 1,000 or more written without a thousands separator while the screen is in English?"),
    "empty": ("The screen looks unfinished",
              "Does the screen show a large blank region where content is clearly meant to be, or placeholder or debug text?"),
    "off_target": ("A highlight misses its target",
                   "Is there a spotlight, highlight ring or pointer that is visibly not centred on the thing it points at, "
                   "so part of the target is left dim or outside it?"),
}
# Two neighbouring frames at a time.
PAIR_CHECKS = {
    "shift": ("Content jumped while the screen stayed the same",
              "These are two consecutive captures. If they show the same screen, did existing content (headings, fields, "
              "rows) move up or down between them, other than by the person scrolling?"),
    "button_moved": ("The main button changed position",
                     "These are two consecutive steps of one flow. Is the primary action button at the bottom in a visibly "
                     "different vertical position in the second image than in the first?"),
}
THRESHOLD = 0.6   # Decision 1 probability at which a frame becomes a suspect

CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "real": {"type": "boolean", "description": "True only if you can point at the exact place it happens"},
        "where": {"type": "string", "description": "Which element, and where on the screen"},
        "what": {"type": "string", "description": "One sentence a developer could act on"},
        "box": {"type": "array", "items": {"type": "number"},
                "description": "[left, top, right, bottom] of the defect as fractions of the image, 0 to 1"},
    },
    "required": ["real", "where", "what", "box"],
}
NUMBERS_SCHEMA = {
    "type": "object",
    "properties": {"quantities": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "What the number measures, lowercase, e.g. 'daily calories', 'weekly pace lb'"},
                       "value": {"type": "string", "description": "Exactly as shown"}},
        "required": ["name", "value"]}}},
    "required": ["quantities"],
}


def _img(path):
    p = pathlib.Path(path)
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return {"type": "image", "data": base64.b64encode(p.read_bytes()).decode(), "mime_type": mime}


def _wait(it):
    while getattr(it, "status", None) in ("in_progress", "queued"):
        time.sleep(1)
        it = beta.interactions.get(id=it.id)
    return it


def frames_from_video(video, out_dir, every=1.5):
    out = pathlib.Path(out_dir) / "frames"
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-vf", f"fps=1/{every},scale=-2:1400",
                    str(out / "f%03d.jpg")], check=True)
    return sorted(str(p) for p in out.glob("f*.jpg"))


def screen(frame):
    qs = {k: {"type": "noul", "instructions": q} for k, (_, q) in SCREEN_CHECKS.items()}
    a = beta.decisions.create(model=DECISION, input=[_img(frame)], questions=qs).answers
    return {k: (round(a[k].noul, 2), round(a[k].confidence, 2)) for k in SCREEN_CHECKS}


def compare(first, second):
    qs = {k: {"type": "noul", "instructions": q} for k, (_, q) in PAIR_CHECKS.items()}
    qs["same_screen"] = {"type": "noul", "instructions": "Do the two images show the same screen of the app (same title and purpose)?"}
    a = beta.decisions.create(model=DECISION, input=[_img(first), _img(second)], questions=qs).answers
    return {k: (round(a[k].noul, 2), round(a[k].confidence, 2)) for k in qs}


def confirm(kind, question, frames):
    """Second opinion from a stronger model. It has to locate the defect, otherwise the flag is dropped."""
    prompt = (f"A fast checker suspects this UI defect: {question}\n"
              "Look carefully. Answer real=true only if it is objectively there and you can point at it. "
              "Do not report matters of taste (too much text, colours, spacing you would choose differently). "
              "Ignore anything caused by a test stub or the simulator itself."
              + (" The box refers to the second image." if len(frames) > 1 else ""))
    it = _wait(beta.interactions.create(
        model=NEON, input=[_img(f) for f in frames] + [{"type": "text", "text": prompt}],
        response_format={"type": "text", "mime_type": "application/json", "schema": CONFIRM_SCHEMA},
        generation_config={"thinking_level": "medium"}))
    return json.loads(it.output_text)


def numbers(frame):
    it = _wait(beta.interactions.create(
        model=HYDROGEN,
        input=[_img(frame), {"type": "text", "text": "List every quantity shown on this app screen that describes the person "
                             "or their plan (targets, totals, rates, counts). Skip times of day, version numbers and prices."}],
        response_format={"type": "text", "mime_type": "application/json", "schema": NUMBERS_SCHEMA},
        generation_config={"thinking_level": "low"}))
    return json.loads(it.output_text)["quantities"]


def _digits(v):
    return "".join(c for c in v if c.isdigit() or c == ".").strip(".")


def review(frames, confirm_step=True, workers=6):
    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        screens = list(ex.map(screen, frames))
        pairs = list(ex.map(lambda i: compare(frames[i], frames[i + 1]), range(len(frames) - 1)))
        nums = list(ex.map(numbers, frames))
    t_fast = time.time() - t0

    suspects = []
    for i, res in enumerate(screens):
        for k, (p, c) in res.items():
            if p >= THRESHOLD:
                suspects.append({"kind": k, "frames": [i], "p": p, "confidence": c, "question": SCREEN_CHECKS[k][1], "label": SCREEN_CHECKS[k][0]})
    for i, res in enumerate(pairs):
        same = res["same_screen"][0] >= 0.5
        for k in PAIR_CHECKS:
            p, c = res[k]
            if p >= THRESHOLD and (same if k == "shift" else not same):
                suspects.append({"kind": k, "frames": [i, i + 1], "p": p, "confidence": c, "question": PAIR_CHECKS[k][1], "label": PAIR_CHECKS[k][0]})

    def check(s):
        s = dict(s)
        if confirm_step:
            v = confirm(s["kind"], s["question"], [frames[i] for i in s["frames"]])
            s.update(confirmed=v["real"], where=v["where"], what=v["what"], box=v["box"])
        else:
            s.update(confirmed=True, where="", what=s["label"], box=[])
        return s
    with ThreadPoolExecutor(workers) as ex:
        checked = list(ex.map(check, suspects))

    # the same quantity shown with two different values anywhere in the flow: plain code, no model
    seen, clashes = {}, []
    for i, qs in enumerate(nums):
        for q in qs:
            name, val = q["name"].strip().lower(), _digits(q["value"])
            if not val:
                continue
            if name in seen and seen[name][0] != val and not any(c["name"] == name for c in clashes):
                clashes.append({"kind": "mismatch", "label": "One quantity, two values", "frames": [seen[name][1], i], "confirmed": True,
                                "name": name, "what": f"'{name}' is shown as {seen[name][2]} on one screen and {q['value']} on another",
                                "where": "", "box": [], "p": 1.0, "confidence": 1.0})
            seen.setdefault(name, (val, i, q["value"]))

    return {"frames": frames, "screens": screens, "pairs": pairs, "numbers": nums,
            "findings": [s for s in checked if s["confirmed"]] + clashes,
            "dropped": [s for s in checked if not s["confirmed"]],
            "seconds": {"fast_checks": round(t_fast, 1), "total": round(time.time() - t0, 1)}}


def baseline(frames):
    """One Neon call over all frames: 'what is wrong here?' For comparison."""
    schema = {"type": "object", "properties": {"findings": {"type": "array", "items": {
        "type": "object", "properties": {"frame": {"type": "integer"}, "what": {"type": "string"}}, "required": ["frame", "what"]}}},
        "required": ["findings"]}
    it = _wait(beta.interactions.create(
        model=NEON, input=[_img(f) for f in frames] + [{"type": "text", "text":
            "These are consecutive captures of a mobile app, numbered from 0. List the objective UI defects (not matters of taste)."}],
        response_format={"type": "text", "mime_type": "application/json", "schema": schema},
        generation_config={"thinking_level": "high"}))
    return json.loads(it.output_text)["findings"]


def report_html(result, out_dir):
    out = pathlib.Path(out_dir)
    rows = []
    for f in result["findings"]:
        shots = ""
        for n, i in enumerate(f["frames"]):
            src = "data:image/jpeg;base64," + base64.b64encode(pathlib.Path(result["frames"][i]).read_bytes()).decode()
            box = ""
            if f.get("box") and len(f["box"]) == 4 and n == len(f["frames"]) - 1:
                l, t, r, b = [max(0, min(1, x)) for x in f["box"]]
                box = f'<i style="left:{l*100:.1f}%;top:{t*100:.1f}%;width:{(r-l)*100:.1f}%;height:{(b-t)*100:.1f}%"></i>'
            shots += f'<figure><div class="shot"><img src="{src}" alt="frame {i}">{box}</div><figcaption>frame {i}</figcaption></figure>'
        rows.append(f'<article><div class="txt"><h2>{html.escape(f["label"])}</h2><p>{html.escape(f["what"])}</p>'
                    f'<p class="s">{html.escape(f.get("where",""))}</p><p class="s">first check {int(f["p"]*100)}% sure, then confirmed by a second model</p></div>'
                    f'<div class="shots">{shots}</div></article>')
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>UI review</title>
<style>body{{font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:24px 18px;background:#f3f4f6;color:#12161c}}
main{{max-width:980px;margin:0 auto;display:grid;gap:14px}}h1{{margin:0;font-size:26px}}.s{{color:#586170;font-size:14px;margin:4px 0 0}}
article{{background:#fff;border:1px solid #d6dae1;border-radius:10px;padding:16px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px}}
h2{{margin:0 0 4px;font-size:18px}}p{{margin:0}}.shots{{display:flex;gap:10px}}figure{{margin:0;text-align:center;font-size:12px;color:#586170}}
.shot{{position:relative;width:170px}}.shot img{{width:100%;display:block;border-radius:6px;border:1px solid #d6dae1}}
.shot i{{position:absolute;border:3px solid #c2321d;border-radius:4px;box-shadow:0 0 0 2000px rgba(18,22,28,.28);clip-path:inset(-2000px)}}
.shot{{overflow:hidden;border-radius:6px}}@media (max-width:640px){{article{{grid-template-columns:1fr}}}}</style>
<main><h1>{len(result["findings"])} things to fix before anyone watches this</h1>
<p class="s">{len(result["frames"])} frames checked in {result["seconds"]["total"]} seconds. {len(result["dropped"])} first-pass flags were dropped because the second model could not point at them. Matters of taste are not judged.</p>
{"".join(rows) or "<article><p>No objective defects found.</p></article>"}</main>"""
    (out / "review.html").write_text(page)
    slim = {k: v for k, v in result.items() if k != "numbers"} | {"numbers": result["numbers"]}
    (out / "review.json").write_text(json.dumps(slim, indent=1))
    return out / "review.html"


if __name__ == "__main__":
    out_dir, inputs = sys.argv[1], sys.argv[2:]
    pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
    frames = frames_from_video(inputs[0], out_dir) if len(inputs) == 1 and pathlib.Path(inputs[0]).suffix.lower() in (".mov", ".mp4", ".webm") else inputs
    res = review(frames)
    print(f"{len(res['findings'])} findings, {len(res['dropped'])} dropped, {res['seconds']}")
    for f in res["findings"]:
        print(f"  frames {f['frames']}: {f['label']}: {f['what']}")
    print(report_html(res, out_dir))
