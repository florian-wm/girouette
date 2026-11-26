"""Accès SQLite et schéma."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app import config

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS groupes (
    uid TEXT PRIMARY KEY,
    abrev TEXT,
    libelle TEXT,
    actif INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS deputes (
    uid TEXT PRIMARY KEY,
    prenom TEXT,
    nom TEXT
);

CREATE TABLE IF NOT EXISTS scrutins (
    uid TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'AN',
    legislature TEXT,
    numero INTEGER,
    date TEXT,
    type_vote TEXT,             -- SPO, SPS, MOC
    nature TEXT,                -- ensemble, partie, motion, article, amendement, autre
    titre TEXT,
    dossier_ref TEXT,
    dossier_libelle TEXT,
    sort TEXT,                  -- adopté / rejeté
    pour INTEGER, contre INTEGER, abstentions INTEGER, votants INTEGER,
    amendement_uid TEXT,
    amendement_auteur TEXT,
    amendement_dispositif TEXT,
    amendement_expose TEXT,
    content_hash TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_scrutins_date ON scrutins(date);

CREATE TABLE IF NOT EXISTS votes_groupes (
    scrutin_uid TEXT NOT NULL,
    groupe_uid TEXT NOT NULL,
    membres INTEGER,
    position_majoritaire TEXT,
    pour INTEGER, contre INTEGER, abstentions INTEGER, non_votants INTEGER,
    PRIMARY KEY (scrutin_uid, groupe_uid)
);

CREATE TABLE IF NOT EXISTS votes_deputes (
    scrutin_uid TEXT NOT NULL,
    depute_uid TEXT NOT NULL,
    groupe_uid TEXT,
    position TEXT,              -- pour, contre, abstention, non_votant
    par_delegation INTEGER,
    PRIMARY KEY (scrutin_uid, depute_uid)
);
CREATE INDEX IF NOT EXISTS idx_votes_deputes_depute ON votes_deputes(depute_uid);

CREATE TABLE IF NOT EXISTS objectifs (
    slug TEXT PRIMARY KEY,
    libelle TEXT NOT NULL,
    description TEXT,
    ordre INTEGER
);

-- Une analyse LLM par scrutin (même si aucun objectif n'est concerné).
CREATE TABLE IF NOT EXISTS analyses (
    scrutin_uid TEXT PRIMARY KEY,
    statut TEXT NOT NULL,       -- ok, refus, erreur
    resume TEXT,
    modele TEXT,
    prompt_version TEXT,
    input_hash TEXT,
    erreur TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS classements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scrutin_uid TEXT NOT NULL,
    objectif_slug TEXT NOT NULL,
    polarite INTEGER NOT NULL,  -- +1 : voter pour fait avancer l'objectif affiché ; -1 : le fait reculer
    confiance REAL NOT NULL,
    justification TEXT,
    modele TEXT,
    prompt_version TEXT,
    statut TEXT NOT NULL DEFAULT 'auto',  -- auto, valide, rejete
    valide_par TEXT,
    valide_at TEXT,
    created_at TEXT,
    UNIQUE (scrutin_uid, objectif_slug)
);
CREATE INDEX IF NOT EXISTS idx_classements_objectif ON classements(objectif_slug);

CREATE TABLE IF NOT EXISTS historique_classements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scrutin_uid TEXT NOT NULL,
    objectif_slug TEXT NOT NULL,
    action TEXT NOT NULL,       -- creation, reclassement, validation, rejet, modification
    avant TEXT,                 -- JSON
    apres TEXT,                 -- JSON
    auteur TEXT,
    at TEXT
);

CREATE TABLE IF NOT EXISTS signalements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scrutin_uid TEXT NOT NULL,
    objectif_slug TEXT,
    message TEXT NOT NULL,
    created_at TEXT,
    traite INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    etape TEXT NOT NULL,
    debut TEXT, fin TEXT,
    statut TEXT,
    details TEXT
);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
    from app.referentiel import sync_objectifs

    sync_objectifs()
