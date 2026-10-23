"""Mod profiles: each profile is its own SMAPI Mods folder, launched via --mods-path.

Layout: <APP_DIR>/profiles/<name>/Mods/<group>/<mod folder with manifest.json>
A "group" is one installed download (one Vortex mod), which may contain several SMAPI mods.
Disabled groups are prefixed with '.', which SMAPI skips.

Vortex deploys every mod into one merged Mods folder, so collections ship "patch" mods with no
manifest whose files overwrite files inside other mods (e.g. a tweaked content.json or a preset
config.json). Those are kept as overlays in <profile>/overlays/<name>/ and copied into the mod
folders they target by apply_overlays(), which runs after every import.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .archives import normalize_tree
from .fomod import FomodInstaller, Selection, find_fomod_root
from .manifest import Manifest, find_mod_dirs, version_at_least
from .paths import APP_DIR

DEFAULT_PROFILE = "default"


class InstallError(Exception):
    pass


def safe_name(name: str) -> str:
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name).strip().lstrip(".")
    return name[:120] or "mod"


@dataclass
class GroupInfo:
    name: str
    path: Path
    enabled: bool
    manifests: list[Manifest]


def load_manifests(directory: Path, include_hidden: bool = False) -> tuple[list[Manifest], list[str]]:
    manifests, errors = [], []
    for mod_dir in find_mod_dirs(directory, include_hidden=include_hidden):
        try:
            manifests.append(Manifest.load(mod_dir))
        except (ValueError, OSError, AttributeError) as e:
            errors.append(f"{mod_dir}: unreadable manifest.json ({e})")
    return manifests, errors


class Profile:
    def __init__(self, name: str = DEFAULT_PROFILE):
        self.name = name
        self.root = APP_DIR / "profiles" / safe_name(name)
        self.mods_dir = self.root / "Mods"
        self.state_file = self.root / "profile.json"
        self.overlays_dir = self.root / "overlays"

    @staticmethod
    def all() -> list["Profile"]:
        base = APP_DIR / "profiles"
        return [Profile(p.name) for p in sorted(base.iterdir()) if p.is_dir()] if base.is_dir() else []

    def exists(self) -> bool:
        return self.mods_dir.is_dir()

    def create(self) -> None:
        self.mods_dir.mkdir(parents=True, exist_ok=True)

    def delete(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # --- state ----------------------------------------------------------------------------

    def load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {"groups": {}}

    def save_state(self, state: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2))

    # --- groups ---------------------------------------------------------------------------

    def _group_path(self, name: str) -> Optional[Path]:
        name = safe_name(name)
        for candidate in (self.mods_dir / name, self.mods_dir / f".{name}"):
            if candidate.is_dir():
                return candidate
        return None

    def groups(self) -> list[GroupInfo]:
        if not self.mods_dir.is_dir():
            return []
        result = []
        for path in sorted(self.mods_dir.iterdir(), key=lambda p: p.name.lstrip(".").lower()):
            if path.is_dir():
                manifests, _ = load_manifests(path, include_hidden=True)
                result.append(GroupInfo(path.name.lstrip("."), path, not path.name.startswith("."), manifests))
        return result

    def find_group(self, query: str) -> GroupInfo:
        groups = self.groups()
        q = query.lower()
        exact = [g for g in groups if g.name.lower() == q]
        by_id = [g for g in groups if any(m.unique_id.lower() == q for m in g.manifests)]
        partial = [g for g in groups if q in g.name.lower()]
        for matches in (exact, by_id, partial):
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise InstallError(f"'{query}' matches several mods: " + ", ".join(g.name for g in matches))
        raise InstallError(f"no installed mod matches '{query}'")

    def set_enabled(self, query: str, enabled: bool) -> GroupInfo:
        group = self.find_group(query)
        if group.enabled != enabled:
            target = self.mods_dir / (group.name if enabled else f".{group.name}")
            group.path.rename(target)
            group.path, group.enabled = target, enabled
        return group

    def has(self, name: str) -> bool:
        return self._group_path(name) is not None or (self.overlays_dir / safe_name(name)).is_dir()

    def overlays(self) -> list[str]:
        if not self.overlays_dir.is_dir():
            return []
        order = list(self.load_state()["groups"])  # install order
        names = [p.name for p in self.overlays_dir.iterdir() if p.is_dir()]
        return sorted(names, key=lambda n: order.index(n) if n in order else len(order))

    def remove(self, query: str) -> GroupInfo:
        overlay = self.overlays_dir / safe_name(query)
        if overlay.is_dir():
            # Files it already copied into other mods stay until those mods are reinstalled.
            shutil.rmtree(overlay)
            state = self.load_state()
            state["groups"].pop(overlay.name, None)
            self.save_state(state)
            return GroupInfo(overlay.name, overlay, False, [])
        group = self.find_group(query)
        shutil.rmtree(group.path)
        state = self.load_state()
        state["groups"].pop(group.name, None)
        self.save_state(state)
        return group

    # --- install --------------------------------------------------------------------------

    def _remove_unique_ids(self, unique_ids: set[str], keep_group: str) -> list[str]:
        """SMAPI refuses to load two mods with the same UniqueID, so installing a newer copy
        replaces the old one wherever it lives."""
        messages = []
        for group in self.groups():
            if group.name == keep_group:
                continue
            for m in group.manifests:
                if m.unique_id.lower() in unique_ids:
                    shutil.rmtree(m.path, ignore_errors=True)
                    messages.append(f"replaced {m.name} {m.version} (from '{group.name}')")
            if not find_mod_dirs(group.path, include_hidden=True):
                shutil.rmtree(group.path, ignore_errors=True)
                state = self.load_state()
                state["groups"].pop(group.name, None)
                self.save_state(state)
        return messages

    def install(self, content: Path, group_name: str, meta: Optional[dict] = None,
                selection: Optional[Selection] = None, interactive: bool = False) -> tuple[list[Manifest], list[str]]:
        """Install extracted mod content as one group. Returns (manifests, messages)."""
        self.create()
        group_name = safe_name(group_name)
        messages: list[str] = []
        tmp_parent = APP_DIR / "tmp"
        tmp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_parent) as tmp:
            fomod_root = find_fomod_root(content)
            if fomod_root is not None:
                installer = FomodInstaller(fomod_root)
                staged = Path(tmp) / "fomod"
                messages += installer.install(installer.resolve(selection, interactive), staged)
                content = staged
            mod_dirs = find_mod_dirs(content)
            if not mod_dirs:
                return [], self._add_overlay(content, group_name, meta)
            manifests, errors = load_manifests(content)
            messages += errors

            messages += self._remove_unique_ids({m.unique_id.lower() for m in manifests}, group_name)
            existing = self._group_path(group_name)
            was_disabled = existing is not None and existing.name.startswith(".")
            if existing is not None:
                shutil.rmtree(existing)
            dest = self.mods_dir / (f".{group_name}" if was_disabled else group_name)
            dest.mkdir(parents=True)
            used: set[str] = set()
            for mod_dir in mod_dirs:
                folder = safe_name(group_name if mod_dir == content else mod_dir.name)
                base, n = folder, 2
                while folder.lower() in used:
                    folder, n = f"{base} ({n})", n + 1
                used.add(folder.lower())
                shutil.copytree(mod_dir, dest / folder)
            normalize_tree(dest)  # strips Vortex markers/Windows junk, clears quarantine

        state = self.load_state()
        state["groups"][group_name] = {**(meta or {}), "unique_ids": [m.unique_id for m in manifests]}
        self.save_state(state)
        return load_manifests(dest, include_hidden=True)[0], messages

    # --- overlays -------------------------------------------------------------------------

    def _add_overlay(self, content: Path, name: str, meta: Optional[dict]) -> list[str]:
        if not any(p.is_file() for p in content.rglob("*")):
            raise InstallError("archive is empty")
        dest = self.overlays_dir / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(content, dest)
        normalize_tree(dest)
        state = self.load_state()
        state["groups"].pop(name, None)  # re-insert at the end: later overlays win
        state["groups"][name] = {**(meta or {}), "overlay": True}
        self.save_state(state)
        return ["no manifest.json: installed as a patch onto other mods' files"]

    def _overlay_indexes(self) -> list[dict[str, list[Path]]]:
        """Folder-name lookups for overlay targets: mod folders first, then group folders."""
        mods: dict[str, list[Path]] = {}
        groups: dict[str, list[Path]] = {}
        for group in self.groups():
            for mod_dir in find_mod_dirs(group.path, include_hidden=True):
                mods.setdefault(mod_dir.name.lstrip(".").lower(), []).append(mod_dir)
            groups.setdefault(group.name.lower(), []).append(group.path)
        return [mods, groups]

    @staticmethod
    def _resolve_overlay_file(parts: tuple, indexes: list[dict[str, list[Path]]]) -> Optional[Path]:
        """Map an overlay file path (as Vortex would deploy it under Mods/) to a real file by
        finding the shallowest folder in its path that names an installed mod."""
        for index in indexes:
            for i, part in enumerate(parts[:-1]):
                candidates = index.get(part.lower())
                if not candidates:
                    continue
                # Several mods can share a folder name; prefer one whose parent matches too.
                parent = parts[i - 1].lower() if i else None
                best = next((c for c in candidates if parent and c.parent.name.lstrip(".").lower() == parent),
                            candidates[0])
                return best.joinpath(*parts[i + 1:])
        return None

    def apply_overlays(self) -> list[str]:
        """Copy every overlay's files into the mods they patch. Returns status messages."""
        messages = []
        indexes = self._overlay_indexes()
        for name in self.overlays():
            root = self.overlays_dir / name
            applied, pending = 0, set()
            for f in sorted(p for p in root.rglob("*") if p.is_file()):
                rel = f.relative_to(root).parts
                target = self._resolve_overlay_file(rel, indexes)
                if target is None:
                    if len(rel) > 1:
                        pending.add(rel[0])
                    continue  # loose readme/screenshot files at the archive root
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                applied += 1
            if pending:
                messages.append(f"patch '{name}': waiting for {', '.join(sorted(pending))} "
                                f"({applied} files applied so far)")
            elif applied:
                messages.append(f"patch '{name}': applied {applied} files")
            else:
                messages.append(f"patch '{name}': contains no files for any mod (XNB/content mods are not supported)")
        return messages

    # --- validation -----------------------------------------------------------------------

    def check(self) -> list[str]:
        """Report the problems SMAPI would hit at launch: duplicates and missing dependencies."""
        problems = []
        installed: dict[str, list[Manifest]] = {}
        for group in self.groups():
            if not group.enabled:
                continue
            manifests, errors = load_manifests(group.path)
            problems += errors
            for m in manifests:
                installed.setdefault(m.unique_id.lower(), []).append(m)
        for uid, copies in installed.items():
            if len(copies) > 1:
                where = ", ".join(str(c.path.relative_to(self.mods_dir)) for c in copies)
                problems.append(f"duplicate mod {copies[0].unique_id}: {where}")
        for copies in installed.values():
            m = copies[0]
            for dep in m.dependencies:
                found = installed.get(dep.unique_id.lower())
                if not found:
                    if dep.required:
                        problems.append(f"{m.name} requires {dep.unique_id}, which is not installed/enabled")
                elif dep.min_version and not version_at_least(found[0].version, dep.min_version):
                    problems.append(f"{m.name} requires {dep.unique_id} {dep.min_version}+, "
                                    f"but {found[0].version} is installed")
        return problems
