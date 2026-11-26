import json
import zipfile

from app import db
from app.ingest import amendements
from app.ingest.an import load, nature_scrutin, parse_scrutin
from tests.conftest import make_scrutin


def test_nature_scrutin():
    assert nature_scrutin("l'ensemble du projet de loi de finances pour 2026", "SPO") == "ensemble"
    assert nature_scrutin("l'amendement n° 874 de Mme X à l'article 4 du projet de loi", "SPO") == "amendement"
    assert nature_scrutin("le sous-amendement n° 12 de M. Y à l'amendement n° 3", "SPO") == "amendement"
    assert nature_scrutin("l’article 15 (examen prioritaire) du projet de loi", "SPO") == "article"
    assert nature_scrutin("l'aticle 6 du projet de loi sur la justice", "SPO") == "article"
    assert nature_scrutin("la motion de rejet préalable, déposée par Mme Panot", "SPO") == "motion"
    assert nature_scrutin("la motion de censure", "MOC") == "motion"
    assert nature_scrutin("la deuxième partie projet de loi de financement de la sécurité sociale", "SPO") == "partie"
    assert nature_scrutin("la proposition de résolution visant à …", "SPO") == "autre"


def test_parse_scrutin_et_load():
    item = parse_scrutin(make_scrutin(groupes={"PO1": (3, 1, 0)}))  # un seul groupe : dict, pas liste
    assert item["scrutin"]["pour"] == 3 and item["scrutin"]["nature"] == "ensemble"
    assert item["groupes"] == [{"groupe_uid": "PO1", "membres": 5, "position_majoritaire": "pour",
                                "pour": 3, "contre": 1, "abstentions": 0, "non_votants": 1}]
    assert sorted(v["position"] for v in item["nominatifs"]) == ["contre", "pour", "pour", "pour"]

    load([{"uid": "PO1", "abrev": "G1", "libelle": "Groupe 1", "actif": 1}], [], [item])
    load([], [], [item])  # idempotent
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM votes_deputes").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM votes_groupes").fetchone()[0] == 1


def test_scrutin_sans_dossier():
    item = parse_scrutin(make_scrutin(dossier_ref=None))
    assert item["scrutin"]["dossier_ref"] is None


def _amdt(uid, numero, date_sort, date_depot, auteur, expose):
    return {"amendement": {
        "uid": uid, "identification": {"numeroLong": numero},
        "signataires": {"libelle": auteur},
        "corps": {"contenuAuteur": {"dispositif": "<p>Supprimer cet article.</p>",
                                    "exposeSommaire": f"<p>{expose}</p><p>Suite&#x00E9;</p>"}},
        "cycleDeVie": {"dateSort": date_sort, "dateDepot": date_depot},
    }}


def test_appariement_amendements(tmp_path):
    zpath = tmp_path / "a.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        files = {
            # 1. même dossier + numéro
            "json/DLR1/T1/AMANR5L17PO838901BT1N000010.json": _amdt("A10", "10", "2026-01-15T10:00:00+01:00", "2026-01-10", "M.&#160;Dupont, Mme&#160;X", "Expose 10"),
            # 2. pas de dossier dans le scrutin -> numéro + date
            "json/DLR2/T2/AMANR5L17PO838901BT2N000020.json": _amdt("A20", "20", "2026-02-01T10:00:00+01:00", "2026-01-25", "Mme&#160;Martin", "Expose 20"),
            "json/DLR3/T3/AMANR5L17PO838901BT3N000020.json": _amdt("A20b", "20", "2026-03-01T10:00:00+01:00", "2026-02-25", "M.&#160;Autre", "Autre"),
            # 3. pas de date de sort -> auteur + date de dépôt
            "json/DLR4/T4/AMANR5L17PO838901BT4N000030.json": _amdt("A30", "30", {"@xsi:nil": "true"}, "2026-03-20", "M.&#160;Le&#160;Fur", "Expose 30"),
            # amendement de commission : ignoré
            "json/DLR1/T1/AMANR5L17PO59048BT1N000010.json": _amdt("C10", "10", "2026-01-15", "2026-01-01", "M. Dupont", "Commission"),
        }
        for name, data in files.items():
            z.writestr(name, json.dumps(data))

    scrutins = [
        parse_scrutin(make_scrutin("S1", 1, "2026-01-15", "l'amendement n° 10 de M. Dupont à l'article 2 du projet de loi", dossier_ref="DLR1")),
        parse_scrutin(make_scrutin("S2", 2, "2026-02-01", "l'amendement n° 20 de Mme Martin après l'article 3 de la proposition de loi", dossier_ref=None)),
        parse_scrutin(make_scrutin("S3", 3, "2026-04-02", "l'amendement n° 30 (rect.) de M. Le Fur après l'article 3 du projet de loi de finances", dossier_ref=None)),
        parse_scrutin(make_scrutin("S4", 4, "2026-04-02", "l'amendement n° 99 de M. Inconnu à l'article 1", dossier_ref=None)),
    ]
    stats = amendements.enrich(scrutins, zpath)
    assert stats == {"apparies": 3, "non_apparies": 1}
    s1, s2, s3, s4 = (it["scrutin"] for it in scrutins)
    assert s1["amendement_uid"] == "A10" and s1["amendement_expose"] == "Expose 10\nSuiteé"
    assert s1["amendement_dispositif"] == "Supprimer cet article."
    assert s2["amendement_uid"] == "A20"
    assert s3["amendement_uid"] == "A30"
    assert "amendement_uid" not in s4


def test_auteur_du_titre():
    assert amendements.auteur_du_titre("l'amendement n° 874 de Mme Brulebois à l'article 4") == "brulebois"
    assert amendements.auteur_du_titre("l'amendement n° 1569 (rect.) de M. Maudet après l'article 7") == "maudet"
    assert amendements.auteur_du_titre("l'amendement n° 3 du Gouvernement à l'article liminaire") == "gouvernement"


def test_repare_groupes_inconnus():
    from app.ingest.an import repare_groupes_inconnus

    ref = parse_scrutin(make_scrutin("S1", 1, "2024-12-01", groupes={"PO1": (3, 0, 0), "PO2": (0, 2, 0)}))
    casse = parse_scrutin(make_scrutin("S2", 2, "2024-12-02", groupes={"PO1": (2, 1, 0), "PO2": (0, 2, 0)}))
    # La source a perdu les identifiants de groupe : on les remplace par PO0
    for g in casse["groupes"]:
        g["groupe_uid"] = "PO0"
    for v in casse["nominatifs"]:
        v["groupe_uid"] = "PO0"
    # Un troisième bloc sans aucun votant identifiable
    casse["groupes"].append({"groupe_uid": "PO0", "membres": 3, "position_majoritaire": "pour",
                             "pour": 0, "contre": 0, "abstentions": 0, "non_votants": 3})
    assert repare_groupes_inconnus([ref, casse]) == 1
    assert [g["groupe_uid"] for g in casse["groupes"]] == ["PO1", "PO2"]
    # PA PO1 p0/p1 ont voté dans S1 avec PO1 ; PO1c0 est inconnu mais le bloc est majoritairement PO1
    assert {v["groupe_uid"] for v in casse["nominatifs"]} == {"PO1", "PO2"}
    assert all("_idx" not in v for v in casse["nominatifs"] + ref["nominatifs"])
