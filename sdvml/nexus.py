"""Minimal Nexus Mods API client (v1 REST + v2 GraphQL for collections)."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from . import __version__

API = "https://api.nexusmods.com"
GAME = "stardewvalley"


class NexusError(Exception):
    pass


def mod_page_url(mod_id: int, file_id: Optional[int] = None, game: str = GAME) -> str:
    url = f"https://www.nexusmods.com/{game}/mods/{mod_id}"
    return f"{url}?tab=files&file_id={file_id}" if file_id else f"{url}?tab=files"


def manager_download_url(mod_id: int, file_id: int, game: str = GAME) -> str:
    """The website's "Mod Manager Download" link; it hands the browser an nxm:// link."""
    return f"{mod_page_url(mod_id, file_id, game)}&nmm=1"


def parse_nxm(link: str) -> dict:
    """nxm://stardewvalley/mods/2400/files/12345?key=...&expires=...&user_id=..."""
    parsed = urllib.parse.urlparse(link)
    match = re.match(r"/mods/(\d+)/files/(\d+)", parsed.path)
    if parsed.scheme != "nxm" or not match:
        raise NexusError(f"not an nxm mod-file link: {link}")
    query = dict(urllib.parse.parse_qsl(parsed.query))
    return {"game": parsed.netloc, "mod_id": int(match.group(1)), "file_id": int(match.group(2)),
            "key": query.get("key"), "expires": query.get("expires")}


def parse_collection_url(url: str) -> Optional[tuple[str, Optional[int]]]:
    """Return (slug, revision) for a Nexus collection URL."""
    match = re.search(r"/collections/([a-z0-9]+)(?:/revisions/(\d+))?", url)
    if not match:
        return None
    return match.group(1), int(match.group(2)) if match.group(2) else None


class NexusClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("NEXUS_API_KEY")

    def _headers(self) -> dict:
        headers = {
            "User-Agent": f"sdvml/{__version__} (macOS)",
            "Application-Name": "sdvml",
            "Application-Version": __version__,
            "Accept": "application/json",
        }
        if self.api_key:
            headers["apikey"] = self.api_key
        return headers

    def _request(self, url: str, data: Optional[bytes] = None, extra: Optional[dict] = None, auth: bool = True):
        if auth and not self.api_key:
            raise NexusError("a Nexus API key is required (set NEXUS_API_KEY or pass --nexus-key; "
                             "get one at https://www.nexusmods.com/users/myaccount?tab=api)")
        headers = {**self._headers(), **(extra or {})}
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            raise NexusError(f"Nexus API {e.code} for {url}: {body}") from e
        except urllib.error.URLError as e:
            raise NexusError(f"network error for {url}: {e.reason}") from e

    def validate(self) -> dict:
        return self._request(f"{API}/v1/users/validate.json")

    def file_info(self, mod_id: int, file_id: int, game: str = GAME) -> dict:
        return self._request(f"{API}/v1/games/{game}/mods/{mod_id}/files/{file_id}.json")

    def download_link(self, mod_id: int, file_id: int, game: str = GAME,
                      key: Optional[str] = None, expires: Optional[str] = None) -> str:
        """Premium accounts can request links directly; free accounts need the key/expires
        pair from an nxm:// link (the "Mod Manager Download" button on the website)."""
        url = f"{API}/v1/games/{game}/mods/{mod_id}/files/{file_id}/download_link.json"
        if key and expires:
            url += "?" + urllib.parse.urlencode({"key": key, "expires": expires})
        links = self._request(url)
        if not links:
            raise NexusError(f"no download link returned for mod {mod_id} file {file_id}")
        return links[0]["URI"]

    def collection_download_link(self, slug: str, revision: Optional[int], game: str = GAME) -> tuple[str, int]:
        query = """query($slug: String!, $revision: Int, $domain: String) {
          collectionRevision(slug: $slug, revision: $revision, domainName: $domain, viewAdultContent: true) {
            revisionNumber downloadLink
          }
        }"""
        rev = self._graphql(query, {"slug": slug, "revision": revision, "domain": game}).get("collectionRevision")
        if not rev or not rev.get("downloadLink"):
            raise NexusError(f"collection {slug} not found")
        link = rev["downloadLink"]
        link = link if link.startswith("http") else API + link
        links = self._request(link)
        if isinstance(links, dict):
            links = links.get("download_links", [])
        if not links:
            raise NexusError("no collection download link returned")
        return links[0]["URI"], rev["revisionNumber"]

    def _graphql(self, query: str, variables: dict, auth: bool = True) -> dict:
        payload = json.dumps({"query": query, "variables": variables}).encode()
        result = self._request(f"{API}/v2/graphql", payload, {"Content-Type": "application/json"}, auth=auth)
        if result.get("errors"):
            raise NexusError("; ".join(e.get("message", "?") for e in result["errors"]))
        return result.get("data") or {}

    def collection_mod_list(self, slug: str, revision: Optional[int], game: str = GAME) -> dict:
        """The public part of a collection (no API key needed): its name and the exact mod files.
        It lacks what's only in the collection archive: FOMOD choices and bundled files."""
        query = """query($slug: String!, $revision: Int, $domain: String) {
          collectionRevision(slug: $slug, revision: $revision, domainName: $domain, viewAdultContent: true) {
            revisionNumber
            collection { name summary user { name } }
            modFiles { optional file { fileId uri version sizeInBytes mod { modId name } } }
          }
        }"""
        rev = self._graphql(query, {"slug": slug, "revision": revision, "domain": game}, auth=False) \
            .get("collectionRevision")
        if not rev:
            raise NexusError(f"collection {slug} not found")
        return rev


def download(url: str, dest_dir: Path, filename: Optional[str] = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Nexus CDN links contain raw spaces/apostrophes from the file name; escape them (but not
    # existing %-escapes or URL syntax).
    url = urllib.parse.quote(url, safe=":/?&=%#+@,;~!$*()[]")
    req = urllib.request.Request(url, headers={"User-Agent": f"sdvml/{__version__} (macOS)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        if not filename:
            disposition = resp.headers.get("Content-Disposition", "")
            match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition)
            filename = urllib.parse.unquote(match.group(1)) if match else \
                urllib.parse.unquote(Path(urllib.parse.urlparse(resp.geturl()).path).name) or "download"
        filename = filename.replace("/", "_").replace("\\", "_")
        target = dest_dir / filename
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        tmp = target.with_name(target.name + ".part")
        with open(tmp, "wb") as out:
            while chunk := resp.read(1 << 16):
                out.write(chunk)
                done += len(chunk)
                if total and sys.stderr.isatty():
                    sys.stderr.write(f"\r  {filename}: {done * 100 // total}%")
        if total and sys.stderr.isatty():
            sys.stderr.write("\n")
        tmp.replace(target)
    return target
