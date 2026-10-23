"""Running the game through SMAPI, deploying profiles into the game folder, installing SMAPI."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from . import __version__
from .archives import extract
from .nexus import download
from .paths import APP_DIR, smapi_executable
from .profile import Profile

# Mods the SMAPI installer puts in the game's Mods folder; profiles need them too.
SMAPI_BUNDLED_MODS = ("ConsoleCommands", "SaveBackup")
BACKUP_NAME = "Mods.sdvml-backup"


class LaunchError(Exception):
    pass


def _game_mods(game_dir: Path) -> Path:
    return game_dir / "Mods"


def copy_smapi_bundled_mods(game_dir: Path, profile: Profile) -> None:
    profile.create()
    for source_root in (game_dir / BACKUP_NAME, _game_mods(game_dir)):
        for name in SMAPI_BUNDLED_MODS:
            src, dest = source_root / name, profile.mods_dir / name
            if src.is_dir() and not dest.exists() and not (profile.mods_dir / f".{name}").exists():
                shutil.copytree(src, dest)


def launch(game_dir: Path, profile: Profile, wait: bool = True) -> int:
    exe = smapi_executable(game_dir)
    if exe is None:
        raise LaunchError("SMAPI is not installed in the game folder; run `sdvml install-smapi`")
    if not profile.exists():
        raise LaunchError(f"profile '{profile.name}' does not exist; import a mod pack first")
    copy_smapi_bundled_mods(game_dir, profile)
    cmd = [str(exe), "--mods-path", str(profile.mods_dir)]
    if wait:
        return subprocess.call(cmd, cwd=game_dir)
    subprocess.Popen(cmd, cwd=game_dir, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def deploy(game_dir: Path, profile: Profile) -> str:
    """Point the game's Mods folder at a profile (via symlink) so launching from Steam uses it.
    An existing real Mods folder is moved aside, not deleted."""
    mods = _game_mods(game_dir)
    if not profile.exists():
        raise LaunchError(f"profile '{profile.name}' does not exist")
    if mods.is_symlink():
        mods.unlink()
    elif mods.exists():
        backup = game_dir / BACKUP_NAME
        if backup.exists():
            backup = game_dir / f"{BACKUP_NAME}-{time.strftime('%Y%m%d-%H%M%S')}"
        mods.rename(backup)
    copy_smapi_bundled_mods(game_dir, profile)
    mods.symlink_to(profile.mods_dir, target_is_directory=True)
    return f"{mods} -> {profile.mods_dir}"


def undeploy(game_dir: Path) -> str:
    mods = _game_mods(game_dir)
    if not mods.is_symlink():
        return "game Mods folder is not managed by sdvml; nothing to do"
    mods.unlink()
    backup = game_dir / BACKUP_NAME
    if backup.is_dir():
        backup.rename(mods)
        return "restored original Mods folder"
    mods.mkdir()
    return "created empty Mods folder"


def deployed_profile(game_dir: Path) -> str | None:
    mods = _game_mods(game_dir)
    if mods.is_symlink():
        target = mods.resolve()
        if target.parent.parent == (APP_DIR / "profiles").resolve():
            return target.parent.name
    return None


def install_smapi(game_dir: Path | None = None) -> int:
    """Download the latest SMAPI release and run its macOS installer. With game_dir, the
    installer runs unattended against that folder; otherwise it prompts."""
    req = urllib.request.Request(
        "https://api.github.com/repos/Pathoschild/SMAPI/releases/latest",
        headers={"User-Agent": f"sdvml/{__version__}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        release = json.loads(resp.read().decode())
    asset = next((a for a in release.get("assets", [])
                  if a["name"].endswith("-installer.zip") and "developer" not in a["name"]), None)
    if asset is None:
        raise LaunchError("could not find the SMAPI installer in the latest GitHub release")
    print(f"Downloading {asset['name']} ({release.get('tag_name')})...", flush=True)
    tmp_parent = APP_DIR / "tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_parent) as tmp:
        archive = download(asset["browser_download_url"], Path(tmp))
        root = extract(archive, Path(tmp) / "installer")  # also clears Gatekeeper quarantine
        script = next(root.rglob("install on macOS.command"), None)
        if script is None:
            raise LaunchError("SMAPI installer archive has no 'install on macOS.command'")
        if game_dir is not None:
            installer = script.parent / "internal" / "macOS" / "SMAPI.Installer"
            print(f"Installing SMAPI into {game_dir}...\n", flush=True)
            return subprocess.call([str(installer), "--install", "--game-path", str(game_dir), "--no-prompt"],
                                   cwd=script.parent)
        print("Starting the SMAPI installer; follow its prompts.\n", flush=True)
        return subprocess.call(["/bin/bash", str(script)], cwd=script.parent)
