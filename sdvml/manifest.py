"""Reading SMAPI mod manifests and locating mod folders."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _strip_json_comments(text: str) -> str:
    """SMAPI uses Json.NET, which tolerates comments and trailing commas; Python's json does not."""
    out = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_lenient_json(path: Path):
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    return json.loads(_strip_json_comments(text))


@dataclass
class Dependency:
    unique_id: str
    required: bool = True
    min_version: Optional[str] = None


@dataclass
class Manifest:
    path: Path
    unique_id: str
    name: str
    version: str = ""
    author: str = ""
    content_pack_for: Optional[str] = None
    dependencies: list[Dependency] = field(default_factory=list)

    @classmethod
    def load(cls, mod_dir: Path) -> "Manifest":
        data = load_lenient_json(mod_dir / "manifest.json")
        # Json.NET binds properties case-insensitively.
        data = {k.lower(): v for k, v in data.items()}
        deps = []
        for dep in data.get("dependencies") or []:
            dep = {k.lower(): v for k, v in dep.items()}
            if dep.get("uniqueid"):
                deps.append(Dependency(dep["uniqueid"], dep.get("isrequired", True) is not False, dep.get("minimumversion")))
        cpf = data.get("contentpackfor")
        if isinstance(cpf, dict):
            cpf = {k.lower(): v for k, v in cpf.items()}
            if cpf.get("uniqueid"):
                deps.append(Dependency(cpf["uniqueid"], True, cpf.get("minimumversion")))
            cpf = cpf.get("uniqueid")
        return cls(
            path=mod_dir,
            unique_id=str(data.get("uniqueid") or mod_dir.name),
            name=str(data.get("name") or mod_dir.name),
            version=str(data.get("version") or ""),
            author=str(data.get("author") or ""),
            content_pack_for=cpf,
            dependencies=deps,
        )


def find_mod_dirs(root: Path, include_hidden: bool = False) -> list[Path]:
    """Find mod folders the way SMAPI does: a folder with a manifest.json is a mod and is not
    searched further; any other folder is searched recursively. Folders starting with '.' are
    ignored by SMAPI (that's how mods are disabled)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        manifest_name = next((f for f in filenames if f.lower() == "manifest.json"), None)
        if manifest_name:
            if manifest_name != "manifest.json":
                # Keep things working on case-sensitive volumes.
                (current / manifest_name).rename(current / "manifest.json")
            found.append(current)
            dirnames[:] = []
        else:
            dirnames[:] = sorted(d for d in dirnames if include_hidden or not d.startswith("."))
    return found


def version_at_least(actual: str, minimum: str) -> bool:
    nums = lambda v: [int(x) for x in re.findall(r"\d+", v.split("-")[0])[:3]] + [0] * 3
    return nums(actual)[:3] >= nums(minimum)[:3]
