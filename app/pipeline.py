"""Enchaînement ingestion -> classement, avec verrou et journal d'exécution."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from app import db

log = logging.getLogger(__name__)
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _etape(nom: str, fn, *args, **kwargs):
    with db.session() as conn:
        run_id = conn.execute(
            "INSERT INTO pipeline_runs (etape, debut, statut) VALUES (?, ?, 'en_cours')",
            (nom, _now()),
        ).lastrowid
    try:
        res = fn(*args, **kwargs)
        statut = "ok"
        return res
    except Exception as e:
        res, statut = {"erreur": repr(e)}, "erreur"
        raise
    finally:
        with db.session() as conn:
            conn.execute(
                "UPDATE pipeline_runs SET fin = ?, statut = ?, details = ? WHERE id = ?",
                (_now(), statut, json.dumps(res, ensure_ascii=False, default=str), run_id),
            )


def llm_disponible() -> bool:
    from app import llm

    try:
        return llm.cle_disponible()
    except llm.ErreurConfiguration:
        return False


def run(force_download: bool = False, classify_limit: int | None = None) -> dict:
    if not _lock.acquire(blocking=False):
        log.warning("Pipeline déjà en cours, exécution ignorée")
        return {"statut": "deja_en_cours"}
    try:
        from app.classify import classify
        from app.ingest.an import ingest

        out = {"ingestion": _etape("ingestion_an", ingest, force=force_download)}
        if llm_disponible():
            out["classement"] = _etape("classement", classify, limit=classify_limit)
        else:
            out["classement"] = "ignoré : aucune clé API pour le fournisseur LLM configuré"
        log.info("Pipeline terminé : %s", out)
        return out
    finally:
        _lock.release()


def en_cours() -> bool:
    return _lock.locked()
