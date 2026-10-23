"""Archive extraction plus the clean-up needed to make Windows-made mod files behave on macOS."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")

# Files Windows, Vortex or macOS leave behind that should never end up in a Mods folder.
JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "__folder_managed_by_vortex"}
JUNK_DIRS = {"__macosx"}


class ArchiveError(Exception):
    pass


def is_archive(path: Path) -> bool:
    name = path.name.lower()
    return path.is_file() and name.endswith(ARCHIVE_SUFFIXES)


def archive_stem(path: Path) -> str:
    name = path.name
    for suffix in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _safe_relpath(name: str) -> PurePosixPath | None:
    """Turn an archive entry name (possibly with Windows separators) into a safe relative path."""
    name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or ":" in parts[0]:
        return None
    return PurePosixPath(*parts)


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            rel = _safe_relpath(info.filename)
            if rel is None:
                continue
            target = dest.joinpath(*rel.parts)
            if info.is_dir() or info.filename.endswith(("/", "\\")):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            # zipfile drops Unix permissions; keep executables (e.g. the SMAPI installer) runnable.
            mode = (info.external_attr >> 16) & 0o777
            if mode & 0o111:
                target.chmod(mode | 0o600)


def _extract_external(archive: Path, dest: Path) -> None:
    # macOS ships bsdtar (libarchive), which reads 7z and rar as well as zip/tar. It refuses
    # absolute paths and ".." entries by default. Fall back to 7-Zip/unar if they are installed.
    commands = [["tar", "-xf", str(archive), "-C", str(dest)]]
    for tool in ("7zz", "7z"):
        if shutil.which(tool):
            commands.append([tool, "x", "-y", f"-o{dest}", str(archive)])
    if shutil.which("unar"):
        commands.append(["unar", "-force-overwrite", "-no-directory", "-o", str(dest), str(archive)])
    errors = []
    for cmd in commands:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return
        errors.append(f"{cmd[0]}: {result.stderr.strip() or result.stdout.strip()}")
    raise ArchiveError(f"could not extract {archive.name}:\n  " + "\n  ".join(errors))


def extract(archive: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        try:
            _extract_zip(archive, dest)
        except (zipfile.BadZipFile, NotImplementedError):
            # e.g. unsupported compression method; let bsdtar/7-Zip try.
            _extract_external(archive, dest)
    else:
        _extract_external(archive, dest)
    normalize_tree(dest)
    return dest


def _fix_backslash_names(root: Path) -> None:
    """Archives created on Windows sometimes extract as files literally named 'a\\b\\c.json'."""
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if "\\" not in path.name or not path.exists():
            continue
        rel = _safe_relpath(path.name)
        if rel is None:
            continue
        target = path.parent.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir() and target.is_dir():
            merge_tree(path, target)
            shutil.rmtree(path)
        else:
            path.replace(target)


def normalize_tree(root: Path) -> None:
    _fix_backslash_names(root)
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        lower = path.name.lower()
        if path.is_dir() and lower in JUNK_DIRS:
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and (lower in JUNK_NAMES or lower.startswith("vortex.deployment")):
            path.unlink()
    clear_quarantine(root)


def clear_quarantine(path: Path) -> None:
    """Gatekeeper flags downloaded files; SMAPI can then fail to load mod DLLs."""
    if shutil.which("xattr"):
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(path)], capture_output=True)


def merge_tree(src: Path, dest: Path) -> None:
    """Copy src into dest, overwriting files that already exist."""
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            merge_tree(item, target)
        else:
            shutil.copy2(item, target)
