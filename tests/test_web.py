import base64

from fastapi.testclient import TestClient

from app import classify, config
from app.web.main import app
from tests.test_classify_scoring import _charger, _llm


def _client():
    return TestClient(app)


def test_pages_publiques():
    _charger()
    classify.enregistrer("S1", _llm([{"objectif": "pauvrete", "polarite": "pour_fait_avancer",
                                      "confiance": 0.9, "justification": "Revalorise le RSA."}]), "h")
    with _client() as c:
        r = c.get("/")
        assert r.status_code == 200 and "Réduire la pauvreté" in r.text
        assert c.get("/objectifs/pauvrete").status_code == 200
        assert c.get("/objectifs/inconnu").status_code == 404
        assert c.get("/groupes/G1").status_code == 200
        r = c.get("/scrutins/S1")
        assert r.status_code == 200 and "Revalorise le RSA." in r.text
        assert c.get("/methode").status_code == 200
        assert c.get("/api/scores").json()["objectifs"][0]["groupes"]["G1"]["score"] == 100.0
        assert c.get("/healthz").json()["ok"]

        r = c.post("/scrutins/S1/signaler", data={"message": "Mauvais sens", "objectif": "pauvrete"},
                   follow_redirects=False)
        assert r.status_code == 303


def test_admin(monkeypatch):
    _charger()
    classify.enregistrer("S1", _llm([{"objectif": "pauvrete", "polarite": "pour_fait_avancer",
                                      "confiance": 0.9, "justification": "x"}]), "h")
    with _client() as c:
        assert c.get("/admin").status_code == 503  # pas de mot de passe configuré
        monkeypatch.setattr(config, "ADMIN_PASSWORD", "secret")
        assert c.get("/admin").status_code == 401
        auth = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
        r = c.get("/admin", headers=auth)
        assert r.status_code == 200 and "Classements à vérifier" in r.text
        r = c.post("/admin/classements/1", data={"action": "inverser"}, headers=auth, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/api/scores").json()["objectifs"][0]["groupes"]["G1"]["score"] == 0.0
