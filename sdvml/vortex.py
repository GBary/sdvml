"""Understanding Vortex collections (collection.json) and matching their mods to downloads."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .archives import archive_stem, is_archive
from .manifest import load_lenient_json

SMAPI_NEXUS_ID = 2400


@dataclass
class CollectionMod:
    name: str
    version: str = ""
    optional: bool = False
    source_type: str = "nexus"
    mod_id: Optional[int] = None
    file_id: Optional[int] = None
    md5: Optional[str] = None
    file_size: Optional[int] = None
    logical_filename: Optional[str] = None
    file_expression: Optional[str] = None
    url: Optional[str] = None
    instructions: str = ""
    choices: Optional[dict] = None
    domain: str = "stardewvalley"
    file_name: Optional[str] = None  # exact Nexus archive name, when known

    @classmethod
    def from_json(cls, data: dict) -> "CollectionMod":
        src = data.get("source") or {}
        as_int = lambda v: int(v) if v not in (None, "") else None
        return cls(
            name=data.get("name") or src.get("logicalFilename") or "Unnamed mod",
            version=str(data.get("version") or ""),
            optional=bool(data.get("optional")),
            source_type=src.get("type", "nexus"),
            mod_id=as_int(src.get("modId")),
            file_id=as_int(src.get("fileId")),
            md5=(src.get("md5") or "").lower() or None,
            file_size=as_int(src.get("fileSize")),
            logical_filename=src.get("logicalFilename"),
            file_expression=src.get("fileExpression"),
            url=src.get("url"),
            instructions=data.get("instructions") or src.get("instructions") or "",
            choices=data.get("choices"),
            domain=data.get("domainName") or "stardewvalley",
        )

    @classmethod
    def from_graphql(cls, mod_file: dict) -> "CollectionMod":
        f = mod_file["file"]
        uri = f.get("uri") or ""
        return cls(
            name=f["mod"]["name"],
            version=f.get("version") or "",
            optional=bool(mod_file.get("optional")),
            mod_id=int(f["mod"]["modId"]),
            file_id=int(f["fileId"]),
            file_size=int(f["sizeInBytes"]) if f.get("sizeInBytes") else None,
            # Newer uploads have opaque storage paths ("94/2b/1f/<uuid>") instead of file names.
            file_name=uri if "." in uri.rsplit("/", 1)[-1] else None,
        )

    @property
    def is_smapi(self) -> bool:
        return self.mod_id == SMAPI_NEXUS_ID or self.name.lower().startswith("smapi")


@dataclass
class Collection:
    root: Path
    name: str
    author: str
    description: str
    install_instructions: str
    mods: list[CollectionMod] = field(default_factory=list)

    @classmethod
    def from_graphql(cls, rev: dict, root: Path) -> "Collection":
        info = rev.get("collection") or {}
        return cls(
            root=root,
            name=info.get("name") or "Nexus collection",
            author=(info.get("user") or {}).get("name") or "",
            description=info.get("summary") or "",
            install_instructions="",
            mods=[CollectionMod.from_graphql(m) for m in rev.get("modFiles") or []],
        )

    @classmethod
    def load(cls, collection_json: Path) -> "Collection":
        data = load_lenient_json(collection_json)
        info = data.get("info") or {}
        return cls(
            root=collection_json.parent,
            name=info.get("name") or collection_json.parent.name,
            author=info.get("author") or "",
            description=info.get("description") or "",
            install_instructions=info.get("installInstructions") or "",
            mods=[CollectionMod.from_json(m) for m in data.get("mods") or []],
        )

    def bundled_path(self, mod: CollectionMod) -> Optional[Path]:
        """Bundled mods are shipped inside the collection archive under bundled/."""
        bundled = self.root / "bundled"
        if not bundled.is_dir():
            return None
        names = [n for n in (mod.file_expression, mod.logical_filename, mod.name) if n]
        for entry in bundled.iterdir():
            for n in names:
                if entry.name == n or entry.name.lower().startswith(n.lower()):
                    return entry
        return None


def find_collection_json(root: Path) -> Optional[Path]:
    for depth in range(3):
        pattern = "/".join(["*"] * depth + ["collection.json"])
        hits = sorted(root.glob(pattern))
        if hits:
            return hits[0]
    return None


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _simplify(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class DownloadIndex:
    """Archives the user downloaded (from Nexus "Manual download", a Vortex downloads folder
    copied off a Windows PC, etc.), matched to collection entries."""

    def __init__(self, dirs: Iterable[Path]):
        self.files: list[Path] = []
        for d in dirs:
            if d.is_file() and is_archive(d):
                self.files.append(d)
            elif d.is_dir():
                self.files.extend(p for p in sorted(d.rglob("*")) if is_archive(p))
        self._md5_cache: dict[Path, str] = {}

    def md5(self, path: Path) -> str:
        if path not in self._md5_cache:
            self._md5_cache[path] = _md5(path)
        return self._md5_cache[path]

    def match(self, mod: CollectionMod) -> Optional[Path]:
        # 0. The exact file name Nexus serves (browsers may append " (1)" etc.).
        if mod.file_name:
            stem = archive_stem(Path(mod.file_name)).lower()
            for f in self.files:
                if f.name.lower() == mod.file_name.lower() or archive_stem(f).lower().startswith(stem):
                    return f
        # 1. Exact archive hash (collection.json stores the md5 of the archive Vortex installed).
        if mod.md5:
            for f in self.files:
                if (mod.file_size is None or f.stat().st_size == mod.file_size) and self.md5(f) == mod.md5:
                    return f
        # 2. Nexus names manual downloads "<Name>-<modId>-<version>-<timestamp>.<ext>".
        if mod.mod_id:
            tagged = [f for f in self.files if re.search(rf"-{mod.mod_id}-", f.name)]
            if mod.file_size:
                sized = [f for f in tagged if f.stat().st_size == mod.file_size]
                if sized:
                    return sized[0]
            if tagged:
                wanted = _simplify(mod.version)
                return next((f for f in tagged if wanted and wanted in _simplify(f.name)), tagged[-1])
        # 3. Fall back to the logical file name.
        for candidate in (mod.logical_filename, mod.file_expression):
            if candidate:
                key = _simplify(candidate)
                for f in self.files:
                    if key and _simplify(f.name).startswith(key):
                        return f
        return None

