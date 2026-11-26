"""Configuration lue depuis l'environnement (voir .env.example)."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", BASE_DIR / "config"))
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "votes.db"))

LEGISLATURE = os.environ.get("AN_LEGISLATURE", "17")
AN_BASE = "https://data.assemblee-nationale.fr/static/openData/repository"
AN_URLS = {
    "scrutins": f"{AN_BASE}/{LEGISLATURE}/loi/scrutins/Scrutins.json.zip",
    # Référentiel historique : inclut les groupes dissous/recréés et les députés sortants.
    "amo": f"{AN_BASE}/{LEGISLATURE}/amo/tous_acteurs_mandats_organes_xi_legislature/"
    f"AMO30_tous_acteurs_tous_mandats_tous_organes_historique.json.zip",
    "amendements": f"{AN_BASE}/{LEGISLATURE}/loi/amendements_div_legis/Amendements.json.zip",
}
# Le téléchargement des amendements (~370 Mo) peut être désactivé.
INGEST_AMENDEMENTS = os.environ.get("INGEST_AMENDEMENTS", "1") == "1"

# --- Classement LLM ---------------------------------------------------------
# Fournisseur : anthropic (Claude), mistral ou gemini. Voir app/llm.py.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
# Vide = modèle par défaut du fournisseur (llm.MODELES_PAR_DEFAUT).
LLM_MODEL = os.environ.get("LLM_MODEL", "").strip()
# Niveau d'effort (Claude uniquement).
LLM_EFFORT = os.environ.get("LLM_EFFORT", "medium")
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "4"))
# Nombre maximum de scrutins classés par exécution du pipeline (garde-fou de coût).
CLASSIFY_LIMIT = int(os.environ.get("CLASSIFY_LIMIT", "300"))
# Natures de scrutins envoyées au LLM, par ordre de priorité.
CLASSIFY_NATURES = [
    n.strip()
    for n in os.environ.get(
        "CLASSIFY_NATURES", "ensemble,partie,motion,article,amendement,autre"
    ).split(",")
    if n.strip()
]

# --- Planification ------------------------------------------------------------
# Format cron "minute heure jour mois jour_semaine" (vide = désactivé).
PIPELINE_CRON = os.environ.get("PIPELINE_CRON", "30 6 * * *")
TIMEZONE = os.environ.get("TZ", "Europe/Paris")

# --- Back-office ----------------------------------------------------------------
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# --- Agrégation -----------------------------------------------------------------
# Poids d'un scrutin selon sa nature (vote sur l'ensemble > partie > article > amendement).
POIDS_NATURE = {
    "ensemble": 4.0,
    "motion": 3.0,
    "partie": 2.0,
    "article": 1.5,
    "amendement": 1.0,
    "autre": 1.0,
}
