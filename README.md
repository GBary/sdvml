# sdvml: Stardew Valley mod loader for macOS

Vortex only runs on Windows. `sdvml` takes the mod packs people build with Vortex and installs
them into [SMAPI](https://smapi.io) on a Mac.

SMAPI mods are cross-platform .NET, so the mods themselves already work on macOS. The part
that doesn't carry over is Vortex's packaging. sdvml handles that:

| Windows/Vortex thing | What sdvml does |
| --- | --- |
| Collections (`collection.json` in a `.7z`) | Reads the mod list and installs bundled mods. Matches each Nexus mod to an archive you downloaded (by MD5, by Nexus file name, or by logical name), and writes a checklist with links for anything still missing |
| FOMOD installers (`fomod/ModuleConfig.xml`) | Replays the options the collection author picked, falls back to the installer's defaults, or asks you (`-i`) |
| Vortex staging folder (`%APPDATA%\Vortex\stardewvalley\mods`) or downloads folder | Imports each mod and recovers its name and Nexus ID from Vortex's folder naming |
| Zips with `\` path separators | Extracts them into real folders instead of files named `a\b\c.json` |
| `.7z` / `.rar` archives | Uses macOS's built-in `bsdtar` (or 7-Zip/unar if installed) |
| Gatekeeper quarantine on downloaded DLLs | Clears it so SMAPI can load the mods |
| Vortex markers, `Thumbs.db`, `desktop.ini`, `__MACOSX` | Removes them |
| Patch mods with no `manifest.json` (Vortex merges every mod into one Mods folder) | Stores them as patches and copies their files into the mods they target, re-applying after each import |
| Two copies of the same mod | Replaces the older copy, matching UniqueID case-insensitively, since SMAPI refuses duplicates |

It uses only the standard library and runs on the `python3` that ships with macOS (3.9+).

## Setup

```sh
git clone <this repo> && cd Stardew
ln -s "$PWD/bin/sdvml" /usr/local/bin/sdvml   # or: pip3 install --user .
sdvml doctor           # finds Steam/GOG/App Store installs automatically
sdvml install-smapi    # if SMAPI isn't installed yet (runs the official installer)
```

If the game isn't found, run `sdvml set-game "/path/to/Stardew Valley.app"`.

## Installing a Vortex collection

1. Get the collection archive: download it from Nexus, or have a Windows user share the `.7z`.
   With a Nexus API key you can pass the collection URL directly.
2. Import it:

   ```sh
   sdvml import ~/Downloads/my-collection.7z
   ```

3. Any mod that isn't bundled and isn't already in `~/Downloads` gets listed in
   `missing-mods.md`, with direct links. Download those files with **Manual download**,
   then run the same import again. Mods that are already installed are skipped.
4. Run `sdvml launch`.

**Nexus Premium:** run `sdvml set-key <api key>`, and every mod downloads automatically.
**Free accounts:** click **Mod Manager Download** on Nexus, copy the `nxm://` link, and run
`sdvml nxm '<link>'`.

## Other kinds of packs

```sh
sdvml import "Vortex mods.zip"        # zipped staging or downloads folder from a Windows PC
sdvml import ~/Desktop/Mods           # a copied Windows "Stardew Valley/Mods" folder
sdvml import SomeMod-1234-1-0-0.zip   # a single mod, FOMOD or not
```

## Managing mods

```sh
sdvml list                      # groups and the SMAPI mods inside them
sdvml disable "Some Mod"        # by name, partial name, or UniqueID
sdvml enable  Pathoschild.LookupAnything
sdvml remove  "Some Mod"
sdvml check                     # missing dependencies, version mismatches, duplicates
```

**Profiles** keep separate mod setups, for example one per save or collection:

```sh
sdvml use expanded              # switch (creates it if new)
sdvml -p cozy import cozy.7z    # act on a profile without switching
sdvml profiles
```

**Launching:**

- `sdvml launch` starts SMAPI with `--mods-path` pointing at the active profile. Your game
  folder isn't touched.
- `sdvml deploy` symlinks the game's `Mods` folder to the profile, so launching from Steam
  uses it too. Your original `Mods` folder is moved to `Mods.sdvml-backup`, and
  `sdvml undeploy` puts it back.

Data lives in `~/Library/Application Support/SDVModLoader` (override with `SDVML_HOME`).
Each profile is a plain SMAPI Mods folder. Disabled mods start with `.`, which SMAPI skips.

## Limitations

- Nexus free accounts can't auto-download (that's Nexus's rule), so use manual downloads
  or `sdvml nxm`.
- XNB/content-replacement mods aren't supported. They're obsolete since Content Patcher.
- Collection load-order rules aren't read. SMAPI orders mods by their declared dependencies,
  and patches are applied after the mods they patch, in collection order.
- The rare mod that ships Windows-only native DLLs won't work on macOS, whatever the loader.

## Development

```sh
python3 -m unittest discover -s tests -v
```
