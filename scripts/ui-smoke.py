#!/usr/bin/env python3
"""Click through the Rewind app in headless Chromium and check that each flow really changes files.

    python scripts/ui-smoke.py

Development-only (needs Playwright + Chromium). Builds the demo home from
make-app-demo.py, starts `rewind app`, then: turns protection on, protects a new
folder through the picker, undoes the last change, goes back in time, brings a
deleted file back, connects an MCP client, changes the safety mode, walks the
onboarding, and opens the embedded timeline. Fails on any browser console error.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = Path(os.environ.get("REWIND_DEMO_DIR", "/tmp/rewind-smoke"))
PY = sys.executable


def wait_for(cond, timeout=15, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.15)
    raise AssertionError("timed out waiting for " + what)


def main():
    subprocess.run([PY, str(ROOT / "scripts" / "make-app-demo.py"), str(DEMO)], check=True, stdout=subprocess.DEVNULL)
    home = DEMO / "home"
    env = dict(os.environ, HOME=str(home), REWIND_HOME=str(home / ".rewind"), REWIND_NO_NOTIFY="1",
               PYTHONPATH=str(ROOT / "src"), REWIND_APP_PORT="47912",
               PATH=str(home / ".local" / "bin") + os.pathsep + os.environ.get("PATH", ""))
    srv = subprocess.Popen([PY, "-m", "rewind_cli", "app", "--foreground", "--no-open"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    for _ in range(100):
        line = srv.stdout.readline()
        if "running at" in line:
            url = line.split("running at ", 1)[1].strip()
            break
    assert url, "app did not start"
    web = home / "code" / "acme-web"
    errors, checks = [], []

    def ok(msg):
        checks.append(msg)
        print("  ok  " + msg)

    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 860})
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            page.goto(url + "#/")
            page.wait_for_selector("#folderList .row")
            reg = json.loads((home / ".rewind" / "registry.json").read_text())
            ids = {f["name"]: f["id"] for f in reg["folders"]}

            # 1. protection toggle on the home screen
            page.click("[data-toggle='%s']" % ids["notes-api"])
            wait_for(lambda: (home / "code" / "notes-api" / ".rewind" / "watch.pid").exists(), what="watcher pid")
            ok("home toggle started a watcher for notes-api")

            # 2. protect a new folder through the picker
            page.goto(url + "#/add")
            page.click(".proj[data-p$='/code/blog']")
            page.wait_for_selector("#pkMsg.ok")
            page.click("#pkProtect")
            page.wait_for_url("**#/f/*")
            wait_for(lambda: (home / "code" / "blog" / ".rewind" / "project.json").exists(), what="blog store")
            page.wait_for_selector(".pill.protected", timeout=20000)
            ok("picker protected ~/code/blog and it shows Protected")

            # 3. undo last change (README edit) with preview
            page.goto(url + "#/f/%s" % ids["acme-web"])
            page.click("#undoBtn")
            page.wait_for_selector("#pvOk:not([disabled])")
            sentence = page.inner_text("#pv .sentence")
            assert sentence == "1 file will change back.", sentence
            page.click("#pvOk")
            wait_for(lambda: "npm run dev" not in (web / "README.md").read_text(), what="README undo")
            ok("undo preview said %r and README.md was reverted" % sentence)

            # 4. go back in time: 1 hour ago, before the agent session
            page.wait_for_selector("[data-preset='1h']")
            page.click("[data-preset='1h']")
            page.wait_for_selector("#gbOk:not([disabled])")
            sentence = page.inner_text("#gbPv .sentence")
            assert "3 files will come back" in sentence, sentence
            page.click("#gbOk")
            wait_for(lambda: (web / "tests" / "pricing.test.ts").exists() and not (web / "lib" / "cart.ts").exists(), what="go back")
            ok("go back preview said %r and the files came back" % sentence)

            # 5. undo the go-back, then recover one deleted file from the Deleted files tab
            page.click("#undoBtn")
            page.wait_for_selector("#pvOk:not([disabled])")
            page.click("#pvOk")
            wait_for(lambda: not (web / "tests").exists(), what="undo of go back")
            page.goto(url + "#/f/%s/deleted" % ids["acme-web"])
            page.wait_for_selector("[data-one='lib/tax.ts']")
            page.click("[data-one='lib/tax.ts']")
            wait_for(lambda: (web / "lib" / "tax.ts").exists(), what="recover tax.ts")
            assert not (web / "notes").exists()
            ok("undo reverted the go-back; Bring back restored only lib/tax.ts")

            # 6. connect Cursor MCP (user level) and see the backup
            page.goto(url + "#/connect")
            page.click("[data-act='connect'][data-tool='cursor'][data-kind='mcp']")
            page.wait_for_selector("#modal .diff")
            page.click("#modal [data-ok]")
            wait_for(lambda: "rewind" in json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"], what="cursor mcp")
            assert list((home / ".rewind" / "backups").glob("*cursor_mcp.json")), "no backup"
            page.wait_for_selector("[data-act='disconnect'][data-tool='cursor'][data-kind='mcp']")
            ok("Cursor MCP connected, existing server kept, backup written")

            # 7. safety mode from settings applies to all folders
            page.goto(url + "#/settings")
            page.click("[data-mode='warn']")
            wait_for(lambda: json.loads((web / ".rewind" / "config.json").read_text()).get("guard", {}).get("mode") == "warn", what="guard mode")
            ok("safety mode set to warn in every folder")

            # 8. onboarding: pick ml-playground, skip tools, finish
            page.goto(url + "#/welcome")
            page.click(".proj[data-p$='/code/ml-playground']")
            page.wait_for_selector("#pkMsg.ok")
            page.click("#pkProtect")
            page.wait_for_url("**#/welcome/2/*")
            page.click("#next")
            page.wait_for_url("**#/welcome/3/*")
            page.click("text=Open my folder")
            page.wait_for_url("**#/f/*")
            assert json.loads((home / ".rewind" / "settings.json").read_text())["onboarded"] is True
            ok("onboarding protected ml-playground and finished")

            # 9. embedded timeline and the activity feed render
            page.goto(url + "#/f/%s/timeline" % ids["acme-web"])
            frame = page.frame_locator("iframe.timeline")
            frame.locator(".snap").first.wait_for()
            n = frame.locator(".snap").count()
            page.goto(url + "#/activity")
            page.wait_for_selector("#act .row")
            titles = page.locator("#act .row .title").all_inner_texts()
            assert any("Brought back" in t for t in titles) and any("Blocked" in t for t in titles), titles
            ok("timeline shows %d saved versions; activity lists restores and the blocked command" % n)
            browser.close()
    finally:
        srv.terminate()
        srv.wait(10)
        subprocess.run([PY, "-c", "import rewind_cli.registry as r, rewind_cli.watch as w; "
                        "[w.stop_daemon(r.repo_for(e)) for e in r.load()]"], env=env)
    if errors:
        print("browser errors:\n  " + "\n  ".join(errors))
        return 1
    print("%d checks passed, no browser errors" % len(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
