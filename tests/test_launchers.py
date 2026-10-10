import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rewind_cli import launchers

ROOT = Path(__file__).resolve().parent.parent
ARGV = ["/opt/tools/bin/rewind"]


def test_macos_app_bundle(tmp_path):
    app = launchers.macos_app(tmp_path, ARGV)
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "Rewind" and info["CFBundleIdentifier"] == "dev.rewind.app"
    assert info["CFBundleIconFile"] == "Rewind" and info["LSUIElement"] is True
    exe = app / "Contents" / "MacOS" / "Rewind"
    assert os.access(str(exe), os.X_OK)
    assert "exec '/opt/tools/bin/rewind' app" in exe.read_text()
    icns = (app / "Contents" / "Resources" / "Rewind.icns").read_bytes()
    assert icns[:4] == b"icns" and int.from_bytes(icns[4:8], "big") == len(icns)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell script")
def test_macos_app_executable_runs_rewind_app(tmp_path):
    marker = tmp_path / "called"
    fake = tmp_path / "fake-rewind"
    fake.write_text("#!/bin/sh\necho \"$@\" > %s\n" % marker)
    fake.chmod(0o755)
    app = launchers.macos_app(tmp_path / "out", [str(fake)])
    subprocess.run([str(app / "Contents" / "MacOS" / "Rewind")], check=True, timeout=10)
    assert marker.read_text().strip() == "app"


def test_icns_and_ico_containers_are_well_formed():
    static = ROOT / "src" / "rewind_cli" / "static"
    data = (static / "icon.icns").read_bytes()
    pos, types = 8, []
    while pos < len(data):
        types.append(data[pos:pos + 4].decode())
        size = int.from_bytes(data[pos + 4:pos + 8], "big")
        assert data[pos + 8:pos + 16] == b"\x89PNG\r\n\x1a\n"
        pos += size
    assert pos == len(data) and {"ic07", "ic08", "ic09"} <= set(types)
    ico = (static / "icon.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00" and int.from_bytes(ico[4:6], "little") == 6


@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="needs sh")
def test_build_macos_app_script(tmp_path):
    env = dict(os.environ, REWIND_BIN="/opt/tools/bin/rewind")
    out = subprocess.run(["sh", str(ROOT / "scripts" / "build-macos-app.sh"), str(tmp_path)], env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    app = tmp_path / "Rewind.app"
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    from rewind_cli import __version__

    assert info["CFBundleShortVersionString"] == __version__ and info["CFBundleExecutable"] == "Rewind"
    assert '"/opt/tools/bin/rewind" app' in (app / "Contents" / "MacOS" / "Rewind").read_text()
    assert (app / "Contents" / "Resources" / "Rewind.icns").exists()
    env.pop("REWIND_BIN")
    env["PATH"] = "/nonexistent"
    fail = subprocess.run(["/bin/sh", str(ROOT / "scripts" / "build-macos-app.sh"), str(tmp_path / "x")], env=env,
                          capture_output=True, text=True, timeout=30)
    assert fail.returncode == 1 and "not on your PATH" in fail.stderr


def test_linux_launcher(tmp_path, fake_home):
    res = launchers.install_launcher(platform="linux", dest=tmp_path, argv=ARGV)
    entry = tmp_path / "applications" / "rewind.desktop"
    icon = tmp_path / "icons" / "hicolor" / "256x256" / "apps" / "rewind.png"
    assert str(entry) in res["files"] and icon.read_bytes()[:4] == b"\x89PNG"
    text = entry.read_text()
    assert "Exec=/opt/tools/bin/rewind app\n" in text and "Icon=%s\n" % icon in text and "Terminal=false" in text
    spaced = launchers.desktop_entry(["/Applications/My Tools/rewind"])
    assert 'Exec="/Applications/My Tools/rewind" app' in spaced


def test_windows_launchers(tmp_path):
    res = launchers.install_launcher(platform="win32", dest=tmp_path, argv=["C:\\Users\\me\\.local\\bin\\rewind.exe"])
    vbs = (tmp_path / "Rewind.vbs").read_text()
    assert 'CreateObject("WScript.Shell").Run' in vbs and ', 0, False' in vbs
    assert '""C:\\Users\\me\\.local\\bin\\rewind.exe"" app' in vbs
    bat = (tmp_path / "Rewind.bat").read_text()
    assert '"C:\\Users\\me\\.local\\bin\\rewind.exe" app' in bat
    assert (tmp_path / "Rewind.ico").exists() and len(res["files"]) == 3  # no shortcuts when dest is given


def test_autostart_files_per_platform(fake_home, monkeypatch):
    mac = launchers.set_autostart(True, platform="darwin", argv=ARGV, run_commands=False)
    plist = plistlib.loads(Path(mac["path"]).read_bytes())
    assert Path(mac["path"]) == fake_home / "Library" / "LaunchAgents" / "dev.rewind.app.plist"
    assert plist["ProgramArguments"] == ARGV + ["app", "--foreground", "--no-open"] and plist["RunAtLoad"]
    assert launchers.autostart_enabled("darwin")
    assert launchers.set_autostart(False, platform="darwin", run_commands=False)["enabled"] is False

    monkeypatch.setattr(launchers.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    lin = launchers.set_autostart(True, platform="linux", argv=ARGV, run_commands=False)
    unit = Path(lin["path"])
    assert unit == fake_home / ".config" / "systemd" / "user" / "rewind-app.service"
    assert "ExecStart=/opt/tools/bin/rewind app --foreground --no-open" in unit.read_text()
    launchers.set_autostart(False, platform="linux", run_commands=False)
    assert not unit.exists()

    monkeypatch.setattr(launchers.shutil, "which", lambda name: None)
    xdg = launchers.set_autostart(True, platform="linux", argv=ARGV, run_commands=False)
    assert xdg["path"].endswith("autostart/rewind.desktop") and "--no-open" in Path(xdg["path"]).read_text()

    monkeypatch.setenv("APPDATA", str(fake_home / "AppData" / "Roaming"))
    win = launchers.set_autostart(True, platform="win32", argv=ARGV, run_commands=False)
    assert "Startup" in win["path"] and "app --no-open" in Path(win["path"]).read_text()


def test_autostart_reports_failed_commands(fake_home, monkeypatch):
    monkeypatch.setattr(launchers, "_run", lambda argv: {"cmd": " ".join(argv), "ok": False, "output": "no launchd here"})
    res = launchers.set_autostart(True, platform="darwin", argv=ARGV)
    assert res["enabled"] and any("no launchd here" in n for n in res["notes"])


def test_cli_install_launcher_and_autostart(cli, tmp_path, fake_home, monkeypatch):
    if sys.platform.startswith("linux"):
        code, out, _ = cli("app", "install-launcher", "--dest", str(tmp_path / "share"))
        assert code == 0 and "rewind.desktop" in out
    monkeypatch.setattr(launchers, "_run", lambda argv: {"cmd": " ".join(argv), "ok": True, "output": ""})
    code, out, _ = cli("app", "--autostart", "on")
    assert code == 0 and "Start at login is on" in out
    code, out, _ = cli("app", "--autostart", "off")
    assert "Start at login is off" in out


def test_static_launchers_in_repo():
    d = ROOT / "scripts" / "launchers"
    assert "rewind app" in (d / "Rewind.bat").read_text()
    assert 'Run "rewind app", 0, False' in (d / "Rewind.vbs").read_text()
    assert "Exec=rewind app" in (d / "rewind.desktop").read_text()


def test_bundled_fonts_ship_with_their_license():
    fonts = ROOT / "src" / "rewind_cli" / "static" / "fonts"
    for name in ("Geist-Variable.woff2", "GeistMono-Variable.woff2"):
        assert (fonts / name).read_bytes()[:4] == b"wOF2"
    assert "SIL Open Font License" in (fonts / "LICENSE-Geist.txt").read_text()
