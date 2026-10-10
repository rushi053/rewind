"""Double-click launchers and start-at-login entries for the Rewind app.

* macOS:   ~/Applications/Rewind.app (a minimal bundle whose executable runs `rewind app`),
           and a LaunchAgent plist for start at login.
* Linux:   ~/.local/share/applications/rewind.desktop (+ icon), and a systemd user unit
           (or an XDG autostart entry when systemd isn't available).
* Windows: Rewind.vbs / Rewind.bat in %LOCALAPPDATA%\\Rewind, Desktop and Start Menu shortcuts,
           and a Startup-folder entry for start at login.

Everything here writes plain text files; the few OS commands (launchctl,
systemctl, PowerShell for .lnk shortcuts) are best-effort and reported back.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

STATIC = Path(__file__).with_name("static")
BUNDLE_ID = "dev.rewind.app"
LAUNCH_AGENT = BUNDLE_ID + ".plist"
SYSTEMD_UNIT = "rewind-app.service"


def rewind_argv():
    """The command that starts Rewind, as absolute paths (GUI launchers don't get your shell PATH)."""
    exe = shutil.which("rewind")
    if exe:
        return [str(Path(exe).resolve()) if os.name != "nt" else exe]
    return [sys.executable, "-m", "rewind_cli"]


def _sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _win_quote(s):
    return '"' + s.replace('"', '""') + '"'


# -------------------------------------------------------------------- macOS
def macos_app(dest_dir, argv=None):
    """Create ``dest_dir/Rewind.app``. Returns the bundle path."""
    argv = argv or rewind_argv()
    app = Path(dest_dir) / "Rewind.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleName": "Rewind",
        "CFBundleDisplayName": "Rewind",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "Rewind",
        "CFBundleIconFile": "Rewind",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "10.13",
        "LSUIElement": True,  # the launcher exits right away; no Dock icon to linger
        "NSHighResolutionCapable": True,
    }
    with open(str(contents / "Info.plist"), "wb") as fh:
        plistlib.dump(info, fh)
    exe = contents / "MacOS" / "Rewind"
    exe.write_text(
        "#!/bin/sh\n"
        "# Opens the Rewind app in your browser (starts it in the background if needed).\n"
        "exec %s app \"$@\" >/dev/null 2>&1\n" % " ".join(_sh_quote(a) for a in argv),
        "utf-8",
    )
    os.chmod(str(exe), 0o755)
    icns = STATIC / "icon.icns"
    if icns.exists():
        shutil.copyfile(str(icns), str(contents / "Resources" / "Rewind.icns"))
    else:
        shutil.copyfile(str(STATIC / "icon-256.png"), str(contents / "Resources" / "Rewind.png"))
    return app


def launchd_plist(argv=None):
    from .store import rewind_home

    argv = argv or rewind_argv()
    log = rewind_home() / "app.log"
    return {
        "Label": BUNDLE_ID,
        "ProgramArguments": list(argv) + ["app", "--foreground", "--no-open"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


# -------------------------------------------------------------------- Linux
def desktop_entry(argv=None, icon="rewind", extra_args=""):
    argv = argv or rewind_argv()
    cmd = " ".join(('"%s"' % a) if " " in a else a for a in argv)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Rewind\n"
        "GenericName=Undo for AI coding agents\n"
        "Comment=Protect folders and undo what AI tools change\n"
        "Exec=%s app%s\n"
        "Icon=%s\n"
        "Terminal=false\n"
        "Categories=Development;Utility;\n"
        "Keywords=undo;backup;snapshot;ai;agent;\n"
        "StartupNotify=false\n" % (cmd, extra_args, icon)
    )


def systemd_unit(argv=None):
    argv = argv or rewind_argv()
    cmd = " ".join(('"%s"' % a) if " " in a else a for a in argv)
    return (
        "[Unit]\n"
        "Description=Rewind app (keeps folder protection running)\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=%s app --foreground --no-open\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n" % cmd
    )


# ------------------------------------------------------------------ Windows
def windows_vbs(argv=None, extra=""):
    """A .vbs that starts Rewind with no console window."""
    argv = argv or rewind_argv()
    cmdline = " ".join(_win_quote(a) for a in argv) + " app" + extra
    return (
        "' Opens the Rewind app (starts it in the background if needed), without a console window.\r\n"
        "CreateObject(\"WScript.Shell\").Run \"%s\", 0, False\r\n" % cmdline.replace('"', '""')
    )


def windows_bat(argv=None):
    argv = argv or rewind_argv()
    return "@echo off\r\nrem Opens the Rewind app in your browser.\r\n%s app\r\n" % " ".join(_win_quote(a) for a in argv)


def _windows_dirs():
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return {
        "home": local / "Rewind",
        "start_menu": appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        "startup": appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup",
        "desktop": Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop",
    }


def _make_lnk(lnk, target_vbs, icon):
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath='wscript.exe';$s.Arguments='\"%s\"';$s.IconLocation='%s';"
        "$s.Description='Rewind';$s.Save()" % (str(lnk).replace("'", "''"), str(target_vbs).replace("'", "''"),
                                               str(icon).replace("'", "''"))
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- install
def install_launcher(platform=None, dest=None, argv=None):
    """Create the double-click launcher for this OS. Returns {"files": [...], "notes": [...]}."""
    platform = platform or sys.platform
    argv = argv or rewind_argv()
    files, notes = [], []
    if platform == "darwin":
        dest = Path(dest) if dest else Path.home() / "Applications"
        app = macos_app(dest, argv)
        files.append(str(app))
        notes.append("Open Rewind from %s (or drag it to the Dock)." % dest)
    elif platform.startswith("win"):
        dirs = _windows_dirs()
        home = Path(dest) if dest else dirs["home"]
        home.mkdir(parents=True, exist_ok=True)
        vbs, bat, ico = home / "Rewind.vbs", home / "Rewind.bat", home / "Rewind.ico"
        vbs.write_text(windows_vbs(argv), "utf-8")
        bat.write_text(windows_bat(argv), "utf-8")
        shutil.copyfile(str(STATIC / "icon.ico"), str(ico))
        files += [str(vbs), str(bat), str(ico)]
        if dest is None:
            for d in (dirs["desktop"], dirs["start_menu"]):
                lnk = d / "Rewind.lnk"
                if d.exists() and _make_lnk(lnk, vbs, ico):
                    files.append(str(lnk))
                else:
                    notes.append("Could not create %s; double-click %s instead." % (lnk, vbs))
    else:
        base = Path(dest) if dest else Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        icon_dir = base / "icons" / "hicolor" / "256x256" / "apps"
        icon_dir.mkdir(parents=True, exist_ok=True)
        icon = icon_dir / "rewind.png"
        shutil.copyfile(str(STATIC / "icon-256.png"), str(icon))
        apps = base / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        entry = apps / "rewind.desktop"
        entry.write_text(desktop_entry(argv, icon=str(icon)), "utf-8")
        os.chmod(str(entry), 0o755)
        files += [str(entry), str(icon)]
        notes.append("Rewind now appears in your applications menu.")
    return {"files": files, "notes": notes}


# --------------------------------------------------------------- autostart
def autostart_path(platform=None):
    platform = platform or sys.platform
    if platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / LAUNCH_AGENT
    if platform.startswith("win"):
        return _windows_dirs()["startup"] / "Rewind.vbs"
    if shutil.which("systemctl"):
        return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd" / "user" / SYSTEMD_UNIT
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "autostart" / "rewind.desktop"


def autostart_enabled(platform=None):
    return autostart_path(platform).exists()


def _run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return {"cmd": " ".join(argv), "ok": r.returncode == 0, "output": (r.stderr or r.stdout).strip()[:300]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"cmd": " ".join(argv), "ok": False, "output": str(exc)}


def set_autostart(on, platform=None, argv=None, run_commands=True):
    """Turn start-at-login on or off. Returns {"enabled", "path", "ran": [...], "notes": [...]}."""
    platform = platform or sys.platform
    argv = argv or rewind_argv()
    path = autostart_path(platform)
    ran, notes = [], []
    if platform == "darwin":
        if on:
            from .store import rewind_home

            rewind_home().mkdir(parents=True, exist_ok=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(str(path), "wb") as fh:
                plistlib.dump(launchd_plist(argv), fh)
            if run_commands:
                ran.append(_run(["launchctl", "unload", str(path)]))
                ran.append(_run(["launchctl", "load", "-w", str(path)]))
        else:
            if run_commands and path.exists():
                ran.append(_run(["launchctl", "unload", "-w", str(path)]))
            _unlink(path)
    elif platform.startswith("win"):
        if on:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(windows_vbs(argv, extra=" --no-open"), "utf-8")
        else:
            _unlink(path)
    elif path.name == SYSTEMD_UNIT:
        if on:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(systemd_unit(argv), "utf-8")
            if run_commands:
                ran.append(_run(["systemctl", "--user", "daemon-reload"]))
                ran.append(_run(["systemctl", "--user", "enable", SYSTEMD_UNIT]))
            notes.append("Rewind will start when you log in. If the app is already running, it keeps running.")
        else:
            if run_commands and path.exists():
                ran.append(_run(["systemctl", "--user", "disable", SYSTEMD_UNIT]))
            _unlink(path)
            if run_commands:
                ran.append(_run(["systemctl", "--user", "daemon-reload"]))
    else:
        if on:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(desktop_entry(argv, extra_args=" --no-open") + "X-GNOME-Autostart-enabled=true\n", "utf-8")
        else:
            _unlink(path)
    failed = [r for r in ran if not r["ok"]]
    if failed:
        notes.append("Some commands failed (protection still starts next time you open Rewind): " +
                     "; ".join("%s: %s" % (r["cmd"], r["output"]) for r in failed))
    return {"enabled": path.exists(), "path": str(path), "ran": ran, "notes": notes}


def _unlink(path):
    try:
        Path(path).unlink()
    except OSError:
        pass
