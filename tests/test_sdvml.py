import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

# Must be set before sdvml is imported: APP_DIR is resolved at import time.
_HOME = tempfile.mkdtemp(prefix="sdvml-test-")
os.environ["SDVML_HOME"] = _HOME

from sdvml import launcher  # noqa: E402
from sdvml.archives import extract  # noqa: E402
from sdvml.fomod import FomodInstaller, selection_from_vortex  # noqa: E402
from sdvml.importer import Importer, clean_group_name  # noqa: E402
from sdvml.manifest import Manifest, find_mod_dirs  # noqa: E402
from sdvml.profile import Profile  # noqa: E402

FOMOD_XML = """<?xml version="1.0" encoding="utf-16"?>
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://qconsulting.ca/fo3/ModConfig5.0.xsd">
  <moduleName>Pretty Portraits</moduleName>
  <installSteps order="Explicit">
    <installStep name="Style">
      <optionalFileGroups order="Explicit">
        <group name="Portrait style" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="Vanilla">
              <description>Vanilla-ish</description>
              <files><folder source="Options\\Vanilla" destination="[CP] Pretty Portraits" priority="0"/></files>
              <conditionFlags><flag name="style">vanilla</flag></conditionFlags>
              <typeDescriptor><type name="Recommended"/></typeDescriptor>
            </plugin>
            <plugin name="Anime">
              <description>Anime style</description>
              <files><folder source="OPTIONS\\anime" destination="[CP] Pretty Portraits" priority="0"/></files>
              <conditionFlags><flag name="style">anime</flag></conditionFlags>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls>
    <patterns>
      <pattern>
        <dependencies operator="And"><flagDependency flag="style" value="anime"/></dependencies>
        <files><file source="Extras\\anime-extra.json" destination="[CP] Pretty Portraits\\assets\\extra.json"/></files>
      </pattern>
    </patterns>
  </conditionalFileInstalls>
</config>
"""


def write_manifest(folder: Path, uid: str, name: str = None, version="1.0.0", deps=(), cpf=None, comment=True):
    folder.mkdir(parents=True, exist_ok=True)
    body = {"Name": name or uid, "Author": "Tester", "Version": version, "UniqueID": uid,
            "Dependencies": [{"UniqueID": d, "IsRequired": True} for d in deps]}
    if cpf:
        body["ContentPackFor"] = {"UniqueID": cpf}
    text = json.dumps(body, indent=2)
    if comment:
        # SMAPI manifests often have comments and trailing commas.
        text = "// made on Windows\n" + text.replace('"Tester"', '"Tester" /* inline */').rstrip("}") + ",}"
    (folder / "manifest.json").write_text(text, encoding="utf-8-sig")


def make_fomod_mod(root: Path) -> None:
    for style in ("Vanilla", "anime"):
        opt = root / "Options" / style
        write_manifest(opt, "tester.PrettyPortraits", "Pretty Portraits", cpf="Pathoschild.ContentPatcher")
        (opt / "style.txt").write_text(style)
    (root / "Extras").mkdir(parents=True)
    (root / "Extras" / "anime-extra.json").write_text("{}")
    (root / "fomod").mkdir()
    (root / "fomod" / "ModuleConfig.xml").write_bytes(FOMOD_XML.encode("utf-16"))


def zip_dir(src: Path, dest: Path, windows_separators=False) -> Path:
    with zipfile.ZipFile(dest, "w") as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                name = str(p.relative_to(src))
                zf.write(p, name.replace("/", "\\") if windows_separators else name)
    return dest


def seven_zip_dir(src: Path, dest: Path) -> Path:
    subprocess.run(["tar", "--format", "7zip", "-cf", str(dest), "-C", str(src), "."], check=True)
    return dest


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=_HOME))
        self.profile = Profile(self.id().rsplit(".", 1)[-1])
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.profile.delete()

    def importer(self, downloads=(), **kw):
        return Importer(self.profile, list(downloads), None, log=self.logs.append, **kw)


class ManifestTests(Base):
    def test_lenient_manifest(self):
        write_manifest(self.tmp / "A", "a.mod", deps=["b.mod"], cpf="cp")
        m = Manifest.load(self.tmp / "A")
        self.assertEqual(m.unique_id, "a.mod")
        self.assertEqual([d.unique_id for d in m.dependencies], ["b.mod", "cp"])

    def test_find_mod_dirs_skips_hidden_and_nested(self):
        write_manifest(self.tmp / "Group" / "A", "a")
        write_manifest(self.tmp / "Group" / "A" / "inner", "inner")
        write_manifest(self.tmp / ".Disabled", "d")
        self.assertEqual(find_mod_dirs(self.tmp), [self.tmp / "Group" / "A"])


class ArchiveTests(Base):
    def test_zip_with_windows_separators(self):
        write_manifest(self.tmp / "src" / "My Mod", "my.mod")
        (self.tmp / "src" / "My Mod" / "assets").mkdir()
        (self.tmp / "src" / "My Mod" / "assets" / "a.png").write_bytes(b"png")
        archive = zip_dir(self.tmp / "src", self.tmp / "mod.zip", windows_separators=True)
        out = extract(archive, self.tmp / "out")
        self.assertTrue((out / "My Mod" / "assets" / "a.png").is_file())
        self.assertTrue((out / "My Mod" / "manifest.json").is_file())

    def test_7z(self):
        write_manifest(self.tmp / "src" / "Mod", "m")
        (self.tmp / "src" / "Mod" / "Thumbs.db").write_bytes(b"x")
        out = extract(seven_zip_dir(self.tmp / "src", self.tmp / "m.7z"), self.tmp / "out")
        self.assertTrue((out / "Mod" / "manifest.json").exists())
        self.assertFalse((out / "Mod" / "Thumbs.db").exists())


class FomodTests(Base):
    def test_defaults_and_recorded_choices(self):
        make_fomod_mod(self.tmp / "mod")
        inst = FomodInstaller(self.tmp / "mod")
        self.assertEqual(inst.module_name, "Pretty Portraits")

        inst.install(inst.resolve(), self.tmp / "default")
        self.assertEqual((self.tmp / "default/[CP] Pretty Portraits/style.txt").read_text(), "Vanilla")
        self.assertFalse((self.tmp / "default/[CP] Pretty Portraits/assets/extra.json").exists())

        choices = {"type": "fomod", "options": [
            {"name": "Style", "groups": [{"name": "Portrait style", "choices": [{"name": "Anime", "idx": 1}]}]}]}
        inst.install(inst.resolve(selection_from_vortex(choices)), self.tmp / "anime")
        self.assertEqual((self.tmp / "anime/[CP] Pretty Portraits/style.txt").read_text(), "anime")
        self.assertTrue((self.tmp / "anime/[CP] Pretty Portraits/assets/extra.json").exists())

    def test_interactive(self):
        make_fomod_mod(self.tmp / "mod")
        inst = FomodInstaller(self.tmp / "mod")
        group, step = inst.steps[0].groups[0], inst.steps[0]
        with contextlib.redirect_stdout(io.StringIO()):
            picked = inst._prompt(step, group, {}, ask=lambda _: "2")
        self.assertEqual([p.name for p in picked], ["Anime"])


class CollectionTests(Base):
    def build_collection(self):
        """A collection archive as Vortex/Nexus produce it, plus a downloads folder."""
        downloads = self.tmp / "downloads"
        downloads.mkdir()
        # Content Patcher, downloaded manually from Nexus (matched by md5).
        write_manifest(self.tmp / "cp" / "ContentPatcher", "Pathoschild.ContentPatcher", "Content Patcher", "2.0.0")
        cp = zip_dir(self.tmp / "cp", downloads / "renamed-by-user.zip", windows_separators=True)
        # A FOMOD mod, matched by Nexus's "<name>-<modId>-<version>-<timestamp>" naming.
        make_fomod_mod(self.tmp / "pp")
        pp = seven_zip_dir(self.tmp / "pp", downloads / "Pretty Portraits-9999-1-2-1700000000.7z")

        coll = self.tmp / "collection"
        write_manifest(coll / "bundled" / "My Tweaks" / "Tweaks", "author.Tweaks", "Tweaks",
                       deps=["Pathoschild.ContentPatcher"])
        mods = [
            {"name": "SMAPI - Stardew Modding API", "version": "4.0.0",
             "source": {"type": "nexus", "modId": 2400, "fileId": 1}},
            {"name": "Content Patcher", "version": "2.0.0",
             "source": {"type": "nexus", "modId": 1915, "fileId": 100,
                        "md5": hashlib.md5(cp.read_bytes()).hexdigest(), "fileSize": cp.stat().st_size}},
            {"name": "Pretty Portraits", "version": "1.2",
             "source": {"type": "nexus", "modId": 9999, "fileId": 200, "md5": "0" * 32},
             "choices": {"type": "fomod", "options": [{"name": "Style", "groups": [
                 {"name": "Portrait style", "choices": [{"name": "Anime", "idx": 1}]}]}]}},
            {"name": "My Tweaks", "version": "1.0", "source": {"type": "bundle", "fileExpression": "My Tweaks"}},
            {"name": "Not Downloaded", "version": "3.1", "optional": True,
             "source": {"type": "nexus", "modId": 4242, "fileId": 300, "logicalFilename": "Not Downloaded"}},
        ]
        (coll / "collection.json").write_text(json.dumps({
            "info": {"name": "Cozy Pack", "author": "someone", "installInstructions": "Have fun"},
            "mods": mods}))
        return seven_zip_dir(coll, self.tmp / "Cozy Pack.7z"), downloads

    def test_import_collection(self):
        archive, downloads = self.build_collection()
        result = self.importer([downloads]).run(str(archive))

        self.assertEqual(sorted(result.installed), ["Content Patcher", "My Tweaks", "Pretty Portraits"])
        self.assertEqual([m.name for m in result.missing], ["Not Downloaded"])
        self.assertIn("nexusmods.com/stardewvalley/mods/4242", result.report.read_text())
        self.assertTrue(any("SMAPI" in s for s in result.skipped))

        mods = self.profile.mods_dir
        self.assertTrue((mods / "Content Patcher/ContentPatcher/manifest.json").exists())
        self.assertEqual((mods / "Pretty Portraits/[CP] Pretty Portraits/style.txt").read_text(), "anime")
        self.assertTrue((mods / "My Tweaks/Tweaks/manifest.json").exists())
        self.assertEqual(self.profile.check(), [])

        # Re-running resumes: installed mods are skipped.
        again = self.importer([downloads]).run(str(archive))
        self.assertEqual(again.installed, [])
        self.assertEqual(sum("already installed" in s for s in again.skipped), 3)
        self.assertIn("Have fun", result.collection.install_instructions)


class LooseImportTests(Base):
    def test_vortex_staging_folder(self):
        staging = self.tmp / "stardewvalley" / "mods"
        cp = staging / "Content Patcher-1915-2-0-6-1700000000"
        write_manifest(cp / "ContentPatcher", "Pathoschild.ContentPatcher", "Content Patcher")
        (cp / "__folder_managed_by_vortex").write_text("")
        write_manifest(staging / "Lookup Anything-541-1-40-1700000001" / "LookupAnything", "Pathoschild.LookupAnything")
        write_manifest(staging / "Some Pack-77-1-0-1700000002" / "[CP] Some Pack", "x.pack",
                       cpf="Pathoschild.ContentPatcher")
        (staging / "vortex.deployment.json").write_text("{}")
        archive = zip_dir(self.tmp / "stardewvalley", self.tmp / "my vortex mods.zip", windows_separators=True)

        result = self.importer().run(str(archive))
        self.assertEqual(sorted(result.installed), ["Content Patcher", "Lookup Anything", "Some Pack"])
        # Importing the unzipped folder directly works the same way.
        Profile(self.profile.name).delete()
        result = self.importer().run(str(self.tmp / "stardewvalley"))
        self.assertEqual(sorted(result.installed), ["Content Patcher", "Lookup Anything", "Some Pack"])
        self.assertEqual(self.profile.load_state()["groups"]["Content Patcher"]["mod_id"], 1915)
        self.assertFalse((self.profile.mods_dir / "Content Patcher" / "__folder_managed_by_vortex").exists())

    def test_single_staging_mod_keeps_vortex_name(self):
        write_manifest(self.tmp / "src" / "mods" / "Lookup Anything-541-1-40-1700000001" / "LookupAnything",
                       "Pathoschild.LookupAnything")
        result = self.importer().run(str(zip_dir(self.tmp / "src", self.tmp / "pack.zip")))
        self.assertEqual(result.installed, ["Lookup Anything"])

    def test_wrapper_folder_and_fomod_archive(self):
        write_manifest(self.tmp / "src" / "My Pack" / "ModA", "a")
        write_manifest(self.tmp / "src" / "My Pack" / "ModB" / "inner", "b")
        result = self.importer().run(str(zip_dir(self.tmp / "src", self.tmp / "pack.zip")))
        self.assertEqual(sorted(result.installed), ["ModA", "ModB"])

        make_fomod_mod(self.tmp / "fm" / "Pretty Portraits")
        result = self.importer().run(str(zip_dir(self.tmp / "fm", self.tmp / "Pretty Portraits-9999-1-2-1700000000.zip")))
        self.assertEqual(result.installed, ["Pretty Portraits"])
        self.assertEqual((self.profile.mods_dir / "Pretty Portraits/[CP] Pretty Portraits/style.txt").read_text(),
                         "Vanilla")

    def test_folder_of_archives_and_single_mod(self):
        dl = self.tmp / "Vortex Downloads"
        write_manifest(self.tmp / "a" / "ModA", "a")
        zip_dir(self.tmp / "a", (dl / "stardewvalley").mkdir(parents=True) or dl / "stardewvalley" / "ModA-1-1-0-1700000000.zip")
        write_manifest(self.tmp / "b", "b", "Mod B")  # manifest at archive root
        zip_dir(self.tmp / "b", dl / "stardewvalley" / "ModB.zip")
        result = self.importer().run(str(dl))
        self.assertEqual(sorted(result.installed), ["ModA", "ModB"])
        self.assertTrue((self.profile.mods_dir / "ModB" / "ModB" / "manifest.json").exists())


class ProfileTests(Base):
    def test_replace_duplicate_and_check(self):
        write_manifest(self.tmp / "old" / "LA", "Pathoschild.LookupAnything", version="1.0.0")
        write_manifest(self.tmp / "new" / "LA", "pathoschild.lookupanything", version="2.0.0")
        write_manifest(self.tmp / "dep" / "D", "needs.cp", deps=["Pathoschild.ContentPatcher"])
        self.profile.install(self.tmp / "old", "Old Name")
        _, msgs = self.profile.install(self.tmp / "new", "Lookup Anything")
        self.assertTrue(any("replaced" in m for m in msgs))
        self.assertEqual([g.name for g in self.profile.groups()], ["Lookup Anything"])

        self.profile.install(self.tmp / "dep", "Needs CP")
        self.assertIn("requires Pathoschild.ContentPatcher", self.profile.check()[0])
        self.profile.set_enabled("needs cp", False)
        self.assertTrue((self.profile.mods_dir / ".Needs CP").is_dir())
        self.assertEqual(self.profile.check(), [])
        self.profile.set_enabled("needs.cp", True)  # by UniqueID
        self.assertTrue((self.profile.mods_dir / "Needs CP").is_dir())


class OverlayTests(Base):
    def test_patch_mod_applies_to_target_when_it_arrives(self):
        # A Vortex "patch": no manifest, files laid out as they'd deploy into the merged Mods folder.
        patch = self.tmp / "patch" / "Grandpa's Tools" / "[CP]Grandpa's Tools"
        patch.mkdir(parents=True)
        (patch / "content.json").write_text("patched")
        (self.tmp / "patch" / "readme.txt").write_text("loose root file is ignored")
        gmcm_patch = self.tmp / "patch2" / "GenericModConfigMenu" / "assets"
        gmcm_patch.mkdir(parents=True)
        (gmcm_patch / "config-button.png").write_bytes(b"earthy")

        manifests, msgs = self.profile.install(self.tmp / "patch", "Grandpa's Tools Patch (CP)")
        self.assertEqual(manifests, [])
        self.profile.install(self.tmp / "patch2", "GMCM compat")
        self.assertIn("waiting for Grandpa's Tools", self.profile.apply_overlays()[0])

        # Target mods arrive later (in a differently named group); the patch then lands in them.
        target = self.tmp / "gt" / "Grandpa's Tools" / "[CP]Grandpa's Tools"
        write_manifest(target, "grandpa.tools")
        (target / "content.json").write_text("original")
        self.profile.install(self.tmp / "gt", "Grandpa's Tools")
        write_manifest(self.tmp / "gmcm" / "GenericModConfigMenu", "spacechase0.GenericModConfigMenu")
        self.profile.install(self.tmp / "gmcm", "Generic Mod Config Menu (GMCM)")

        msgs = self.profile.apply_overlays()
        self.assertTrue(all("applied" in m for m in msgs), msgs)
        mods = self.profile.mods_dir
        self.assertEqual((mods / "Grandpa's Tools/[CP]Grandpa's Tools/content.json").read_text(), "patched")
        self.assertEqual((mods / "Generic Mod Config Menu (GMCM)/GenericModConfigMenu/assets/config-button.png")
                         .read_bytes(), b"earthy")
        self.assertFalse(any(p.name == "readme.txt" for p in mods.rglob("*")))
        self.assertTrue(self.profile.has("GMCM compat"))
        self.assertEqual(self.profile.check(), [])


class DeployTests(Base):
    def test_deploy_and_undeploy(self):
        game = self.tmp / "game"
        game.mkdir()
        (game / "Stardew Valley.dll").write_bytes(b"")
        write_manifest(game / "Mods" / "ConsoleCommands", "SMAPI.ConsoleCommands")
        write_manifest(game / "Mods" / "MyOldMod", "old")
        self.profile.create()

        launcher.deploy(game, self.profile)
        self.assertTrue((game / "Mods").is_symlink())
        self.assertTrue((game / "Mods" / "ConsoleCommands" / "manifest.json").exists())
        self.assertEqual(launcher.deployed_profile(game), self.profile.root.name)

        launcher.undeploy(game)
        self.assertFalse((game / "Mods").is_symlink())
        self.assertTrue((game / "Mods" / "MyOldMod").is_dir())

    def test_group_name_cleanup(self):
        self.assertEqual(clean_group_name("Content Patcher-1915-2-0-6-1700000000"), ("Content Patcher", 1915))
        self.assertEqual(clean_group_name("Plain Name"), ("Plain Name", None))


def tearDownModule():
    shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
