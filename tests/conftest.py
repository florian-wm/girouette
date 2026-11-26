import pytest

from app import config, db


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "PIPELINE_CRON", "")
    db.init_db()
    yield


def make_scrutin(uid="VTANR5L17V1", numero=1, date="2026-01-15", titre="l'ensemble du projet de loi X",
                 groupes=None, type_vote="SPO", dossier_ref="DLR5L17N1", dossier_libelle="Dossier X"):
    """Construit un scrutin au format JSON de l'open data AN."""
    groupes = groupes or {"PO1": (10, 2, 1), "PO2": (0, 8, 0)}
    g_json = []
    for i, (guid, (pour, contre, abst)) in enumerate(groupes.items()):
        votants = lambda n, p: {"votant": [{"acteurRef": f"PA{guid}{p}{k}", "mandatRef": "PM", "parDelegation": "false"} for k in range(n)]} if n else None
        g_json.append({
            "organeRef": guid, "nombreMembresGroupe": str(pour + contre + abst + 1),
            "vote": {
                "positionMajoritaire": "pour" if pour >= contre else "contre",
                "decompteVoix": {"nonVotants": "1", "pour": str(pour), "contre": str(contre), "abstentions": str(abst)},
                "decompteNominatif": {"nonVotants": None, "pours": votants(pour, "p"), "contres": votants(contre, "c"),
                                      "abstentions": votants(abst, "a")},
            },
        })
    tp = sum(v[0] for v in groupes.values()); tc = sum(v[1] for v in groupes.values()); ta = sum(v[2] for v in groupes.values())
    return {"scrutin": {
        "uid": uid, "numero": str(numero), "organeRef": "PO838901", "legislature": "17", "dateScrutin": date,
        "typeVote": {"codeTypeVote": type_vote}, "sort": {"code": "adopté" if tp > tc else "rejeté"},
        "titre": titre,
        "objet": {"libelle": titre, "dossierLegislatif": {"libelle": dossier_libelle, "dossierRef": dossier_ref} if dossier_ref else {"@xsi:nil": "true"}},
        "syntheseVote": {"nombreVotants": str(tp + tc + ta), "decompte": {"pour": str(tp), "contre": str(tc), "abstentions": str(ta)}},
        "ventilationVotes": {"organe": {"organeRef": "PO838901", "groupes": {"groupe": g_json if len(g_json) > 1 else g_json[0]}}},
    }}
