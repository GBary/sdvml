"""Registering sdvml as the macOS handler for nxm:// links (Nexus "Mod Manager Download")."""

from __future__ import annotations

import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import APP_DIR

APP_NAME = "SDVML NXM Handler.app"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")


def handler_path() -> Path:
    return APP_DIR / APP_NAME


def register() -> Path:
    """Build a tiny AppleScript app that passes nxm:// links to `sdvml nxm`, and register it."""
    package_root = Path(__file__).resolve().parent.parent
    command = (f"cd {shlex.quote(str(package_root))} && "
               f"{shlex.quote(sys.executable)} -m sdvml nxm --notify ")
    log = APP_DIR / "nxm.log"
    script = f"""
on open location theURL
    do shell script {_applescript_string(command)} & quoted form of theURL & " >> " & quoted form of {_applescript_string(str(log))} & " 2>&1 &"
end open location

on run
    display dialog "This app downloads Nexus Mods files for sdvml. Click 'Mod Manager Download' on nexusmods.com to use it." buttons {{"OK"}}
end run
"""
    app = handler_path()
    if app.exists():
        shutil.rmtree(app)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["osacompile", "-o", str(app), "-e", script], check=True, capture_output=True)

    info_path = app / "Contents" / "Info.plist"
    with open(info_path, "rb") as f:
        info = plistlib.load(f)
    info["CFBundleIdentifier"] = "local.sdvml.nxm-handler"
    info["CFBundleURLTypes"] = [{"CFBundleURLName": "Nexus Mods download", "CFBundleURLSchemes": ["nxm"]}]
    with open(info_path, "wb") as f:
        plistlib.dump(info, f)
    # Editing Info.plist invalidates the ad-hoc signature osacompile applied.
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], capture_output=True)
    subprocess.run([LSREGISTER, "-f", str(app)], capture_output=True)
    _set_default_handler("nxm", "local.sdvml.nxm-handler")
    return app


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _set_default_handler(scheme: str, bundle_id: str) -> None:
    # Make sdvml win over Vortex-style handlers that may also claim nxm:// (LaunchServices API via JXA).
    js = (f'ObjC.import("CoreServices");'
          f'$.LSSetDefaultHandlerForURLScheme($("{scheme}"), $("{bundle_id}"));')
    subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True)


def notify(title: str, message: str) -> None:
    script = f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)
