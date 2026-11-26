"""Agrégation des votes en scores par groupe et par objectif.

Pour un scrutin classé sur un objectif avec une polarité p (+1 : voter pour fait avancer
l'objectif affiché ; -1 : le fait reculer), la position d'un groupe vaut :

    position = (pour - contre) / (pour + contre + abstentions)   ∈ [-1, 1]

Le score d'un groupe sur un objectif est la moyenne pondérée de p × position, avec
poids = poids_nature × confiance (1 si validé à la main). Ramené sur 0–100 :
0 = a toujours voté contre l'objectif affiché, 50 = neutre / partagé, 100 = toujours pour.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app import config, db, referentiel


@dataclass
class Score:
    groupe: str  # sigle du groupe (un groupe dissous puis recréé garde le même sigle)
    objectif: str
    somme: float = 0.0
    poids: float = 0.0
    n_scrutins: int = 0
    n_valides: int = 0
    details: list = field(default_factory=list)

    @property
    def valeur(self) -> float | None:
        if self.poids == 0:
            return None
        return round((self.somme / self.poids + 1) * 50, 1)


def position_groupe(pour: int, contre: int, abstentions: int) -> float | None:
    exprimes = pour + contre + abstentions
    if exprimes == 0:
        return None
    return (pour - contre) / exprimes


def poids(nature: str, confiance: float, statut: str) -> float:
    c = 1.0 if statut == "valide" else confiance
    return config.POIDS_NATURE.get(nature, 1.0) * c


_QUERY = """
SELECT c.scrutin_uid, c.objectif_slug, c.polarite, c.confiance, c.statut,
       s.nature, s.date, COALESCE(g.abrev, vg.groupe_uid) AS groupe,
       vg.pour, vg.contre, vg.abstentions
FROM classements c
JOIN scrutins s ON s.uid = c.scrutin_uid
JOIN votes_groupes vg ON vg.scrutin_uid = c.scrutin_uid
LEFT JOIN groupes g ON g.uid = vg.groupe_uid
WHERE c.statut != 'rejete' {filtre}
"""


def calculer(objectif: str | None = None, groupe: str | None = None,
             depuis: str | None = None) -> dict[tuple[str, str], Score]:
    filtre, params = [], []
    if objectif:
        filtre.append("AND c.objectif_slug = ?")
        params.append(objectif)
    if groupe:
        filtre.append("AND COALESCE(g.abrev, vg.groupe_uid) = ?")
        params.append(groupe)
    if depuis:
        filtre.append("AND s.date >= ?")
        params.append(depuis)
    scores: dict[tuple[str, str], Score] = {}
    with db.session() as conn:
        rows = conn.execute(_QUERY.format(filtre=" ".join(filtre)), params).fetchall()
    for r in rows:
        pos = position_groupe(r["pour"], r["contre"], r["abstentions"])
        if pos is None:
            continue
        key = (r["groupe"], r["objectif_slug"])
        sc = scores.setdefault(key, Score(r["groupe"], r["objectif_slug"]))
        w = poids(r["nature"], r["confiance"], r["statut"])
        sc.somme += w * r["polarite"] * pos
        sc.poids += w
        sc.n_scrutins += 1
        sc.n_valides += r["statut"] == "valide"
    return scores


def groupes_affiches() -> list[dict]:
    """Groupes actifs, enrichis de la correspondance partis/couleur."""
    mapping = referentiel.groupes_partis()
    with db.session() as conn:
        rows = conn.execute(
            """SELECT g.*, (SELECT membres FROM votes_groupes vg JOIN scrutins s
                   ON s.uid = vg.scrutin_uid WHERE vg.groupe_uid = g.uid
                   ORDER BY s.date DESC, s.numero DESC LIMIT 1) AS membres
               FROM groupes g WHERE g.actif = 1"""
        ).fetchall()
    out = []
    for r in rows:
        g = dict(r)
        m = mapping.get(g["abrev"], {})
        g["partis"] = m.get("partis", [])
        g["couleur"] = m.get("couleur", "#888888")
        out.append(g)
    out.sort(key=lambda g: -(g["membres"] or 0))
    return out


def matrice() -> dict:
    """Tableau objectifs × groupes pour la page d'accueil et l'API."""
    scores = calculer()
    groupes = groupes_affiches()
    objectifs = referentiel.objectifs()
    lignes = []
    for o in objectifs:
        cellules = []
        for g in groupes:
            sc = scores.get((g["abrev"], o["slug"]))
            cellules.append(
                {
                    "groupe": g,
                    "valeur": sc.valeur if sc else None,
                    "n_scrutins": sc.n_scrutins if sc else 0,
                }
            )
        lignes.append({"objectif": o, "cellules": cellules})
    return {"groupes": groupes, "lignes": lignes}


def stats_globales() -> dict:
    with db.session() as conn:
        q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "scrutins": q("SELECT COUNT(*) FROM scrutins"),
            "analyses": q("SELECT COUNT(*) FROM analyses WHERE statut = 'ok'"),
            "classes": q("SELECT COUNT(DISTINCT scrutin_uid) FROM classements WHERE statut != 'rejete'"),
            "valides": q("SELECT COUNT(*) FROM classements WHERE statut = 'valide'"),
            "derniere_date": q("SELECT MAX(date) FROM scrutins"),
            "par_objectif": {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT objectif_slug, COUNT(*) FROM classements WHERE statut != 'rejete' "
                    "GROUP BY objectif_slug"
                )
            },
        }


def par_objectif_groupe(objectif: str) -> dict[str, list[dict]]:
    """Détail des scrutins d'un objectif avec la position de chaque groupe (page objectif)."""
    with db.session() as conn:
        rows = conn.execute(
            """SELECT c.*, s.numero, s.date, s.titre, s.nature, s.sort, s.dossier_libelle,
                      a.resume
               FROM classements c JOIN scrutins s ON s.uid = c.scrutin_uid
               LEFT JOIN analyses a ON a.scrutin_uid = c.scrutin_uid
               WHERE c.objectif_slug = ? AND c.statut != 'rejete'
               ORDER BY s.date DESC, s.numero DESC""",
            (objectif,),
        ).fetchall()
        votes = defaultdict(dict)
        for v in conn.execute(
            """SELECT vg.*, COALESCE(g.abrev, vg.groupe_uid) AS groupe FROM votes_groupes vg
               JOIN classements c ON c.scrutin_uid = vg.scrutin_uid
               LEFT JOIN groupes g ON g.uid = vg.groupe_uid WHERE c.objectif_slug = ?""",
            (objectif,),
        ):
            votes[v["scrutin_uid"]][v["groupe"]] = dict(v)
    return {"scrutins": [dict(r) for r in rows], "votes": votes}
