"""Téléchargement conditionnel (ETag / Last-Modified) des archives open data."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


def download(url: str, dest: Path, force: bool = False, tentatives: int = 4) -> bool:
    """Télécharge `url` vers `dest` si le fichier distant a changé (avec reprises).

    Retourne True si un nouveau fichier a été écrit.
    """
    for i in range(1, tentatives + 1):
        try:
            return _download(url, dest, force)
        except httpx.TransportError as e:
            if i == tentatives:
                raise
            log.warning("Téléchargement interrompu (%s), nouvelle tentative %d/%d", e, i + 1, tentatives)
            time.sleep(5 * i)
    return False


def _download(url: str, dest: Path, force: bool) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta_path = dest.with_suffix(dest.suffix + ".meta.json")
    headers = {"User-Agent": "votes-intentions-politiques/0.1"}
    if dest.exists() and meta_path.exists() and not force:
        meta = json.loads(meta_path.read_text())
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]

    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, headers=headers, timeout=120, follow_redirects=True) as r:
        if r.status_code == 304:
            log.info("Inchangé : %s", url)
            return False
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
        meta = {
            "url": url,
            "etag": r.headers.get("etag"),
            "last_modified": r.headers.get("last-modified"),
        }
    tmp.replace(dest)
    meta_path.write_text(json.dumps(meta))
    log.info("Téléchargé : %s (%.1f Mo)", url, dest.stat().st_size / 1e6)
    return True
