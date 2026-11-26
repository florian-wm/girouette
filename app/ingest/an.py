"""Ingestion des données ouvertes de l'Assemblée nationale (scrutins, groupes, députés)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx

from app import config, db
from app.ingest import amendements
from app.ingest.download import download

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- utils


def as_list(v: Any) -> list:
    """Le JSON AN (converti depuis XML) met un dict seul ou une liste selon le cardinal."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def as_str(v: Any) -> str | None:
    """Les champs nil sont représentés par un dict {"@xsi:nil": "true"}."""
    return v if isinstance(v, str) else None


def iter_zip_json(path: Path, prefix: str = "") -> Iterator[tuple[str, dict]]:
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith(".json") and prefix in name:
                yield name, json.loads(z.read(name))


# ------------------------------------------------------------------------ nature

_PROCEDURE = re.compile(r"suspension de séance|demande de constitution|rappel au règlement", re.I)


def nature_scrutin(titre: str, code_type_vote: str | None) -> str:
    """Classe le scrutin selon ce qui est voté (utilisé pour la pondération)."""
    t = titre.lower().replace("’", "'")
    if code_type_vote == "MOC" or "motion" in t[:40]:
        return "motion"
    if "amendement" in t[:40]:
        return "amendement"
    if t.startswith("l'ensemble") or " l'ensemble " in t[:30]:
        return "ensemble"
    if re.match(r"^la (première|deuxième|seconde|troisième) partie", t):
        return "partie"
    if re.match(r"^l'a(r)?ticle", t):
        return "article"
    return "autre"


def est_procedural(titre: str) -> bool:
    return bool(_PROCEDURE.search(titre))


# ----------------------------------------------------------------------- parsing


def parse_organes_acteurs(amo_zip: Path) -> tuple[list[dict], list[dict]]:
    groupes, deputes = [], []
    for name, data in iter_zip_json(amo_zip):
        if "/organe/" in name:
            o = data["organe"]
            if o.get("codeType") == "GP" and o.get("legislature") == config.LEGISLATURE:
                fin = (o.get("viMoDe") or {}).get("dateFin")
                groupes.append(
                    {
                        "uid": o["uid"],
                        "abrev": o.get("libelleAbrev"),
                        "libelle": o.get("libelle"),
                        "actif": 0 if as_str(fin) else 1,
                    }
                )
        elif "/acteur/" in name:
            a = data["acteur"]
            uid = a["uid"]["#text"] if isinstance(a["uid"], dict) else a["uid"]
            ident = a["etatCivil"]["ident"]
            deputes.append({"uid": uid, "prenom": ident.get("prenom"), "nom": ident.get("nom")})
    return groupes, deputes


_POSITIONS = {
    "pours": "pour",
    "contres": "contre",
    "abstentions": "abstention",
    "nonVotants": "non_votant",
}


def parse_scrutin(data: dict) -> dict:
    """Transforme un scrutin JSON AN en dict à plat + votes par groupe + votes nominatifs."""
    s = data["scrutin"]
    synthese = s.get("syntheseVote") or {}
    decompte = synthese.get("decompte") or {}
    objet = s.get("objet") or {}
    dossier = objet.get("dossierLegislatif") or {}
    titre = (s.get("titre") or objet.get("libelle") or "").strip()
    type_vote = (s.get("typeVote") or {}).get("codeTypeVote")

    groupes, nominatifs = [], []
    ventilation = ((s.get("ventilationVotes") or {}).get("organe") or {}).get("groupes") or {}
    for idx, g in enumerate(as_list(ventilation.get("groupe"))):
        vote = g.get("vote") or {}
        dv = vote.get("decompteVoix") or {}
        groupes.append(
            {
                "groupe_uid": g["organeRef"],
                "membres": as_int(g.get("nombreMembresGroupe")),
                "position_majoritaire": vote.get("positionMajoritaire"),
                "pour": as_int(dv.get("pour")),
                "contre": as_int(dv.get("contre")),
                "abstentions": as_int(dv.get("abstentions")),
                "non_votants": as_int(dv.get("nonVotants")),
            }
        )
        dn = vote.get("decompteNominatif") or {}
        for key, position in _POSITIONS.items():
            bloc = dn.get(key)
            if not isinstance(bloc, dict):
                continue
            for v in as_list(bloc.get("votant")):
                nominatifs.append(
                    {
                        "depute_uid": v["acteurRef"],
                        "groupe_uid": g["organeRef"],
                        "position": position,
                        "par_delegation": 1 if v.get("parDelegation") == "true" else 0,
                        "_idx": idx,
                    }
                )

    row = {
        "uid": s["uid"],
        "legislature": s.get("legislature"),
        "numero": as_int(s.get("numero")),
        "date": s.get("dateScrutin"),
        "type_vote": type_vote,
        "nature": nature_scrutin(titre, type_vote),
        "titre": titre,
        "dossier_ref": as_str(dossier.get("dossierRef")),
        "dossier_libelle": as_str(dossier.get("libelle")),
        "sort": (s.get("sort") or {}).get("code"),
        "pour": as_int(decompte.get("pour")),
        "contre": as_int(decompte.get("contre")),
        "abstentions": as_int(decompte.get("abstentions")),
        "votants": as_int(synthese.get("nombreVotants")),
        "organe_ref": s.get("organeRef"),
    }
    return {"scrutin": row, "groupes": groupes, "nominatifs": nominatifs}


GROUPE_INCONNU = "PO0"


def repare_groupes_inconnus(scrutins: list[dict]) -> int:
    """Certains scrutins de la source ont tous leurs groupes notés « PO0 ».

    On retrouve le groupe à partir des députés qui ont voté : leur groupe le plus fréquent
    dans les autres scrutins du même mois. Les groupes non identifiables sont écartés.
    Retourne le nombre de groupes écartés.
    """
    appartenance: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for it in scrutins:
        mois = (it["scrutin"]["date"] or "")[:7]
        for v in it["nominatifs"]:
            if v["groupe_uid"] != GROUPE_INCONNU:
                appartenance[(v["depute_uid"], mois)][v["groupe_uid"]] += 1

    ecartes = 0
    for it in scrutins:
        if not any(g["groupe_uid"] == GROUPE_INCONNU for g in it["groupes"]):
            continue
        mois = (it["scrutin"]["date"] or "")[:7]
        # Les votes nominatifs sont dans l'ordre des groupes : on les regroupe par bloc.
        blocs: list[list[dict]] = [[] for _ in it["groupes"]]
        for v in it["nominatifs"]:
            blocs[v.pop("_idx")].append(v)
        gardes, nominatifs = [], []
        for g, votants in zip(it["groupes"], blocs):
            if g["groupe_uid"] == GROUPE_INCONNU:
                votes = Counter()
                for v in votants:
                    votes.update(appartenance.get((v["depute_uid"], mois), {}))
                if not votes:
                    ecartes += 1
                    continue
                g["groupe_uid"] = votes.most_common(1)[0][0]
                for v in votants:
                    v["groupe_uid"] = g["groupe_uid"]
            if any(x["groupe_uid"] == g["groupe_uid"] for x in gardes):
                ecartes += 1
                continue
            gardes.append(g)
            nominatifs.extend(votants)
        it["groupes"], it["nominatifs"] = gardes, nominatifs
    for it in scrutins:
        for v in it["nominatifs"]:
            v.pop("_idx", None)
    if ecartes:
        log.warning("%d groupe(s) non identifiable(s) écarté(s) (organeRef PO0)", ecartes)
    return ecartes


def content_hash(row: dict) -> str:
    keys = (
        "titre", "dossier_libelle", "nature", "sort",
        "amendement_dispositif", "amendement_expose",
    )
    payload = json.dumps([row.get(k) for k in keys], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ------------------------------------------------------------------------ loading

_SCRUTIN_COLS = (
    "uid", "legislature", "numero", "date", "type_vote", "nature", "titre", "dossier_ref",
    "dossier_libelle", "sort", "pour", "contre", "abstentions", "votants", "amendement_uid",
    "amendement_auteur", "amendement_dispositif", "amendement_expose", "content_hash",
    "updated_at",
)


def load(
    groupes: Iterable[dict], deputes: Iterable[dict], scrutins: list[dict]
) -> dict[str, int]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.session() as conn:
        conn.executemany(
            """INSERT INTO groupes (uid, abrev, libelle, actif) VALUES (:uid, :abrev, :libelle, :actif)
               ON CONFLICT(uid) DO UPDATE SET abrev=excluded.abrev, libelle=excluded.libelle,
               actif=excluded.actif""",
            list(groupes),
        )
        conn.executemany(
            """INSERT INTO deputes (uid, prenom, nom) VALUES (:uid, :prenom, :nom)
               ON CONFLICT(uid) DO UPDATE SET prenom=excluded.prenom, nom=excluded.nom""",
            list(deputes),
        )
        placeholders = ", ".join(f":{c}" for c in _SCRUTIN_COLS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in _SCRUTIN_COLS if c != "uid")
        rows = []
        for item in scrutins:
            r = dict(item["scrutin"])
            r.setdefault("amendement_uid", None)
            r.setdefault("amendement_auteur", None)
            r.setdefault("amendement_dispositif", None)
            r.setdefault("amendement_expose", None)
            r["content_hash"] = content_hash(r)
            r["updated_at"] = now
            rows.append({c: r.get(c) for c in _SCRUTIN_COLS})
        conn.executemany(
            f"INSERT INTO scrutins ({', '.join(_SCRUTIN_COLS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(uid) DO UPDATE SET {updates}",
            rows,
        )
        # Votes : remplacés intégralement (source = vérité, pas de saisie manuelle).
        uids = [(item["scrutin"]["uid"],) for item in scrutins]
        conn.executemany("DELETE FROM votes_groupes WHERE scrutin_uid = ?", uids)
        conn.executemany("DELETE FROM votes_deputes WHERE scrutin_uid = ?", uids)
        conn.executemany(
            """INSERT INTO votes_groupes (scrutin_uid, groupe_uid, membres, position_majoritaire,
               pour, contre, abstentions, non_votants) VALUES (:scrutin_uid, :groupe_uid, :membres,
               :position_majoritaire, :pour, :contre, :abstentions, :non_votants)""",
            [
                {**g, "scrutin_uid": item["scrutin"]["uid"]}
                for item in scrutins
                for g in item["groupes"]
            ],
        )
        conn.executemany(
            """INSERT OR REPLACE INTO votes_deputes (scrutin_uid, depute_uid, groupe_uid, position,
               par_delegation) VALUES (:scrutin_uid, :depute_uid, :groupe_uid, :position,
               :par_delegation)""",
            [
                {**v, "scrutin_uid": item["scrutin"]["uid"]}
                for item in scrutins
                for v in item["nominatifs"]
            ],
        )
    return {"scrutins": len(rows)}


def ingest(force: bool = False) -> dict[str, Any]:
    """Télécharge et charge les données AN. Idempotent."""
    cache = config.CACHE_DIR / f"an{config.LEGISLATURE}"
    scrutins_zip = cache / "Scrutins.json.zip"
    amo_zip = cache / "AMO30.json.zip"
    amdt_zip = cache / "Amendements.json.zip"

    changed = download(config.AN_URLS["scrutins"], scrutins_zip, force)
    changed |= download(config.AN_URLS["amo"], amo_zip, force)
    if config.INGEST_AMENDEMENTS:
        try:
            changed |= download(config.AN_URLS["amendements"], amdt_zip, force)
        except httpx.HTTPError as e:
            # Non bloquant : on garde l'archive précédente si elle existe.
            log.error("Archive des amendements indisponible (%s), version en cache utilisée", e)
    if not changed and not force:
        return {"statut": "inchangé"}

    groupes, deputes = parse_organes_acteurs(amo_zip)
    scrutins = [parse_scrutin(data) for _, data in iter_zip_json(scrutins_zip)]
    stats: dict[str, Any] = {"groupes_inconnus_ecartes": repare_groupes_inconnus(scrutins)}
    log.info("%d scrutins, %d groupes, %d députés lus", len(scrutins), len(groupes), len(deputes))

    if config.INGEST_AMENDEMENTS and amdt_zip.exists():
        stats["amendements"] = amendements.enrich(scrutins, amdt_zip)

    stats.update(load(groupes, deputes, scrutins))
    return stats
