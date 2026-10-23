"""Locating the game, SMAPI and sdvml's own data directories on macOS."""

from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path
from typing import Iterator, Optional

APP_DIR = Path(
    os.environ.get("SDVML_HOME", "~/Library/Application Support/SDVModLoader")
).expanduser()

STEAM_ROOT = Path("~/Library/Application Support/Steam").expanduser()

# The folder that holds the game's assemblies (and SMAPI, once installed).
_GAME_CANDIDATES = [
    STEAM_ROOT / "steamapps/common/Stardew Valley/Contents/MacOS",
    Path("/Applications/Stardew Valley.app/Contents/MacOS"),
    Path("~/Applications/Stardew Valley.app/Contents/MacOS").expanduser(),
    Path("~/Library/Application Support/GOG.com/Galaxy/Applications/Stardew Valley.app/Contents/MacOS").expanduser(),
]

_GAME_MARKERS = ("Stardew Valley.dll", "StardewValley.dll", "Stardew Valley.deps.json")

# Stardew Valley 1.6 and SMAPI 4 run on .NET 6, which supports macOS 10.15 (Catalina) and newer.
MIN_MACOS = (10, 15)


def macos_version() -> Optional[tuple[int, ...]]:
    """The running macOS version, or None when not on macOS. Python builds made with old SDKs
    report Big Sur and newer as "10.16", which still compares correctly against MIN_MACOS."""
    release = platform.mac_ver()[0]
    if not release:
        return None
    return tuple(int(p) for p in re.findall(r"\d+", release))


def macos_supported(version: Optional[tuple[int, ...]]) -> bool:
    return version is not None and version >= MIN_MACOS


def config_path() -> Path:
    return APP_DIR / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(cfg, indent=2))


def is_game_dir(path: Path) -> bool:
    return any((path / m).exists() for m in _GAME_MARKERS)


def normalize_game_dir(path: Path) -> Path:
    """Accept the .app bundle, the Steam folder or Contents/MacOS itself."""
    path = path.expanduser()
    for candidate in (path, path / "Contents/MacOS", path / "Stardew Valley.app/Contents/MacOS"):
        if is_game_dir(candidate):
            return candidate
    return path


def _steam_library_dirs() -> Iterator[Path]:
    vdf = STEAM_ROOT / "steamapps/libraryfolders.vdf"
    try:
        text = vdf.read_text(errors="replace")
    except OSError:
        return
    for match in re.finditer(r'"path"\s+"([^"]+)"', text):
        yield Path(match.group(1)) / "steamapps/common/Stardew Valley/Contents/MacOS"


def find_game_dir() -> Optional[Path]:
    override = os.environ.get("SDVML_GAME_PATH") or load_config().get("game_path")
    if override:
        path = normalize_game_dir(Path(override))
        return path if is_game_dir(path) else None
    for candidate in [*_GAME_CANDIDATES, *_steam_library_dirs()]:
        if is_game_dir(candidate):
            return candidate
    return None


def smapi_executable(game_dir: Path) -> Optional[Path]:
    exe = game_dir / "StardewModdingAPI"
    return exe if exe.exists() and (game_dir / "StardewModdingAPI.dll").exists() else None


def downloads_dirs() -> list[Path]:
    """Folders searched for mod archives the user downloaded manually."""
    dirs = [APP_DIR / "downloads", Path("~/Downloads").expanduser()]
    return [d for d in dirs if d.is_dir()]
