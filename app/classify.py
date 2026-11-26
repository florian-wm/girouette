"""Classement thématique + polarité des scrutins par LLM (Claude, Mistral ou Gemini : voir app/llm.py).

Principe de neutralité (README §5) : on ne juge pas les *effets* d'une mesure, on mesure la
cohérence entre le vote et l'objectif **tel que le texte lui-même l'affiche**. Le prompt
ci-dessous est publié tel quel sur la page « Méthode ».
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from app import config, db, llm, referentiel
from app.llm import ErreurConfiguration, Refus

log = logging.getLogger(__name__)

PROMPT_VERSION = "2026-11-26.2"
MAX_CHARS_AMENDEMENT = 8000

SYSTEM_PROMPT_TEMPLATE = """\
Tu aides un site citoyen non partisan à relier les votes de l'Assemblée nationale à des \
objectifs politiques largement partagés. Pour chaque scrutin, tu indiques quels objectifs \
il concerne et dans quel sens joue un vote « pour ».

## Objectifs suivis

{objectifs}

## Règles

1. Rattachement. Un scrutin concerne un objectif seulement si son objet principal, ou une \
mesure qu'il contient, porte directement sur cet objectif. Les liens indirects ou \
spéculatifs ne comptent pas. Beaucoup de scrutins ne concernent aucun objectif : renvoie \
alors une liste vide.

2. Polarité : tu ne juges pas si la mesure est efficace ou souhaitable. Tu te fondes sur \
ce que le texte ou l'amendement **affiche lui-même** (intitulé, dispositif, exposé des \
motifs).
   - "pour_fait_avancer" : adopter ce qui est mis aux voix va, selon son propre objet \
affiché, dans le sens de l'objectif (ex. un amendement qui « vise à revaloriser le RSA » \
pour l'objectif pauvreté).
   - "pour_fait_reculer" : adopter ce qui est mis aux voix supprime, réduit ou retarde une \
mesure affichée comme servant l'objectif (ex. un amendement de suppression d'un article \
qui crée des postes de soignants, pour l'objectif hôpital).
   - Motions de rejet ou de censure : voter « pour » rejette le texte ou renverse le \
gouvernement. La polarité est celle d'un vote pour le rejet, donc l'inverse de celle du \
texte visé.
   - Si l'objet affiché est ambigu ou mêle des effets opposés sur le même objectif, \
n'inclus pas cet objectif, ou mets une confiance basse.

3. Confiance (0 à 1) : 0,9 et plus si le lien et le sens sont explicites dans le texte ; \
autour de 0,5 si tu dois déduire ; en dessous de 0,4, n'inclus pas l'objectif. Si seul le \
titre du scrutin est disponible (sans dispositif ni exposé), la confiance ne dépasse pas \
0,6, sauf si le titre est sans ambiguïté.

4. Justification : une ou deux phrases factuelles en français, compréhensibles par le \
grand public, qui citent ce que le texte affiche. Pas de jugement de valeur, pas \
d'adjectif sur les partis.

5. Résumé : une phrase neutre en français qui dit ce qui était mis aux voix, sans \
jargon parlementaire.

6. Sources : l'intitulé du scrutin fait foi. Le libellé du dossier législatif provient \
d'un rattachement automatique parfois erroné ; s'il contredit l'intitulé, ignore-le.
"""


def system_prompt() -> str:
    lignes = [
        f"- `{o['slug']}` : {o['libelle']}. Périmètre : {o['description'].strip()}"
        for o in referentiel.objectifs()
    ]
    return SYSTEM_PROMPT_TEMPLATE.format(objectifs="\n".join(lignes))


def output_schema() -> dict:
    slugs = [o["slug"] for o in referentiel.objectifs()]
    return {
        "type": "object",
        "properties": {
            "resume": {"type": "string"},
            "objectifs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "objectif": {"type": "string", "enum": slugs},
                        "polarite": {
                            "type": "string",
                            "enum": ["pour_fait_avancer", "pour_fait_reculer"],
                        },
                        "confiance": {"type": "number"},
                        "justification": {"type": "string"},
                    },
                    "required": ["objectif", "polarite", "confiance", "justification"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["resume", "objectifs"],
        "additionalProperties": False,
    }


def _tronque(texte: str | None, limite: int) -> str | None:
    if not texte:
        return None
    if len(texte) <= limite:
        return texte
    return texte[:limite] + f"\n[… texte tronqué : {len(texte) - limite} caractères non transmis]"


def user_message(s: dict) -> str:
    parts = [
        f"Scrutin n° {s['numero']} du {s['date']} — nature : {s['nature']}",
        f"Intitulé : {s['titre']}",
    ]
    if s.get("dossier_libelle"):
        parts.append(f"Dossier législatif : {s['dossier_libelle']}")
    parts.append(f"Résultat : {s.get('sort') or 'inconnu'}")
    if s["nature"] == "amendement":
        if s.get("amendement_dispositif") or s.get("amendement_expose"):
            parts.append(f"Auteurs : {s.get('amendement_auteur') or 'inconnus'}")
            disp = _tronque(s.get("amendement_dispositif"), MAX_CHARS_AMENDEMENT // 2)
            expo = _tronque(s.get("amendement_expose"), MAX_CHARS_AMENDEMENT)
            parts.append(f"Dispositif de l'amendement :\n{disp or '(non disponible)'}")
            parts.append(f"Exposé sommaire :\n{expo or '(non disponible)'}")
        else:
            parts.append("(Texte de l'amendement non disponible : seul le titre est connu.)")
    return "\n\n".join(parts)


def input_hash(s: dict) -> str:
    return hashlib.sha256(
        # Le prompt système contient la taxonomie : la modifier déclenche un reclassement.
        (PROMPT_VERSION + llm.identifiant() + system_prompt() + user_message(s)).encode()
    ).hexdigest()[:16]


# --------------------------------------------------------------------- appel LLM


def appeler_llm(s: dict) -> dict:
    """Appelle le fournisseur configuré et renvoie {resume, objectifs: [...], _modele}."""
    data, modele = llm.fournisseur().classer(system_prompt(), user_message(s), output_schema())
    data["_modele"] = f"{llm.nom_fournisseur()}:{modele}"
    return data


# ------------------------------------------------------------------- persistance


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enregistrer(scrutin_uid: str, result: dict, h: str) -> None:
    """Remplace les classements automatiques ; ne touche jamais aux décisions humaines."""
    modele = result.get("_modele", llm.identifiant())
    now = _now()
    with db.session() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO analyses (scrutin_uid, statut, resume, modele, prompt_version,
               input_hash, erreur, created_at) VALUES (?, 'ok', ?, ?, ?, ?, NULL, ?)""",
            (scrutin_uid, result.get("resume"), modele, PROMPT_VERSION, h, now),
        )
        existants = {
            r["objectif_slug"]: dict(r)
            for r in conn.execute("SELECT * FROM classements WHERE scrutin_uid = ?", (scrutin_uid,))
        }
        nouveaux = {}
        for o in result.get("objectifs", []):
            conf = max(0.0, min(1.0, float(o["confiance"])))
            if conf < 0.4:
                continue
            nouveaux[o["objectif"]] = {
                "polarite": 1 if o["polarite"] == "pour_fait_avancer" else -1,
                "confiance": round(conf, 2),
                "justification": o["justification"].strip(),
            }

        for slug, ex in existants.items():
            if ex["statut"] != "auto":
                continue  # validé ou rejeté par un humain : on garde
            if slug not in nouveaux:
                conn.execute("DELETE FROM classements WHERE id = ?", (ex["id"],))
                _historique(conn, scrutin_uid, slug, "reclassement", ex, None, modele)

        for slug, n in nouveaux.items():
            ex = existants.get(slug)
            if ex and ex["statut"] != "auto":
                continue
            conn.execute(
                """INSERT INTO classements (scrutin_uid, objectif_slug, polarite, confiance,
                   justification, modele, prompt_version, statut, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'auto', ?)
                   ON CONFLICT(scrutin_uid, objectif_slug) DO UPDATE SET polarite=excluded.polarite,
                   confiance=excluded.confiance, justification=excluded.justification,
                   modele=excluded.modele, prompt_version=excluded.prompt_version,
                   created_at=excluded.created_at""",
                (scrutin_uid, slug, n["polarite"], n["confiance"], n["justification"],
                 modele, PROMPT_VERSION, now),
            )
            _historique(
                conn, scrutin_uid, slug, "reclassement" if ex else "creation", ex, n, modele
            )


def _historique(conn, scrutin_uid, slug, action, avant, apres, auteur) -> None:
    def clean(d):
        if d is None:
            return None
        keep = ("polarite", "confiance", "justification", "statut")
        return json.dumps({k: d[k] for k in keep if k in d}, ensure_ascii=False)

    conn.execute(
        """INSERT INTO historique_classements (scrutin_uid, objectif_slug, action, avant, apres,
           auteur, at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (scrutin_uid, slug, action, clean(avant), clean(apres), auteur, _now()),
    )


def enregistrer_echec(scrutin_uid: str, statut: str, erreur: str, h: str) -> None:
    with db.session() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO analyses (scrutin_uid, statut, resume, modele, prompt_version,
               input_hash, erreur, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)""",
            (scrutin_uid, statut, llm.identifiant(), PROMPT_VERSION, h, erreur[:1000], _now()),
        )


# ------------------------------------------------------------------- sélection


def a_classer(limit: int, natures: list[str] | None = None) -> list[dict]:
    """Scrutins jamais analysés, en erreur, ou dont le contenu / prompt / modèle a changé.

    Priorité : nature (ensemble d'abord), puis scrutins les plus récents.
    """
    from app.ingest.an import est_procedural

    natures = natures or config.CLASSIFY_NATURES
    ordre = {n: i for i, n in enumerate(natures)}
    with db.session() as conn:
        rows = conn.execute(
            f"""SELECT s.*, a.input_hash AS analyse_hash, a.statut AS analyse_statut
                FROM scrutins s LEFT JOIN analyses a ON a.scrutin_uid = s.uid
                WHERE s.nature IN ({','.join('?' * len(natures))})""",
            natures,
        ).fetchall()
    out = []
    for r in rows:
        s = dict(r)
        if est_procedural(s["titre"]):
            continue
        if s["analyse_hash"] == input_hash(s) and s["analyse_statut"] in ("ok", "refus"):
            continue
        out.append(s)
    out.sort(key=lambda s: (ordre.get(s["nature"], 99), -(s["numero"] or 0)))
    return out[:limit]


def classify(limit: int | None = None, natures: list[str] | None = None) -> dict[str, Any]:
    limit = config.CLASSIFY_LIMIT if limit is None else limit
    todo = a_classer(limit, natures)
    stats = {"a_traiter": len(todo), "ok": 0, "refus": 0, "erreur": 0}
    if not todo:
        return stats

    def run(s):
        h = input_hash(s)
        try:
            enregistrer(s["uid"], appeler_llm(s), h)
            return "ok"
        except Refus as e:
            enregistrer_echec(s["uid"], "refus", str(e), h)
            return "refus"
        except ErreurConfiguration:
            raise  # clé ou modèle invalide : inutile de continuer
        except Exception as e:  # noqa: BLE001 - on journalise et on passe au suivant
            log.warning("Échec classement %s : %s", s["uid"], e)
            enregistrer_echec(s["uid"], "erreur", repr(e), h)
            return "erreur"

    log.info("Classement de %d scrutins avec %s", len(todo), llm.identifiant())
    with ThreadPoolExecutor(max_workers=config.LLM_CONCURRENCY) as pool:
        futures = [pool.submit(run, s) for s in todo]
        try:
            for i, f in enumerate(as_completed(futures), 1):
                stats[f.result()] += 1
                if i % 25 == 0:
                    log.info("Classement : %d / %d", i, len(todo))
        except ErreurConfiguration:
            pool.shutdown(cancel_futures=True)  # on n'envoie pas le reste du lot
            raise
    return stats
