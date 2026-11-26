"""Rattachement des scrutins d'amendement au texte de l'amendement (dispositif + exposé sommaire).

Les scrutins AN ne contiennent que « l'amendement n° 874 de Mme X à l'article 4 du projet
de loi… ». Sans le contenu de l'amendement, le classement thématique serait de la devinette :
on va donc chercher l'amendement correspondant dans l'archive des amendements.

Appariement, du plus sûr au moins sûr :
  1. même dossier législatif + même numéro (+ date du sort = date du scrutin si plusieurs) ;
  2. même numéro + date du sort = date du scrutin ;
  3. même numéro + auteur cité dans le titre du scrutin + déposé dans les 90 jours avant le vote.
À chaque étape, on départage par l'auteur ; si l'ambiguïté persiste, on n'apparie pas.
"""

from __future__ import annotations

import html
import json
import logging
import re
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

_NUM = re.compile(r"amendement n[°o]\s*(\d+)", re.I)
_AUTEUR_TITRE = re.compile(r"amendement n[°o]\s*\d+\s*(?:\([^)]*\)\s*)?(?:de|du)\s+(?:M\.|Mme)?\s*([^,]+?)\s+(?:à|après|avant|aux|au|et)\s", re.I)
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Amdt:
    name: str
    dossier: str
    numero: str
    date_sort: str
    date_depot: str
    auteur: str


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", html.unescape(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", " ", s.lower()).strip()


def html_to_text(s: str | None) -> str | None:
    if not isinstance(s, str) or not s.strip():
        return None
    s = re.sub(r"</p>\s*", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = html.unescape(_TAG.sub("", s)).replace("\xa0", " ")
    return re.sub(r"\n{2,}", "\n", s).strip() or None


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def premier_auteur(a: dict) -> str:
    sign = a.get("signataires") or {}
    libelle = html.unescape(_str(sign.get("libelle")))
    premier = libelle.split(",")[0]
    premier = re.sub(r"^(M\.|Mme)\s*", "", premier.strip())
    return norm(premier)


def auteur_du_titre(titre: str) -> str | None:
    m = _AUTEUR_TITRE.search(titre.replace("’", "'"))
    return norm(m.group(1)) if m else None


def build_index(zip_path: Path, organe_seance: str) -> list[Amdt]:
    """Liste des amendements examinés en séance publique (organe `organe_seance`)."""
    out: list[Amdt] = []
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            parts = name.split("/")
            if len(parts) != 4 or organe_seance not in parts[3]:
                continue
            a = json.loads(z.read(name))["amendement"]
            cycle = a.get("cycleDeVie") or {}
            out.append(
                Amdt(
                    name=name,
                    dossier=parts[1],
                    numero=_str(a["identification"].get("numeroLong")).split(" ")[0],
                    date_sort=_str(cycle.get("dateSort"))[:10],
                    date_depot=_str(cycle.get("dateDepot"))[:10],
                    auteur=premier_auteur(a),
                )
            )
    return out


def _departage(cands: list[Amdt], auteur: str | None) -> Amdt | None:
    if len(cands) == 1:
        return cands[0]
    if auteur and cands:
        par_auteur = [c for c in cands if c.auteur and (c.auteur in auteur or auteur in c.auteur)]
        if len(par_auteur) == 1:
            return par_auteur[0]
    return None


def match(scrutin: dict, by_dossier, by_jour, by_num) -> Amdt | None:
    m = _NUM.search(scrutin["titre"])
    if not m:
        return None
    num, jour, dossier = m.group(1), scrutin["date"], scrutin.get("dossier_ref")
    auteur = auteur_du_titre(scrutin["titre"])

    if dossier:
        cands = by_dossier.get((dossier, num), [])
        if len(cands) > 1:
            cands = [c for c in cands if c.date_sort == jour] or cands
        if found := _departage(cands, auteur):
            return found

    if found := _departage(by_jour.get((num, jour), []), auteur):
        return found

    if auteur:
        try:
            d_vote = date.fromisoformat(jour)
        except (TypeError, ValueError):
            return None
        cands = []
        for c in by_num.get(num, []):
            if not c.date_depot or not (c.auteur and (c.auteur in auteur or auteur in c.auteur)):
                continue
            delta = (d_vote - date.fromisoformat(c.date_depot)).days
            if 0 <= delta <= 90:
                cands.append(c)
        if len(cands) == 1:
            return cands[0]
    return None


def enrich(scrutins: list[dict], zip_path: Path) -> dict[str, int]:
    """Ajoute le texte de l'amendement aux scrutins d'amendement (modifie `scrutins` en place)."""
    cibles = [it["scrutin"] for it in scrutins if it["scrutin"]["nature"] == "amendement"]
    if not cibles:
        return {"apparies": 0, "non_apparies": 0}
    organe = cibles[0].get("organe_ref") or "PO838901"
    index = build_index(zip_path, organe)

    by_dossier, by_jour, by_num = defaultdict(list), defaultdict(list), defaultdict(list)
    for a in index:
        by_dossier[(a.dossier, a.numero)].append(a)
        by_jour[(a.numero, a.date_sort)].append(a)
        by_num[a.numero].append(a)

    ok = 0
    with zipfile.ZipFile(zip_path) as z:
        for s in cibles:
            found = match(s, by_dossier, by_jour, by_num)
            if not found:
                continue
            a = json.loads(z.read(found.name))["amendement"]
            contenu = ((a.get("corps") or {}).get("contenuAuteur")) or {}
            s["amendement_uid"] = a["uid"]
            s["amendement_auteur"] = html.unescape(_str((a.get("signataires") or {}).get("libelle")))[:500] or None
            s["amendement_dispositif"] = html_to_text(contenu.get("dispositif"))
            s["amendement_expose"] = html_to_text(contenu.get("exposeSommaire"))
            ok += 1
    log.info("Amendements appariés : %d / %d", ok, len(cibles))
    return {"apparies": ok, "non_apparies": len(cibles) - ok}
