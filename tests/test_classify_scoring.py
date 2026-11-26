import pytest

from app import classify, db, scoring
from app.ingest.an import load, parse_scrutin
from tests.conftest import make_scrutin

GROUPES = [{"uid": "PO1", "abrev": "G1", "libelle": "Groupe 1", "actif": 1},
           {"uid": "PO2", "abrev": "G2", "libelle": "Groupe 2", "actif": 1}]


def _charger():
    load(GROUPES, [], [
        # G1 vote pour (10/0/0), G2 contre (0/8/0)
        parse_scrutin(make_scrutin("S1", 1, "2026-01-01", "l'ensemble de la proposition de loi revalorisant le RSA",
                                   groupes={"PO1": (10, 0, 0), "PO2": (0, 8, 0)})),
        # amendement de suppression : G1 contre, G2 pour
        parse_scrutin(make_scrutin("S2", 2, "2026-01-02", "l'amendement n° 5 de M. X à l'article 1 de la proposition de loi",
                                   groupes={"PO1": (0, 10, 0), "PO2": (8, 0, 0)})),
        parse_scrutin(make_scrutin("S3", 3, "2026-01-03", "la demande de suspension de séance présentée par M. Y")),
    ])


def _llm(objectifs, resume="Résumé"):
    return {"resume": resume, "objectifs": objectifs, "_modele": "test-model"}


def test_pipeline_classement_et_score():
    _charger()
    todo = classify.a_classer(100)
    assert [s["uid"] for s in todo] == ["S1", "S2"]  # ensemble d'abord, procédural exclu

    classify.enregistrer("S1", _llm([{"objectif": "pauvrete", "polarite": "pour_fait_avancer",
                                      "confiance": 0.9, "justification": "Revalorise le RSA."}]),
                         classify.input_hash(todo[0]))
    classify.enregistrer("S2", _llm([
        {"objectif": "pauvrete", "polarite": "pour_fait_reculer", "confiance": 0.8, "justification": "Supprime."},
        {"objectif": "climat", "polarite": "pour_fait_avancer", "confiance": 0.2, "justification": "Trop faible."},
    ]), classify.input_hash(todo[1]))

    with db.session() as conn:
        rows = conn.execute("SELECT scrutin_uid, objectif_slug, polarite FROM classements ORDER BY scrutin_uid").fetchall()
    assert [tuple(r) for r in rows] == [("S1", "pauvrete", 1), ("S2", "pauvrete", -1)]  # confiance < 0,4 écartée

    assert classify.a_classer(100) == []  # déjà analysés, rien à refaire

    scores = scoring.calculer()
    assert scores[("G1", "pauvrete")].valeur == 100.0
    assert scores[("G2", "pauvrete")].valeur == 0.0
    assert scores[("G1", "pauvrete")].n_scrutins == 2


def test_reclassement_respecte_les_decisions_humaines():
    _charger()
    s = classify.a_classer(1)[0]
    h = classify.input_hash(s)
    classify.enregistrer("S1", _llm([
        {"objectif": "pauvrete", "polarite": "pour_fait_avancer", "confiance": 0.9, "justification": "a"},
        {"objectif": "pouvoir-achat", "polarite": "pour_fait_avancer", "confiance": 0.6, "justification": "b"},
    ]), h)
    with db.session() as conn:
        conn.execute("UPDATE classements SET statut='rejete' WHERE objectif_slug='pouvoir-achat'")
    # Le LLM change d'avis : pauvreté disparaît (auto -> supprimé), pouvoir d'achat rejeté reste rejeté
    classify.enregistrer("S1", _llm([
        {"objectif": "pouvoir-achat", "polarite": "pour_fait_reculer", "confiance": 0.9, "justification": "c"},
    ]), h)
    with db.session() as conn:
        rows = {r["objectif_slug"]: dict(r) for r in conn.execute("SELECT * FROM classements")}
        histo = conn.execute("SELECT COUNT(*) FROM historique_classements").fetchone()[0]
    assert set(rows) == {"pouvoir-achat"}
    assert rows["pouvoir-achat"]["statut"] == "rejete" and rows["pouvoir-achat"]["polarite"] == 1
    assert histo == 3


def test_position_et_poids():
    assert scoring.position_groupe(0, 0, 0) is None
    assert scoring.position_groupe(5, 5, 0) == 0
    assert scoring.position_groupe(2, 0, 2) == 0.5
    assert scoring.poids("ensemble", 0.5, "valide") == 4.0
    assert scoring.poids("amendement", 0.5, "auto") == 0.5


def test_user_message_amendement_sans_texte():
    s = {"numero": 1, "date": "2026-01-01", "nature": "amendement", "titre": "l'amendement n° 1",
         "dossier_libelle": None, "sort": "rejeté"}
    assert "non disponible" in classify.user_message(s)
    s.update(amendement_expose="x" * 20000, amendement_dispositif="Supprimer.")
    msg = classify.user_message(s)
    assert "texte tronqué" in msg and "Supprimer." in msg


def test_schema_et_prompt():
    schema = classify.output_schema()
    assert schema["properties"]["objectifs"]["items"]["properties"]["objectif"]["enum"][0] == "pauvrete"
    assert "Hôpital".lower() in classify.system_prompt().lower()
