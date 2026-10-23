"""Command-line interface: `sdvml <command>`."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

from . import __version__, launcher, nexus, nxm_handler
from .archives import ArchiveError
from .fomod import FomodError
from .importer import Importer
from .paths import APP_DIR, MIN_MACOS, downloads_dirs, find_game_dir, is_game_dir, load_config, \
    macos_supported, macos_version, normalize_game_dir, save_config, smapi_executable
from .profile import DEFAULT_PROFILE, InstallError, Profile
from .vortex import CollectionMod, DownloadIndex


def _profile(args) -> Profile:
    return Profile(args.profile or load_config().get("active_profile") or DEFAULT_PROFILE)


def _game_dir() -> Path:
    game = find_game_dir()
    if game is None:
        raise launcher.LaunchError("Stardew Valley not found; run `sdvml set-game <path to the game or .app>`")
    return game


def cmd_doctor(args) -> int:
    game = find_game_dir()
    print(f"sdvml {__version__}")
    version = macos_version()
    minimum = ".".join(map(str, MIN_MACOS))
    if version is None:
        print(f"macOS:         NOT macOS ({platform.system()}); sdvml only supports macOS")
    elif macos_supported(version):
        print(f"macOS:         {'.'.join(map(str, version))} (ok)")
    else:
        print(f"macOS:         {'.'.join(map(str, version))} - TOO OLD: Stardew Valley 1.6 and SMAPI 4 "
              f"need macOS {minimum} (Catalina) or newer")
    print(f"Data folder:   {APP_DIR}")
    print(f"Game folder:   {game or 'NOT FOUND (use `sdvml set-game`)'}")
    if game:
        print(f"SMAPI:         {'installed' if smapi_executable(game) else 'NOT INSTALLED (use `sdvml install-smapi`)'}")
        deployed = launcher.deployed_profile(game)
        print(f"Game Mods dir: {'-> profile ' + deployed if deployed else 'not managed by sdvml'}")
    profile = _profile(args)
    print(f"Profile:       {profile.name} ({len(profile.groups())} mods)")
    print(f"Downloads:     {', '.join(str(d) for d in downloads_dirs()) or '(none)'}")
    print(f"Nexus API key: {'set' if nexus.NexusClient().api_key or load_config().get('nexus_api_key') else 'not set (optional)'}")
    return 0 if macos_supported(version) else 1


def cmd_set_game(args) -> int:
    path = normalize_game_dir(Path(args.path))
    if not is_game_dir(path):
        print(f"{path} doesn't look like a Stardew Valley install", file=sys.stderr)
        return 1
    cfg = load_config()
    cfg["game_path"] = str(path)
    save_config(cfg)
    print(f"Game folder set to {path}")
    return 0


def cmd_set_key(args) -> int:
    cfg = load_config()
    cfg["nexus_api_key"] = args.key
    save_config(cfg)
    try:
        user = nexus.NexusClient(args.key).validate()
        tier = "premium (automatic downloads enabled)" if user.get("is_premium") else "free"
        print(f"Key saved for {user.get('name')}, {tier} account")
    except nexus.NexusError as e:
        print(f"Key saved, but validation failed: {e}")
    return 0


def _client(args) -> nexus.NexusClient:
    return nexus.NexusClient(getattr(args, "nexus_key", None) or load_config().get("nexus_api_key"))


def cmd_import(args) -> int:
    profile = _profile(args)
    downloads = [Path(d).expanduser() for d in args.downloads] + downloads_dirs()
    importer = Importer(profile, downloads, _client(args), interactive=args.interactive,
                        include_optional=not args.skip_optional, reinstall=args.reinstall)
    result = importer.run(args.source)
    print()
    print(f"Profile '{profile.name}': {len(result.installed)} installed, {len(result.skipped)} skipped, "
          f"{len(result.failed)} failed, {len(result.missing)} missing")
    for line in result.skipped:
        print(f"  skipped: {line}")
    for line in result.failed:
        print(f"  failed:  {line}")
    if result.missing:
        print(f"\n{len(result.missing)} mods still need downloading. Checklist with links:\n  {result.report}")
    if result.collection and result.collection.install_instructions:
        print(f"\nCollection instructions:\n{result.collection.install_instructions.strip()}")
    problems = profile.check()
    if problems:
        print("\nProblems SMAPI will report:")
        for p in problems:
            print(f"  - {p}")
    return 1 if result.failed else 0


def cmd_nxm(args) -> int:
    try:
        info = nexus.parse_nxm(args.link)
        client = _client(args)
        url = client.download_link(info["mod_id"], info["file_id"], info["game"], info["key"], info["expires"])
        path = nexus.download(url, APP_DIR / "downloads")
    except (nexus.NexusError, OSError) as e:
        if args.notify:
            nxm_handler.notify("sdvml download failed", str(e)[:200])
        raise
    print(f"Saved {path}\nRe-run your import to install it.")
    if args.notify:
        nxm_handler.notify("sdvml downloaded", path.name)
    return 0


def cmd_register_nxm(args) -> int:
    app = nxm_handler.register()
    print(f"Registered {app} as the handler for nxm:// links.\n"
          "'Mod Manager Download' buttons on nexusmods.com now download straight into sdvml.")
    return 0


def cmd_open_missing(args) -> int:
    profile = _profile(args)
    try:
        missing = [CollectionMod(**m) for m in json.loads((profile.root / "missing-mods.json").read_text())]
    except OSError:
        print(f"Profile '{profile.name}' has no missing-mods list; run an import first.")
        return 1
    index = DownloadIndex(downloads_dirs())
    todo = [m for m in missing if m.mod_id and m.file_id and index.match(m) is None]
    if args.skip_optional:
        todo = [m for m in todo if not m.optional]
    if not todo:
        print("Everything on the list is downloaded. Re-run the import to install it.")
        return 0
    batch = todo[: args.count]
    for mod in batch:
        subprocess.run(["open", nexus.manager_download_url(mod.mod_id, mod.file_id, mod.domain)])
        print(f"opened {mod.name} {mod.version}")
    print(f"\n{len(todo) - len(batch)} more after these. Run `sdvml open-missing` again for the next batch.")
    return 0


def cmd_list(args) -> int:
    profile = _profile(args)
    groups = profile.groups()
    if not groups and not profile.overlays():
        print(f"Profile '{profile.name}' has no mods.")
        return 0
    for g in groups:
        state = "on " if g.enabled else "off"
        print(f"[{state}] {g.name}")
        for m in g.manifests:
            print(f"        {m.name} {m.version}  ({m.unique_id})")
    for name in profile.overlays():
        print(f"[patch] {name}")
    return 0


def cmd_toggle(args, enabled: bool) -> int:
    profile = _profile(args)
    for name in args.mods:
        group = profile.set_enabled(name, enabled)
        print(f"{'Enabled' if enabled else 'Disabled'} {group.name}")
    return 0


def cmd_remove(args) -> int:
    profile = _profile(args)
    for name in args.mods:
        print(f"Removed {profile.remove(name).name}")
    return 0


def cmd_check(args) -> int:
    problems = _profile(args).check()
    for p in problems:
        print(f"- {p}")
    print("No problems found." if not problems else f"\n{len(problems)} problem(s).")
    return 1 if problems else 0


def cmd_profiles(args) -> int:
    active = load_config().get("active_profile") or DEFAULT_PROFILE
    profiles = Profile.all()
    if not profiles:
        print("No profiles yet; `sdvml import` creates one.")
    for p in profiles:
        print(f"{'*' if p.name == active else ' '} {p.name}  ({len(p.groups())} mods)")
    return 0


def cmd_use(args) -> int:
    cfg = load_config()
    cfg["active_profile"] = args.name
    save_config(cfg)
    Profile(args.name).create()
    print(f"Active profile: {args.name}")
    return 0


def cmd_delete_profile(args) -> int:
    profile = Profile(args.name)
    if not profile.root.exists():
        print(f"No profile named {args.name}", file=sys.stderr)
        return 1
    if not args.yes and input(f"Delete profile '{args.name}' and all its mods? [y/N] ").lower() != "y":
        return 1
    profile.delete()
    print(f"Deleted {args.name}")
    return 0


def cmd_launch(args) -> int:
    return launcher.launch(_game_dir(), _profile(args), wait=not args.detach)


def cmd_deploy(args) -> int:
    print(launcher.deploy(_game_dir(), _profile(args)))
    return 0


def cmd_undeploy(args) -> int:
    print(launcher.undeploy(_game_dir()))
    return 0


def cmd_install_smapi(args) -> int:
    return launcher.install_smapi(_game_dir() if args.yes else None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdvml", description="Stardew Valley mod loader for macOS - installs Vortex mod packs into SMAPI.")
    parser.add_argument("--version", action="version", version=f"sdvml {__version__}")
    parser.add_argument("-p", "--profile", help="profile to act on (default: the active profile)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="show what sdvml detected").set_defaults(func=cmd_doctor)

    p = sub.add_parser("set-game", help="tell sdvml where Stardew Valley is installed")
    p.add_argument("path")
    p.set_defaults(func=cmd_set_game)

    p = sub.add_parser("set-key", help="save a Nexus Mods API key")
    p.add_argument("key")
    p.set_defaults(func=cmd_set_key)

    p = sub.add_parser("install-smapi", help="download and run the latest SMAPI installer")
    p.add_argument("-y", "--yes", action="store_true", help="install into the detected game folder without prompts")
    p.set_defaults(func=cmd_install_smapi)

    p = sub.add_parser("import", help="install a Vortex collection, staging/downloads folder, or mod archive")
    p.add_argument("source", help="archive, folder, or Nexus collection URL")
    p.add_argument("-d", "--downloads", action="append", default=[],
                   help="extra folder with downloaded mod archives (repeatable)")
    p.add_argument("--nexus-key", help="Nexus API key (or set NEXUS_API_KEY / `sdvml set-key`)")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="ask about FOMOD options the pack didn't record")
    p.add_argument("--skip-optional", action="store_true", help="skip mods the collection marks optional")
    p.add_argument("--reinstall", action="store_true", help="reinstall mods that are already installed")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("nxm", help="download a file from a Nexus 'Mod Manager Download' nxm:// link")
    p.add_argument("link")
    p.add_argument("--nexus-key")
    p.add_argument("--notify", action="store_true", help="show a macOS notification when done")
    p.set_defaults(func=cmd_nxm)
    sub.add_parser("register-nxm", help="make 'Mod Manager Download' links on nexusmods.com download via sdvml") \
        .set_defaults(func=cmd_register_nxm)
    p = sub.add_parser("open-missing", help="open download pages for mods still missing from the last import")
    p.add_argument("-n", "--count", type=int, default=10, help="pages to open at once (default 10)")
    p.add_argument("--skip-optional", action="store_true")
    p.set_defaults(func=cmd_open_missing)

    sub.add_parser("list", help="list installed mods").set_defaults(func=cmd_list)
    for name, enabled in (("enable", True), ("disable", False)):
        p = sub.add_parser(name, help=f"{name} mods by name or UniqueID")
        p.add_argument("mods", nargs="+")
        p.set_defaults(func=lambda a, e=enabled: cmd_toggle(a, e))
    p = sub.add_parser("remove", help="uninstall mods")
    p.add_argument("mods", nargs="+")
    p.set_defaults(func=cmd_remove)
    sub.add_parser("check", help="check for missing dependencies and duplicates").set_defaults(func=cmd_check)

    sub.add_parser("profiles", help="list profiles").set_defaults(func=cmd_profiles)
    p = sub.add_parser("use", help="switch the active profile (creates it if needed)")
    p.add_argument("name")
    p.set_defaults(func=cmd_use)
    p = sub.add_parser("delete-profile", help="delete a profile and its mods")
    p.add_argument("name")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_delete_profile)

    p = sub.add_parser("launch", help="start the game with SMAPI using the profile's mods")
    p.add_argument("--detach", action="store_true", help="don't wait for the game to exit")
    p.set_defaults(func=cmd_launch)
    sub.add_parser("deploy", help="link the game's Mods folder to the profile (for Steam launches)") \
        .set_defaults(func=cmd_deploy)
    sub.add_parser("undeploy", help="restore the game's original Mods folder").set_defaults(func=cmd_undeploy)
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # show progress promptly when piped to a log
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (InstallError, launcher.LaunchError, nexus.NexusError, ArchiveError, FomodError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
