#!/usr/bin/env python3
"""Build a throwaway home directory with protected projects and a day of history.

Used for the app screenshots and for trying the app without touching your real
folders:

    python scripts/make-app-demo.py /tmp/rewind-demo
    HOME=/tmp/rewind-demo/home REWIND_HOME=/tmp/rewind-demo/home/.rewind rewind app

Everything is produced by Rewind's real code paths (hooks, restores, the
seatbelt). The only staging is that snapshot timestamps are spread over the
past day afterwards, so the "go back in time" slider has something to show.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))


def main(base):
    base = Path(base).resolve()
    if base.exists():
        shutil.rmtree(str(base))
    home = base / "home"
    os.environ["HOME"] = str(home)
    os.environ["REWIND_HOME"] = str(home / ".rewind")
    os.environ["REWIND_NO_NOTIFY"] = "1"
    os.environ.pop("REWIND_SESSION", None)
    for d in ("Desktop", "Documents", "code", ".local/bin"):
        (home / d).mkdir(parents=True)
    # a `rewind` on PATH, like a pipx install would create (config snippets show its path)
    shim = home / ".local" / "bin" / "rewind"
    shim.write_text("#!/bin/sh\nPYTHONPATH=%s exec %s -m rewind_cli \"$@\"\n" % (HERE.parent / "src", sys.executable))
    shim.chmod(0o755)
    os.environ["PATH"] = str(shim.parent) + os.pathsep + os.environ.get("PATH", "")

    from rewind_cli import hooks, registry, restore
    from rewind_cli.store import Repo, read_json, write_json

    def git(root, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="Demo", GIT_AUTHOR_EMAIL="demo@example.com",
                   GIT_COMMITTER_NAME="Demo", GIT_COMMITTER_EMAIL="demo@example.com")
        subprocess.run(["git", "-C", str(root)] + list(args), check=True, capture_output=True, env=env)

    def write(root, rel, text):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    # ------------------------------------------------------------ acme-web
    web = home / "code" / "acme-web"
    files = {
        "package.json": '{\n  "name": "acme-web",\n  "private": true,\n  "scripts": {"dev": "next dev", "test": "vitest"}\n}\n',
        "README.md": "# Acme Web\n\nThe storefront.\n",
        ".env": "STRIPE_SECRET_KEY=sk_test_51Hx...\nDATABASE_URL=postgres://localhost/acme\n",
        "app/page.tsx": "export default function Home() {\n  return <main>Welcome to Acme</main>;\n}\n",
        "app/checkout/page.tsx": "import { Cart } from '../../components/Cart';\n\nexport default function Checkout() {\n  return <Cart />;\n}\n",
        "app/products/[id]/page.tsx": "export default function Product({ params }) {\n  return <h1>Product {params.id}</h1>;\n}\n",
        "components/Cart.tsx": "export function Cart() {\n  const total = 0;\n  return <div className=\"cart\">Total: {total}</div>;\n}\n",
        "components/Button.tsx": "export function Button(props) {\n  return <button {...props} />;\n}\n",
        "lib/pricing.ts": "export function price(cents: number) {\n  return (cents / 100).toFixed(2);\n}\n",
        "lib/tax.ts": "export const TAX_RATE = 0.0825;\n\nexport function withTax(cents: number) {\n  return Math.round(cents * (1 + TAX_RATE));\n}\n",
        "tests/pricing.test.ts": "import { price } from '../lib/pricing';\n\ntest('formats cents', () => {\n  expect(price(1999)).toBe('19.99');\n});\n",
        "notes/launch-plan.md": "# Launch plan\n\n- [ ] Stripe live keys\n- [ ] Final copy review\n",
    }
    for rel, text in files.items():
        write(web, rel, text)
    git(web, "init", "-q", "-b", "main")
    git(web, "add", "-A", ":!.env", ":!notes")
    git(web, "commit", "-q", "-m", "Initial storefront")
    repo = registry.setup_folder(web)
    repo.capture(message="initial snapshot", trigger="init")
    entry = registry.register(web, protected=True, source="app")

    def hook(event, payload):
        payload.setdefault("session_id", "demo-claude-1")
        payload.setdefault("cwd", str(web))
        return hooks.run_hook("claude", event, json.dumps(payload))

    # a manual checkpoint, then an agent session that refactors, edits and deletes
    write(web, "lib/pricing.ts", files["lib/pricing.ts"] + "\nexport const FREE_SHIPPING_CENTS = 5000;\n")
    repo.capture(message="before pricing refactor", trigger="manual")
    hook("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": str(web / "components/Cart.tsx")}})
    write(web, "components/Cart.tsx", "import { price } from '../lib/pricing';\n\nexport function Cart({ items = [] }) {\n  const total = items.reduce((s, i) => s + i.cents, 0);\n  return <div className=\"cart\">Total: ${price(total)}</div>;\n}\n")
    hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": str(web / "components/Cart.tsx")}})
    hook("PreToolUse", {"tool_name": "Write", "tool_input": {"file_path": str(web / "lib/cart.ts")}})
    write(web, "lib/cart.ts", "export type Item = { id: string; cents: number };\n")
    hook("PostToolUse", {"tool_name": "Write", "tool_input": {"file_path": str(web / "lib/cart.ts")}})
    hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf tests notes && npm test"}})
    shutil.rmtree(str(web / "tests"))
    shutil.rmtree(str(web / "notes"))
    (web / "lib" / "tax.ts").unlink()
    write(web, "app/checkout/page.tsx", "import { Cart } from '../../components/Cart';\n\nexport default function Checkout() {\n  return <Cart items={[]} />;\n}\n")
    hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf tests notes && npm test"}})
    # the seatbelt stops the next one
    hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "git reset --hard HEAD && git clean -fdx"}})
    write(web, "README.md", "# Acme Web\n\nThe storefront. Run `npm run dev`.\n")
    hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": str(web / "README.md")}})

    # spread the history over the last ~26 hours (newest stays a few minutes old)
    ids = repo.snapshot_ids()
    now = time.time()
    offsets = [26 * 3600, 20 * 3600, 52 * 60, 49 * 60, 46 * 60, 9 * 60, 4 * 60]
    offsets = ([26 * 3600] * max(0, len(ids) - len(offsets)) + offsets)[-len(ids):]
    for sid, off in zip(ids, offsets):
        meta = read_json(repo.snap_path(sid))
        meta["time"] = now - off
        write_json(repo.snap_path(sid), meta)
    for s in repo.list_sessions():
        times = [read_json(repo.snap_path(i))["time"] for i in s["snapshots"] if repo.snap_path(i).exists()]
        if times:
            s["started"], s["last"] = min(times) - 20, max(times)
            repo.save_session(s)

    # ------------------------------------------------------------ other folders
    api = home / "code" / "notes-api"
    write(api, "main.py", "from fastapi import FastAPI\n\napp = FastAPI()\n")
    write(api, "pyproject.toml", "[project]\nname = 'notes-api'\n")
    git(api, "init", "-q", "-b", "main")
    r2 = registry.setup_folder(api)
    r2.capture(message="initial snapshot", trigger="init")
    registry.register(api, protected=False, source="app")
    meta = read_json(r2.snap_path(1))
    meta["time"] = time.time() - 3 * 86400
    write_json(r2.snap_path(1), meta)
    for name in ("ml-playground", "dotfiles", "blog"):
        p = home / "code" / name
        write(p, "README.md", "# %s\n" % name)
        git(p, "init", "-q", "-b", "main")
    write(home / "Desktop" / "client-site", "index.html", "<h1>Hi</h1>\n")
    git(home / "Desktop" / "client-site", "init", "-q", "-b", "main")

    # pretend Claude Code, Cursor and Claude Desktop are installed (config folders only), and
    # connect Claude Code's hooks for all folders through the same code path the app uses
    from rewind_cli import integrations

    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text('{\n  "model": "opus"\n}\n')
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text('{\n  "mcpServers": {\n    "github": {"command": "github-mcp"}\n  }\n}\n')
    integrations.claude_desktop_config_path().parent.mkdir(parents=True)
    integrations.apply(integrations.plan("claude-code", "hooks", "global"))
    registry.log("protected", "Protection turned on for acme-web", folder=entry)
    registry.log("connected", "Connected Claude Code: Safety hooks", detail=str(home / ".claude" / "settings.json"))
    # reorder the activity file chronologically with realistic times
    path = home / ".rewind" / "activity.jsonl"
    events = [json.loads(l) for l in path.read_text().splitlines()]
    stamps = {"protected": now - 26 * 3600, "connected": now - 26 * 3600 + 60, "blocked": now - 9 * 60}
    for ev in events:
        ev["time"] = stamps.get(ev["kind"], ev["time"])
    events.sort(key=lambda e: e["time"])
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    registry.save_settings(onboarded=True)
    print(home)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/rewind-demo")
