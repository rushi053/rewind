#!/usr/bin/env python3
"""Record a short screen capture of the app's motion design to docs/app-motion.mp4.

    python scripts/record-motion.py

Development-only (needs Playwright + Chromium and ffmpeg). Uses the same demo
home as scripts/screenshots.py and walks through navigation, a protection
toggle, tab switching, the go-back dialog and its preview, the success state
after going back, and the theme switch.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "app-motion.mp4"
DEMO = Path(os.environ.get("REWIND_DEMO_DIR", "/tmp/rewind-motion"))
PY = os.environ.get("REWIND_PYTHON", sys.executable)
W, H = 1280, 800

# A visible pointer, since headless video has none.
CURSOR = """
addEventListener('DOMContentLoaded', () => {
  const c = document.createElement('div');
  c.style.cssText = 'position:fixed;z-index:99999;left:0;top:0;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;'
    + 'background:rgba(91,91,214,.28);border:1.5px solid rgba(91,91,214,.9);pointer-events:none;transition:transform .12s, background .12s;';
  document.body.appendChild(c);
  addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', () => { c.style.transform = 'scale(.7)'; c.style.background = 'rgba(91,91,214,.5)'; }, true);
  addEventListener('mouseup', () => { c.style.transform = ''; c.style.background = 'rgba(91,91,214,.28)'; }, true);
});
"""


def main():
    subprocess.run([PY, str(ROOT / "scripts" / "make-app-demo.py"), str(DEMO)], check=True, stdout=subprocess.DEVNULL)
    home = DEMO / "home"
    env = dict(os.environ, HOME=str(home), REWIND_HOME=str(home / ".rewind"), REWIND_NO_NOTIFY="1",
               PYTHONPATH=str(ROOT / "src"), REWIND_APP_PORT="47914",
               PATH=str(home / ".local" / "bin") + os.pathsep + os.environ.get("PATH", ""))
    srv = subprocess.Popen([PY, "-m", "rewind_cli", "app", "--foreground", "--no-open"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    for _ in range(100):
        line = srv.stdout.readline()
        if "running at" in line:
            url = line.split("running at ", 1)[1].strip()
            break
    if not url:
        srv.kill()
        raise SystemExit("app did not start")
    reg = json.loads((home / ".rewind" / "registry.json").read_text())
    ids = {f["name"]: f["id"] for f in reg["folders"]}
    vid_dir = Path(tempfile.mkdtemp())
    from playwright.sync_api import sync_playwright

    def glide(page, sel, steps=10):
        box = page.locator(sel).first.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=steps)

    def tap(page, sel, wait=650):
        glide(page, sel)
        page.wait_for_timeout(90)
        page.click(sel)
        page.wait_for_timeout(wait)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # warm the font cache so the recording starts with the real typeface
            warm = browser.new_page()
            warm.goto(url + "#/")
            warm.wait_for_timeout(600)
            warm.close()
            ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(vid_dir),
                                      record_video_size={"width": W, "height": H}, color_scheme="light")
            ctx.add_init_script("localStorage.setItem('rewind.theme', 'light')")
            ctx.add_init_script(CURSOR)
            page = ctx.new_page()
            page.goto(url + "#/")
            page.mouse.move(W / 2, H / 2)
            page.wait_for_timeout(700)
            tap(page, "[data-toggle='%s']" % ids["notes-api"], 800)
            tap(page, ".side-folders a[href='#/f/%s']" % ids["acme-web"], 700)
            tap(page, ".tab[data-tab='deleted']", 550)
            tap(page, ".tab[data-tab='overview']", 600)
            tap(page, "[data-preset='1h']", 800)
            box = page.locator("#gbRange").bounding_box()
            y = box["y"] + box["height"] / 2
            page.mouse.move(box["x"] + box["width"] * 0.2, y, steps=8)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.55, y, steps=14)
            page.mouse.move(box["x"] + box["width"] * 0.18, y, steps=14)
            page.mouse.up()
            page.wait_for_timeout(700)
            page.locator("#gbOk:not([disabled])").wait_for()
            tap(page, "#gbOk", 1800)
            tap(page, "#themeBtn", 900)
            video = page.video.path()
            ctx.close()
            browser.close()
        OUT.parent.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.4", "-i", str(video),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                        "-r", "30", "-movflags", "+faststart", "-an", str(OUT)], check=True)
        print("wrote", OUT)
    finally:
        srv.terminate()
        srv.wait(10)
        subprocess.run([PY, "-c", "import rewind_cli.registry as r, rewind_cli.watch as w; "
                        "[w.stop_daemon(r.repo_for(e)) for e in r.load()]"], env=env)
        shutil.rmtree(str(vid_dir), ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
