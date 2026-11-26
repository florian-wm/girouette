"""Site public + back-office de validation + API JSON."""

from __future__ import annotations

import json
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import classify, config, db, llm, pipeline, referentiel, scoring

log = logging.getLogger(__name__)
HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler = None
    if config.PIPELINE_CRON:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BackgroundScheduler(timezone=config.TIMEZONE)
        scheduler.add_job(pipeline.run, CronTrigger.from_crontab(config.PIPELINE_CRON),
                          id="pipeline", max_instances=1, coalesce=True)
        scheduler.start()
        log.info("Pipeline planifié : %s (%s)", config.PIPELINE_CRON, config.TIMEZONE)
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Votes & intentions politiques", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def _fmt_date(d: str | None) -> str:
    if not d:
        return ""
    try:
        return datetime.fromisoformat(d[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return d


def _couleur_score(v: float | None) -> str:
    """Palette divergente neutre : orange (contre l'objectif) -> gris -> bleu (pour)."""
    if v is None:
        return "transparent"
    t = (v - 50) / 50  # -1..1
    if t >= 0:
        return f"rgba(37, 99, 235, {0.12 + 0.7 * t:.2f})"
    return f"rgba(234, 88, 12, {0.12 + 0.7 * -t:.2f})"


templates.env.filters["date_fr"] = _fmt_date
templates.env.globals["couleur_score"] = _couleur_score
templates.env.globals["legislature"] = config.LEGISLATURE
templates.env.globals["NATURES"] = {
    "ensemble": "Vote sur l'ensemble d'un texte",
    "partie": "Vote sur une partie d'un texte",
    "motion": "Motion (rejet, censure…)",
    "article": "Vote sur un article",
    "amendement": "Amendement",
    "autre": "Autre",
}


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _objectif(slug: str) -> dict:
    o = next((o for o in referentiel.objectifs() if o["slug"] == slug), None)
    if not o:
        raise HTTPException(404, "Objectif inconnu")
    return o


def _groupes_par_uid() -> dict[str, dict]:
    with db.session() as conn:
        tous = {r["uid"]: dict(r) for r in conn.execute("SELECT * FROM groupes")}
    for g in scoring.groupes_affiches():
        tous[g["uid"]] = g
    mapping = referentiel.groupes_partis()
    for g in tous.values():
        g.setdefault("couleur", mapping.get(g.get("abrev"), {}).get("couleur", "#888"))
    return tous


# ------------------------------------------------------------------ pages publiques


@app.get("/", response_class=HTMLResponse)
def accueil(request: Request):
    return render(request, "index.html", m=scoring.matrice(), stats=scoring.stats_globales())


@app.get("/objectifs/{slug}", response_class=HTMLResponse)
def page_objectif(request: Request, slug: str):
    o = _objectif(slug)
    scores = scoring.calculer(objectif=slug)
    groupes = scoring.groupes_affiches()
    classement = sorted(
        [{"groupe": g, "score": scores.get((g["abrev"], slug))} for g in groupes],
        key=lambda x: -(x["score"].valeur if x["score"] and x["score"].valeur is not None else -1),
    )
    detail = scoring.par_objectif_groupe(slug)
    return render(request, "objectif.html", o=o, classement=classement, groupes=groupes,
                  detail=detail)


@app.get("/groupes/{abrev}", response_class=HTMLResponse)
def page_groupe(request: Request, abrev: str):
    g = next((g for g in scoring.groupes_affiches() if g["abrev"] == abrev), None)
    if not g:
        raise HTTPException(404, "Groupe inconnu")
    scores = scoring.calculer(groupe=abrev)
    lignes = [{"objectif": o, "score": scores.get((abrev, o["slug"]))}
              for o in referentiel.objectifs()]
    with db.session() as conn:
        votes = conn.execute(
            """SELECT s.uid, s.numero, s.date, s.titre, s.nature, c.objectif_slug, c.polarite,
                      vg.position_majoritaire, vg.pour, vg.contre, vg.abstentions, a.resume
               FROM classements c JOIN scrutins s ON s.uid = c.scrutin_uid
               JOIN votes_groupes vg ON vg.scrutin_uid = s.uid
               JOIN groupes gr ON gr.uid = vg.groupe_uid AND gr.abrev = ?
               LEFT JOIN analyses a ON a.scrutin_uid = s.uid
               WHERE c.statut != 'rejete'
               ORDER BY s.date DESC, s.numero DESC LIMIT 100""",
            (abrev,),
        ).fetchall()
    return render(request, "groupe.html", g=g, lignes=lignes, votes=votes,
                  objectifs={o["slug"]: o for o in referentiel.objectifs()})


@app.get("/scrutins/{uid}", response_class=HTMLResponse)
def page_scrutin(request: Request, uid: str, merci: int = 0):
    with db.session() as conn:
        s = conn.execute("SELECT * FROM scrutins WHERE uid = ?", (uid,)).fetchone()
        if not s:
            raise HTTPException(404, "Scrutin inconnu")
        analyse = conn.execute("SELECT * FROM analyses WHERE scrutin_uid = ?", (uid,)).fetchone()
        classements = conn.execute(
            "SELECT * FROM classements WHERE scrutin_uid = ? ORDER BY objectif_slug", (uid,)
        ).fetchall()
        votes = conn.execute(
            "SELECT * FROM votes_groupes WHERE scrutin_uid = ? ORDER BY membres DESC", (uid,)
        ).fetchall()
        historique = conn.execute(
            "SELECT * FROM historique_classements WHERE scrutin_uid = ? ORDER BY id DESC", (uid,)
        ).fetchall()
    return render(request, "scrutin.html", s=s, analyse=analyse, classements=classements,
                  votes=votes, historique=historique, groupes=_groupes_par_uid(),
                  objectifs={o["slug"]: o for o in referentiel.objectifs()}, merci=merci)


@app.post("/scrutins/{uid}/signaler")
def signaler(uid: str, message: str = Form(...), objectif: str = Form(""),
             site_web: str = Form("")):
    if site_web:  # champ piège anti-robots
        return RedirectResponse(f"/scrutins/{uid}?merci=1", status_code=303)
    message = message.strip()[:2000]
    if not message:
        raise HTTPException(400, "Message vide")
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM scrutins WHERE uid = ?", (uid,)).fetchone():
            raise HTTPException(404)
        conn.execute(
            "INSERT INTO signalements (scrutin_uid, objectif_slug, message, created_at) "
            "VALUES (?, ?, ?, ?)",
            (uid, objectif or None, message, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    return RedirectResponse(f"/scrutins/{uid}?merci=1", status_code=303)


@app.get("/methode", response_class=HTMLResponse)
def methode(request: Request):
    return render(request, "methode.html", prompt=classify.system_prompt(),
                  schema=json.dumps(classify.output_schema(), ensure_ascii=False, indent=2),
                  prompt_version=classify.PROMPT_VERSION, modele=llm.identifiant(),
                  poids=config.POIDS_NATURE, objectifs=referentiel.objectifs(),
                  groupes=referentiel.groupes_partis())


# ----------------------------------------------------------------------------- API


@app.get("/api/scores")
def api_scores():
    m = scoring.matrice()
    return {
        "objectifs": [
            {
                "slug": l["objectif"]["slug"],
                "libelle": l["objectif"]["libelle"],
                "groupes": {
                    c["groupe"]["abrev"]: {"score": c["valeur"], "n_scrutins": c["n_scrutins"]}
                    for c in l["cellules"]
                },
            }
            for l in m["lignes"]
        ]
    }


@app.get("/api/scrutins/{uid}")
def api_scrutin(uid: str):
    with db.session() as conn:
        s = conn.execute("SELECT * FROM scrutins WHERE uid = ?", (uid,)).fetchone()
        if not s:
            raise HTTPException(404)
        return {
            "scrutin": dict(s),
            "classements": [dict(r) for r in conn.execute(
                "SELECT * FROM classements WHERE scrutin_uid = ?", (uid,))],
            "votes_groupes": [dict(r) for r in conn.execute(
                "SELECT * FROM votes_groupes WHERE scrutin_uid = ?", (uid,))],
        }


@app.get("/healthz")
def healthz():
    with db.session() as conn:
        conn.execute("SELECT 1")
    return {"ok": True, "pipeline_en_cours": pipeline.en_cours()}


# ---------------------------------------------------------------------- back-office

security = HTTPBasic(auto_error=False)


def admin(creds: HTTPBasicCredentials | None = Depends(security)) -> str:
    if not config.ADMIN_PASSWORD:
        raise HTTPException(503, "Back-office désactivé : définir ADMIN_PASSWORD")
    if creds is None:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    ok = secrets.compare_digest(creds.username.encode(), config.ADMIN_USER.encode()) and \
        secrets.compare_digest(creds.password.encode(), config.ADMIN_PASSWORD.encode())
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    return creds.username


@app.get("/admin", response_class=HTMLResponse)
def page_admin(request: Request, user: str = Depends(admin), objectif: str = "",
               statut: str = "auto"):
    with db.session() as conn:
        signalements = conn.execute(
            """SELECT sg.*, s.titre FROM signalements sg JOIN scrutins s ON s.uid = sg.scrutin_uid
               WHERE sg.traite = 0 ORDER BY sg.id DESC"""
        ).fetchall()
        params: list = [statut]
        filtre = ""
        if objectif:
            filtre = "AND c.objectif_slug = ?"
            params.append(objectif)
        # Échantillon à valider : les moins sûrs d'abord.
        a_valider = conn.execute(
            f"""SELECT c.*, s.titre, s.numero, s.date, s.nature FROM classements c
                JOIN scrutins s ON s.uid = c.scrutin_uid
                WHERE c.statut = ? {filtre} ORDER BY c.confiance ASC, s.date DESC LIMIT 50""",
            params,
        ).fetchall()
        runs = conn.execute("SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 10").fetchall()
        erreurs = conn.execute(
            "SELECT statut, COUNT(*) n FROM analyses GROUP BY statut"
        ).fetchall()
    return render(request, "admin.html", signalements=signalements, a_valider=a_valider,
                  runs=runs, erreurs=erreurs, objectifs=referentiel.objectifs(),
                  objectif=objectif, statut=statut, en_cours=pipeline.en_cours(),
                  llm=pipeline.llm_disponible(), fournisseur=config.LLM_PROVIDER,
                  stats=scoring.stats_globales())


@app.post("/admin/classements/{cid}")
def admin_classement(cid: int, action: str = Form(...), retour: str = Form("/admin"),
                     user: str = Depends(admin)):
    with db.session() as conn:
        c = conn.execute("SELECT * FROM classements WHERE id = ?", (cid,)).fetchone()
        if not c:
            raise HTTPException(404)
        avant = dict(c)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if action == "valider":
            conn.execute("UPDATE classements SET statut='valide', valide_par=?, valide_at=? "
                         "WHERE id=?", (user, now, cid))
        elif action == "rejeter":
            conn.execute("UPDATE classements SET statut='rejete', valide_par=?, valide_at=? "
                         "WHERE id=?", (user, now, cid))
        elif action == "inverser":
            conn.execute("UPDATE classements SET polarite=-polarite, statut='valide', "
                         "valide_par=?, valide_at=? WHERE id=?", (user, now, cid))
        elif action == "reouvrir":
            conn.execute("UPDATE classements SET statut='auto', valide_par=NULL, valide_at=NULL "
                         "WHERE id=?", (cid,))
        else:
            raise HTTPException(400, "Action inconnue")
        apres = dict(conn.execute("SELECT * FROM classements WHERE id = ?", (cid,)).fetchone())
        keep = ("polarite", "confiance", "justification", "statut")
        conn.execute(
            """INSERT INTO historique_classements (scrutin_uid, objectif_slug, action, avant, apres,
               auteur, at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (c["scrutin_uid"], c["objectif_slug"], action,
             json.dumps({k: avant[k] for k in keep}, ensure_ascii=False),
             json.dumps({k: apres[k] for k in keep}, ensure_ascii=False), user, now),
        )
    if not retour.startswith("/"):
        retour = "/admin"
    return RedirectResponse(retour, status_code=303)


@app.post("/admin/signalements/{sid}/traiter")
def admin_signalement(sid: int, user: str = Depends(admin)):
    with db.session() as conn:
        conn.execute("UPDATE signalements SET traite = 1 WHERE id = ?", (sid,))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/pipeline")
def admin_pipeline(limit: int = Form(50), user: str = Depends(admin)):
    if not pipeline.en_cours():
        threading.Thread(target=pipeline.run, kwargs={"classify_limit": limit}, daemon=True).start()
    return RedirectResponse("/admin", status_code=303)
