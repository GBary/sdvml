"""A FOMOD (ModuleConfig.xml) installer.

Selections come from, in order of preference: the choices a Vortex collection recorded, an
interactive prompt, or the installer's defaults (Required/Recommended options).
"""

from __future__ import annotations

import re
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from .archives import merge_tree


class FomodError(Exception):
    pass


@dataclass
class FileOp:
    source: str
    destination: str
    is_folder: bool
    priority: int = 0


@dataclass
class Plugin:
    name: str
    description: str
    files: list[FileOp]
    flags: dict[str, str]
    type_el: Optional[ET.Element]


@dataclass
class Group:
    name: str
    type: str
    plugins: list[Plugin]


@dataclass
class Step:
    name: str
    visible: Optional[ET.Element]
    groups: list[Group]


@dataclass
class Selection:
    """Selected plugin names keyed by (step name, group name)."""

    picks: dict[tuple[str, str], list[str]] = field(default_factory=dict)


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _parse_xml(path: Path) -> ET.Element:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
    # The declaration often lies about the encoding; we've already decoded it.
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        return _strip_ns(ET.fromstring(text))
    except ET.ParseError as e:
        raise FomodError(f"invalid ModuleConfig.xml: {e}") from e


def find_fomod_root(root: Path, max_depth: int = 3) -> Optional[Path]:
    """Return the folder containing fomod/ModuleConfig.xml (the module's file root)."""
    candidates = [root]
    for _ in range(max_depth):
        next_level = []
        for directory in candidates:
            for child in sorted(directory.iterdir()) if directory.is_dir() else []:
                if child.is_dir() and child.name.lower() == "fomod":
                    if any(f.name.lower() == "moduleconfig.xml" for f in child.iterdir()):
                        return directory
                elif child.is_dir():
                    next_level.append(child)
        candidates = next_level
    return None


def _file_ops(files_el: Optional[ET.Element]) -> list[FileOp]:
    ops = []
    if files_el is None:
        return ops
    for el in files_el:
        if el.tag not in ("file", "folder"):
            continue
        src = el.get("source", "")
        ops.append(FileOp(src, el.get("destination"), el.tag == "folder", int(el.get("priority") or 0)))
    return ops


class FomodInstaller:
    def __init__(self, module_root: Path):
        self.root = module_root
        fomod_dir = next(p for p in module_root.iterdir() if p.is_dir() and p.name.lower() == "fomod")
        config = next(p for p in fomod_dir.iterdir() if p.name.lower() == "moduleconfig.xml")
        self.xml = _parse_xml(config)
        self.module_name = (self.xml.findtext("moduleName") or module_root.name).strip()
        self.required_files = _file_ops(self.xml.find("requiredInstallFiles"))
        self.steps = [self._parse_step(s) for s in self.xml.findall("installSteps/installStep")]
        self.conditional = self.xml.findall("conditionalFileInstalls/patterns/pattern")

    def _parse_step(self, el: ET.Element) -> Step:
        groups = []
        for g in el.findall("optionalFileGroups/group"):
            plugins = []
            for p in g.findall("plugins/plugin"):
                flags = {f.get("name", ""): (f.text or "").strip() for f in p.findall("conditionFlags/flag")}
                plugins.append(Plugin(
                    name=p.get("name", ""),
                    description=(p.findtext("description") or "").strip(),
                    files=_file_ops(p.find("files")),
                    flags=flags,
                    type_el=p.find("typeDescriptor"),
                ))
            groups.append(Group(g.get("name", ""), g.get("type", "SelectAny"), plugins))
        return Step(el.get("name", ""), el.find("visible"), groups)

    # --- condition evaluation -------------------------------------------------------------

    def _eval_deps(self, el: Optional[ET.Element], flags: dict[str, str]) -> bool:
        if el is None:
            return True
        # <visible> may wrap a <dependencies> element or hold the conditions directly.
        if el.tag != "dependencies" and el.find("dependencies") is not None and len(el) == 1:
            el = el.find("dependencies")
        results = []
        for child in el:
            if child.tag == "flagDependency":
                results.append(flags.get(child.get("flag", ""), "") == (child.get("value") or ""))
            elif child.tag == "fileDependency":
                # We can't know what else is "active" the way a Windows mod manager can; assume
                # referenced files are absent.
                results.append((child.get("state") or "").lower() == "missing")
            elif child.tag == "dependencies":
                results.append(self._eval_deps(child, flags))
            else:  # gameDependency, fommDependency, foseDependency...
                results.append(True)
        if not results:
            return True
        return any(results) if (el.get("operator") or "And").lower() == "or" else all(results)

    def _plugin_type(self, plugin: Plugin, flags: dict[str, str]) -> str:
        el = plugin.type_el
        if el is None:
            return "Optional"
        simple = el.find("type")
        if simple is not None:
            return simple.get("name", "Optional")
        dep_type = el.find("dependencyType")
        if dep_type is None:
            return "Optional"
        for pattern in dep_type.findall("patterns/pattern"):
            if self._eval_deps(pattern.find("dependencies"), flags):
                t = pattern.find("type")
                if t is not None:
                    return t.get("name", "Optional")
        default = dep_type.find("defaultType")
        return default.get("name", "Optional") if default is not None else "Optional"

    # --- selection ------------------------------------------------------------------------

    def _default_pick(self, group: Group, flags: dict[str, str]) -> list[Plugin]:
        types = {p.name: self._plugin_type(p, flags) for p in group.plugins}
        usable = [p for p in group.plugins if types[p.name] != "NotUsable"]
        if group.type == "SelectAll":
            return group.plugins
        picked = [p for p in usable if types[p.name] in ("Required", "Recommended")]
        if group.type in ("SelectExactlyOne", "SelectAtMostOne"):
            picked = picked[:1]
        if not picked and group.type in ("SelectExactlyOne", "SelectAtLeastOne") and usable:
            picked = usable[:1]
        return picked

    def _prompt(self, step: Step, group: Group, flags: dict[str, str], ask: Callable[[str], str]) -> list[Plugin]:
        default = self._default_pick(group, flags)
        if group.type == "SelectAll" or len(group.plugins) <= 1 and group.type != "SelectAny":
            return default
        print(f"\n[{self.module_name}] {step.name} > {group.name} ({group.type})")
        for i, p in enumerate(group.plugins, 1):
            mark = "*" if p in default else " "
            desc = f" - {p.description[:100]}" if p.description else ""
            print(f"  {mark}{i}. {p.name} [{self._plugin_type(p, flags)}]{desc}")
        single = group.type in ("SelectExactlyOne", "SelectAtMostOne")
        hint = "number" if single else "numbers separated by spaces"
        while True:
            answer = ask(f"  Choose {hint} (Enter for *): ").strip()
            if not answer:
                return default
            try:
                idx = [int(x) - 1 for x in answer.replace(",", " ").split()]
                chosen = [group.plugins[i] for i in idx if 0 <= i < len(group.plugins)]
            except ValueError:
                chosen = []
            if chosen and (not single or len(chosen) == 1):
                return chosen
            print("  Invalid choice.")

    def resolve(self, selection: Optional[Selection] = None, interactive: bool = False,
                ask: Callable[[str], str] = input) -> list[FileOp]:
        """Walk the install steps and return the file operations to perform."""
        flags: dict[str, str] = {}
        ops = list(self.required_files)
        for step in self.steps:
            if not self._eval_deps(step.visible, flags):
                continue
            for group in step.groups:
                key = (step.name, group.name)
                if selection is not None and key in selection.picks:
                    names = selection.picks[key]
                    chosen = [p for p in group.plugins if p.name in names]
                elif interactive and sys.stdin.isatty():
                    chosen = self._prompt(step, group, flags, ask)
                else:
                    chosen = self._default_pick(group, flags)
                for plugin in chosen:
                    flags.update(plugin.flags)
                    ops.extend(plugin.files)
        for pattern in self.conditional:
            if self._eval_deps(pattern.find("dependencies"), flags):
                ops.extend(_file_ops(pattern.find("files")))
        return ops

    # --- installation ---------------------------------------------------------------------

    def _resolve_source(self, source: str) -> Optional[Path]:
        """Resolve a Windows-style, case-insensitive source path inside the module."""
        current = self.root
        for part in PurePosixPath(source.replace("\\", "/")).parts:
            if part in (".", ""):
                continue
            exact = current / part
            if exact.exists():
                current = exact
                continue
            match = next((c for c in current.iterdir() if c.name.lower() == part.lower()), None) if current.is_dir() else None
            if match is None:
                return None
            current = match
        return current

    def install(self, ops: list[FileOp], dest: Path) -> list[str]:
        """Copy the resolved files into dest. Returns warnings."""
        warnings = []
        dest.mkdir(parents=True, exist_ok=True)
        for op in sorted(ops, key=lambda o: o.priority):  # stable: later ops win ties
            src = self._resolve_source(op.source)
            if src is None:
                warnings.append(f"FOMOD source not found: {op.source}")
                continue
            if op.destination is None:
                rel = op.source
            elif op.destination == "" and not op.is_folder:
                rel = src.name
            elif op.destination.endswith(("/", "\\")) and not op.is_folder:
                rel = op.destination + src.name
            else:
                rel = op.destination
            parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
            if ".." in parts:
                warnings.append(f"FOMOD destination escapes mod folder: {rel}")
                continue
            target = dest.joinpath(*parts)
            if src.is_dir():
                merge_tree(src, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
        return warnings


def selection_from_vortex(choices: Optional[dict]) -> Optional[Selection]:
    """Convert a collection.json "choices" entry ({"type": "fomod", "options": [...]})."""
    if not choices or choices.get("type") != "fomod":
        return None
    sel = Selection()
    for step in choices.get("options") or []:
        for group in step.get("groups") or []:
            sel.picks[(step.get("name", ""), group.get("name", ""))] = [
                c.get("name", "") for c in group.get("choices") or []
            ]
    return sel
