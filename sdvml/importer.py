"""Importing Vortex mod packs into a profile.

Accepted sources:
  * a Vortex/Nexus collection archive (.7z containing collection.json and maybe bundled/)
    or a Nexus collection URL
  * a zipped or copied Vortex staging folder (%APPDATA%\\Vortex\\stardewvalley\\mods)
  * a zipped or copied Vortex downloads folder (a folder full of mod archives)
  * a zipped Windows "Stardew Valley\\Mods" folder, a single mod archive, or a mod folder
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import nexus
from .archives import ArchiveError, archive_stem, extract, is_archive
from .fomod import FomodError, find_fomod_root, selection_from_vortex
from .manifest import find_mod_dirs
from .paths import APP_DIR
from .profile import InstallError, Profile, safe_name
from .vortex import Collection, CollectionMod, DownloadIndex, find_collection_json

Log = Callable[[str], None]

# Vortex names staging folders "<mod name>-<nexus id>-<version parts>-<timestamp>".
_VORTEX_SUFFIX = re.compile(r"-(\d+)(?:-[\w.]+)*-\d{9,}$")


@dataclass
class ImportResult:
    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    missing: list[CollectionMod] = field(default_factory=list)
    collection: Optional[Collection] = None
    report: Optional[Path] = None


def clean_group_name(name: str) -> tuple[str, Optional[int]]:
    match = _VORTEX_SUFFIX.search(name)
    if match:
        return name[: match.start()] or name, int(match.group(1))
    return name, None


class Importer:
    def __init__(self, profile: Profile, downloads: list[Path], client: Optional[nexus.NexusClient] = None,
                 interactive: bool = False, include_optional: bool = True, reinstall: bool = False,
                 log: Log = print):
        self.profile = profile
        self.downloads = downloads
        self.client = client
        self.interactive = interactive
        self.include_optional = include_optional
        self.reinstall = reinstall
        self.log = log
        self.result = ImportResult()
        self._premium: Optional[bool] = None
        (APP_DIR / "tmp").mkdir(parents=True, exist_ok=True)

    # --- entry point ----------------------------------------------------------------------

    def run(self, source: str) -> ImportResult:
        collection_ref = nexus.parse_collection_url(source) if source.startswith("http") else None
        if collection_ref:
            path = self._fetch_collection(*collection_ref)
            if path is None:
                return self._import_public_collection(*collection_ref)
        else:
            path = Path(source).expanduser()
            if not path.exists():
                raise InstallError(f"{source} does not exist")
        self.profile.create()
        with tempfile.TemporaryDirectory(dir=APP_DIR / "tmp") as tmp:
            root = extract(path, Path(tmp) / "pack") if is_archive(path) else path
            collection_json = find_collection_json(root)
            if collection_json is not None:
                self._import_collection(Collection.load(collection_json), Path(tmp))
            else:
                self._import_loose(root, archive_stem(path) if is_archive(path) else path.name, Path(tmp))
        self._apply_overlays()
        return self.result

    def _apply_overlays(self) -> None:
        messages = self.profile.apply_overlays()
        if messages:
            self.log("Patches:")
        for msg in messages:
            self.log(f"  {msg}")

    def _fetch_collection(self, slug: str, revision: Optional[int]) -> Optional[Path]:
        """Download the collection archive, or return None if that needs a key we don't have."""
        client = self.client or nexus.NexusClient()
        cached = sorted((APP_DIR / "downloads" / "collections").glob(f"{slug}-rev*.7z"))
        if not client.api_key:
            if cached and revision is None:
                return cached[-1]
            return None
        try:
            url, rev = client.collection_download_link(slug, revision)
        except nexus.NexusError as e:
            self.log(f"Couldn't download the collection archive ({e}); using the public mod list.")
            return cached[-1] if cached and revision is None else None
        target = APP_DIR / "downloads" / "collections" / f"{slug}-rev{rev}.7z"
        if target.is_file():
            return target
        self.log(f"Downloading collection {slug} revision {rev}...")
        return nexus.download(url, target.parent, target.name)

    def _import_public_collection(self, slug: str, revision: Optional[int]) -> ImportResult:
        client = self.client or nexus.NexusClient()
        rev = client.collection_mod_list(slug, revision)
        self.log("No Nexus API key, so using the collection's public mod list. FOMOD options the author\n"
                 "picked aren't included in it; installer defaults are used (pass -i to choose).")
        self.profile.create()
        with tempfile.TemporaryDirectory(dir=APP_DIR / "tmp") as tmp:
            self._import_collection(Collection.from_graphql(rev, Path(tmp)), Path(tmp))
        self._apply_overlays()
        return self.result

    # --- install helpers ------------------------------------------------------------------

    def _install(self, content: Path, name: str, meta: dict, choices: Optional[dict] = None) -> bool:
        try:
            manifests, messages = self.profile.install(
                content, name, meta, selection_from_vortex(choices), self.interactive)
        except (InstallError, FomodError, OSError) as e:
            self.result.failed.append(f"{name}: {e}")
            self.log(f"  ✗ {name}: {e}")
            return False
        mods = ", ".join(f"{m.name} {m.version}".strip() for m in manifests) or "patch"
        self.log(f"  ✓ {name}  [{mods}]")
        for msg in messages:
            self.log(f"      {msg}")
        self.result.installed.append(name)
        return True

    def _install_archive(self, archive: Path, name: str, meta: dict, tmp: Path,
                         choices: Optional[dict] = None) -> bool:
        work = Path(tempfile.mkdtemp(dir=tmp))
        try:
            extract(archive, work)
        except ArchiveError as e:
            self.result.failed.append(f"{name}: {e}")
            self.log(f"  ✗ {name}: {e}")
            return False
        return self._install(work, name, meta, choices)

    # --- loose packs (staging folders, Mods folders, folders of archives) ----------------

    def _groups_root(self, root: Path) -> Path:
        """Skip wrapper folders like 'MyPack/' or 'Stardew Valley/Mods/'."""
        while True:
            if find_mod_dirs(root) == [root]:
                return root
            children = [c for c in root.iterdir() if not c.name.startswith(".")]
            dirs = [c for c in children if c.is_dir()]
            mods_dir = next((d for d in dirs if d.name.lower() == "mods"), None)
            if mods_dir is not None and find_mod_dirs(mods_dir):
                root = mods_dir
            elif len(dirs) == 1 and not any(is_archive(c) for c in children) \
                    and len(find_mod_dirs(dirs[0])) != 1 and not _VORTEX_SUFFIX.search(dirs[0].name):
                root = dirs[0]
            else:
                # The remaining children are the groups (a lone child holding one mod is a group).
                return root

    def _import_loose(self, top: Path, source_name: str, tmp: Path) -> None:
        root = self._groups_root(top)
        if find_mod_dirs(root) == [root] or find_fomod_root(root, max_depth=1) is not None:
            name, mod_id = clean_group_name(source_name if root == top else root.name)
            self._install(root, name, {"source": source_name, "mod_id": mod_id})
            return
        self.log(f"Importing mods from {source_name}:")
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            name, mod_id = clean_group_name(archive_stem(entry) if entry.is_file() else entry.name)
            meta = {"source": entry.name, "mod_id": mod_id}
            if is_archive(entry):
                self._install_archive(entry, name, meta, tmp)
            elif entry.is_dir():
                if find_mod_dirs(entry) or find_fomod_root(entry) is not None:
                    self._install(entry, name, meta)
                elif any(is_archive(p) for p in entry.rglob("*")):
                    self._import_loose(entry, entry.name, tmp)
                else:
                    self.result.skipped.append(f"{entry.name}: no mods found")

    # --- collections ----------------------------------------------------------------------

    def _is_premium(self) -> bool:
        if self._premium is None:
            self._premium = False
            if self.client and self.client.api_key:
                try:
                    self._premium = bool(self.client.validate().get("is_premium"))
                except nexus.NexusError as e:
                    self.log(f"  (Nexus API key check failed: {e})")
        return self._premium

    def _already_installed(self, mod: CollectionMod) -> bool:
        if self.reinstall or not self.profile.has(mod.name):
            return False
        entry = self.profile.load_state()["groups"].get(safe_name(mod.name)) or {}
        if mod.file_id:
            return entry.get("file_id") == mod.file_id
        # Bundled/direct mods have no Nexus file id; compare the recorded version instead.
        return bool(mod.version) and entry.get("version") == mod.version

    def _locate(self, mod: CollectionMod, collection: Collection, index: DownloadIndex) -> Optional[Path]:
        if mod.source_type == "bundle":
            return collection.bundled_path(mod)
        found = index.match(mod)
        if found:
            return found
        target_dir = APP_DIR / "downloads"
        try:
            if mod.source_type == "nexus" and mod.mod_id and mod.file_id and self._is_premium():
                url = self.client.download_link(mod.mod_id, mod.file_id, mod.domain)
                self.log(f"  ↓ downloading {mod.name} from Nexus")
                return nexus.download(url, target_dir)
            if mod.source_type == "direct" and mod.url:
                self.log(f"  ↓ downloading {mod.name} from {mod.url}")
                return nexus.download(mod.url, target_dir)
        except (nexus.NexusError, OSError, ValueError, http.client.HTTPException) as e:
            self.log(f"  ! download failed for {mod.name}: {e}")
        return None

    def _import_collection(self, collection: Collection, tmp: Path) -> None:
        self.result.collection = collection
        self.log(f"Collection: {collection.name}" + (f" by {collection.author}" if collection.author else ""))
        self.log(f"  {len(collection.mods)} mods listed")
        index = DownloadIndex(self.downloads)
        for mod in collection.mods:
            label = f"{mod.name} {mod.version}".strip()
            if mod.is_smapi:
                self.result.skipped.append(f"{label}: SMAPI is installed into the game, use `sdvml install-smapi`")
                continue
            if mod.optional and not self.include_optional:
                self.result.skipped.append(f"{label}: optional")
                continue
            if self._already_installed(mod):
                self.result.skipped.append(f"{label}: already installed")
                continue
            location = self._locate(mod, collection, index)
            if location is None:
                self.result.missing.append(mod)
                self.log(f"  … {label}: archive not found (see report)")
                continue
            meta = {"source": location.name, "mod_id": mod.mod_id, "file_id": mod.file_id,
                    "version": mod.version, "collection": collection.name}
            if location.is_dir():
                self._install(location, mod.name, meta, mod.choices)
            else:
                self._install_archive(location, mod.name, meta, tmp, mod.choices)
        if self.result.missing:
            self.result.report = self._write_missing_report(collection)
        else:
            for stale in ("missing-mods.md", "missing-mods.json"):
                (self.profile.root / stale).unlink(missing_ok=True)

    def _write_missing_report(self, collection: Collection) -> Path:
        lines = [
            f"# Missing mods for {collection.name}",
            "",
            "Download each file below with **Manual download** on Nexus Mods (or use the",
            "**Mod Manager Download** button and run `sdvml nxm '<nxm link>'`), save them to",
            f"`{APP_DIR / 'downloads'}` or `~/Downloads`, then run the same import again.",
            "Already-installed mods are skipped.",
            "",
        ]
        for mod in self.result.missing:
            if mod.mod_id:
                link = nexus.mod_page_url(mod.mod_id, mod.file_id, mod.domain)
            else:
                link = mod.url or "(no link recorded)"
            optional = " *(optional)*" if mod.optional else ""
            lines.append(f"- [ ] **{mod.name}** {mod.version}{optional} - {link}")
            if mod.file_name or mod.logical_filename:
                lines.append(f"  - file: {mod.file_name or mod.logical_filename}")
            if mod.instructions:
                lines.append(f"  - instructions: {mod.instructions.strip()}")
        report = self.profile.root / "missing-mods.md"
        report.write_text("\n".join(lines) + "\n")
        # Machine-readable copy for `sdvml open-missing`.
        (self.profile.root / "missing-mods.json").write_text(json.dumps(
            [dataclasses.asdict(m) for m in self.result.missing], indent=1))
        return report
