"""Record each case as a real screen video by driving a local browser. Usage: python eval/record.py [case_id ...]"""
import shutil, sys, pathlib
from playwright.sync_api import sync_playwright
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from cases import CASES

OUT = pathlib.Path(__file__).parent.parent / "data" / "videos"
OUT.mkdir(parents=True, exist_ok=True)

def record(case, p):
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1024, "height": 720}, record_video_dir=str(OUT / "tmp"), record_video_size={"width": 1024, "height": 720})
    page = ctx.new_page()
    page.goto(case["url"], wait_until="domcontentloaded")
    for a in case["actions"]:
        kind = a[0]
        if kind == "wait": page.wait_for_timeout(a[1])
        elif kind == "click": page.click(a[1])
        elif kind == "fill": page.fill(a[1], a[2]); page.wait_for_timeout(400)
        elif kind == "reload": page.reload(wait_until="domcontentloaded")
        elif kind == "scroll": page.mouse.wheel(0, a[1])
        elif kind == "hover": page.hover(a[1])
        elif kind == "select": page.select_option(a[1], a[2])
    page.wait_for_timeout(500)
    path = page.video.path()
    ctx.close(); browser.close()
    shutil.move(path, OUT / f"{case['id']}.webm")
    print("recorded", case["id"])

if __name__ == "__main__":
    want = set(sys.argv[1:])
    with sync_playwright() as p:
        for c in CASES:
            if not want or c["id"] in want:
                try: record(c, p)
                except Exception as e: print("FAILED", c["id"], str(e)[:150])
    shutil.rmtree(OUT / "tmp", ignore_errors=True)
