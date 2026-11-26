"""Ligne de commande : python -m app.cli <commande>."""

from __future__ import annotations

import argparse
import json
import logging

from app import db


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Crée le schéma et synchronise les objectifs")

    pi = sub.add_parser("ingest", help="Télécharge et charge les données AN")
    pi.add_argument("--force", action="store_true", help="Retélécharge même si inchangé")

    pc = sub.add_parser("classify", help="Classe les scrutins non encore analysés (LLM)")
    pc.add_argument("--limit", type=int, default=None)
    pc.add_argument("--natures", default=None, help="ex. ensemble,motion,article")
    pc.add_argument("--dry-run", action="store_true", help="Liste sans appeler le LLM")

    pp = sub.add_parser("pipeline", help="Ingestion puis classement")
    pp.add_argument("--force", action="store_true")
    pp.add_argument("--limit", type=int, default=None)

    sub.add_parser("scores", help="Affiche la matrice des scores")
    sub.add_parser("prompt", help="Affiche le prompt système du classement")

    args = p.parse_args(argv)
    db.init_db()

    if args.cmd == "init-db":
        print("Base initialisée")
    elif args.cmd == "ingest":
        from app.ingest.an import ingest

        print(json.dumps(ingest(force=args.force), ensure_ascii=False, indent=2))
    elif args.cmd == "classify":
        from app import classify

        natures = args.natures.split(",") if args.natures else None
        if args.dry_run:
            todo = classify.a_classer(args.limit or 10**9, natures)
            from collections import Counter

            print(f"{len(todo)} scrutins à classer :", dict(Counter(s["nature"] for s in todo)))
            for s in todo[:5]:
                print("---\n" + classify.user_message(s))
        else:
            print(json.dumps(classify.classify(args.limit, natures), indent=2))
    elif args.cmd == "pipeline":
        from app import pipeline

        print(json.dumps(pipeline.run(args.force, args.limit), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "scores":
        from app import scoring

        m = scoring.matrice()
        print("objectif".ljust(16) + "".join(g["abrev"][:7].rjust(8) for g in m["groupes"]))
        for ligne in m["lignes"]:
            vals = "".join(
                (f"{c['valeur']:.0f}" if c["valeur"] is not None else "-").rjust(8)
                for c in ligne["cellules"]
            )
            print(ligne["objectif"]["slug"].ljust(16) + vals)
    elif args.cmd == "prompt":
        from app import classify

        print(classify.system_prompt())


if __name__ == "__main__":
    main()
