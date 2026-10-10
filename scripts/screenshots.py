#!/usr/bin/env python3
"""Take the README screenshots of the Rewind app with Playwright + Chromium.

    python scripts/screenshots.py            # builds /tmp/rewind-demo, starts the app, writes docs/app-*.png

Development-only (needs `pip install playwright && playwright install chromium`).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DEMO = Path(os.environ.get("REWIND_DEMO_DIR", "/tmp/rewind-demo"))
PY = os.environ.get("REWIND_PYTHON", sys.executable)


def main():
    subprocess.run([PY, str(ROOT / "scripts" / "make-app-demo.py"), str(DEMO)], check=True)
    home = DEMO / "home"
    env = dict(os.environ, HOME=str(home), REWIND_HOME=str(home / ".rewind"), REWIND_NO_NOTIFY="1",
               PYTHONPATH=str(ROOT / "src"), REWIND_APP_PORT="47911",
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
    base, query = url.split("/?")
    reg = json.loads((home / ".rewind" / "registry.json").read_text())
    web = next(f for f in reg["folders"] if f["name"] == "acme-web")["id"]
    from playwright.sync_api import sync_playwright

    shots = [
        ("app-home", "#/", None, "light"),
        ("app-home-dark", "#/", None, "dark"),
        ("app-folder", "#/f/%s" % web, None, "light"),
        ("app-go-back", "#/f/%s" % web, "goback", "light"),
        ("app-recover", "#/f/%s/deleted" % web, None, "light"),
        ("app-connect", "#/connect", None, "light"),
        ("app-connect-confirm", "#/connect", "connect", "light"),
        ("app-settings", "#/settings", None, "light"),
        ("app-activity", "#/activity", None, "light"),
        ("app-timeline", "#/f/%s/timeline" % web, None, "dark"),
        ("app-onboarding", "#/welcome", None, "light"),
        ("app-onboarding-tools", "#/welcome/2/%s" % web, None, "light"),
    ]
    only = set(sys.argv[1:])
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for name, hash_, action, theme in shots:
                if only and name not in only:
                    continue
                ctx = browser.new_context(viewport={"width": 1360, "height": 900}, device_scale_factor=2,
                                          color_scheme=theme)
                page = ctx.new_page()
                page.on("console", lambda m: errors.append("%s: %s" % (name, m.text)) if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append("%s: %s" % (name, e)))
                page.add_init_script("localStorage.setItem('rewind.theme', %r)" % theme)
                page.goto("%s/?%s%s" % (base, query, hash_))
                page.wait_for_timeout(1200)
                if action == "goback":
                    page.click("[data-preset='1h']")
                    page.wait_for_timeout(1200)
                    page.evaluate("document.querySelector('#gbPv details') && (document.querySelector('#gbPv details').open = true)")
                    page.wait_for_timeout(300)
                if action == "connect":
                    page.click("[data-act='connect'][data-tool='cursor'][data-kind='mcp']")
                    page.wait_for_timeout(800)
                if name == "app-onboarding":
                    page.wait_for_timeout(800)
                page.screenshot(path=str(DOCS / (name + ".png")))
                print("wrote", DOCS / (name + ".png"))
                ctx.close()
            browser.close()
    finally:
        srv.terminate()
        srv.wait(10)
        subprocess.run([PY, "-c", "import rewind_cli.registry as r, rewind_cli.watch as w; "
                        "[w.stop_daemon(r.repo_for(e)) for e in r.load()]"], env=env)
    if errors:
        print("\n".join(["browser errors:"] + errors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
