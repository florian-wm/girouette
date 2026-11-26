"""Référentiels maintenus à la main : objectifs et correspondance groupes/partis."""

from __future__ import annotations

from functools import lru_cache

import yaml

from app import config, db


@lru_cache
def objectifs() -> list[dict]:
    with open(config.CONFIG_DIR / "objectifs.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["objectifs"]


@lru_cache
def groupes_partis() -> dict[str, dict]:
    with open(config.CONFIG_DIR / "groupes.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["groupes"]


def sync_objectifs() -> None:
    with db.session() as conn:
        for i, o in enumerate(objectifs()):
            conn.execute(
                """INSERT INTO objectifs (slug, libelle, description, ordre) VALUES (?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET libelle=excluded.libelle,
                   description=excluded.description, ordre=excluded.ordre""",
                (o["slug"], o["libelle"], o["description"].strip(), i),
            )
