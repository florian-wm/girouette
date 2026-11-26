# Votes & intentions politiques

> Mettre en regard les grands objectifs affichés par les partis (réduire la pauvreté, sauver l'hôpital public, etc.) et ce qu'ils votent réellement à l'Assemblée nationale et au Parlement européen.

## 1. Le besoin

Les partis communiquent sur des objectifs. Leurs votes, eux, sont publics, mais pratiquement illisibles pour le grand public : des milliers de scrutins, souvent sur des amendements techniques, répartis entre deux parlements.

Le projet consiste à faire un site grand public qui :

- présente une liste d'**objectifs politiques** compréhensibles (ex. réduire la pauvreté, sauver l'hôpital public, logement, climat, pouvoir d'achat, etc.) ;
- montre, **par parti / groupe**, comment ses élus ont voté sur les textes et amendements qui touchent à chaque objectif ;
- couvre l'**Assemblée nationale** et la **délégation française au Parlement européen** ;
- est **mis à jour automatiquement** (aucune saisie manuelle des votes) ;
- est **transparent** : chaque score renvoie aux votes qui le composent, au texte voté et à la justification du classement.

## 2. Ce qui existe déjà

| Outil | Périmètre | Ce qu'il fait | Ce qui manque |
|---|---|---|---|
| [Datan.fr](https://datan.fr) | Assemblée nationale | Votes par scrutin, position et cohésion des groupes, profils de députés | Pas de lecture par objectif politique |
| [NosParlementaires.fr](https://nosparlementaires.fr) (suite de NosDéputés.fr) | AN + Sénat | Votes, amendements, présence, dossiers ; open data + API | Pas de lecture par objectif |
| [HowTheyVote.eu](https://howtheyvote.eu) | Parlement européen | Votes par appel nominal, publiés ~30 min après le vote ; API + CSV | Pas de lecture par objectif ; pas de votes à main levée |
| ParlemenTerre (Agir pour l'environnement), Réseau Action Climat / CAN Europe | AN ou PE | Notes des élus sur l'écologie à partir d'une sélection de votes | Mono-thématique, ponctuel (avant élections), sélection manuelle, portés par des ONG engagées |
| Outils de « vote matching » (EUI / euandi, YourVoteMatters…) | PE | Compare les réponses de l'utilisateur aux votes des eurodéputés | Uniquement en période électorale, ~20 votes choisis à la main |

**Conclusion :** les briques existent (données ouvertes, sites de suivi des votes, classements thématiques ponctuels), mais aucun outil grand public ne propose une lecture **par objectif, multi-thèmes, AN + PE, mise à jour en continu**.

## 3. Sources de données

- **Assemblée nationale** — [data.assemblee-nationale.fr](https://data.assemblee-nationale.fr) : scrutins publics, votes nominatifs, amendements (avec exposé sommaire), dossiers législatifs, députés et groupes, en JSON. Environ 8 400 scrutins publics sur la 17e législature.
  - Outil utile : [`@tricoteuses/assemblee`](https://www.npmjs.com/package/@tricoteuses/assemblee) (récupération et nettoyage des données AN).
  - Alternative consolidée : API / dumps de NosParlementaires.fr (Licence Ouverte 2.0).
- **Parlement européen** — [HowTheyVote.eu](https://howtheyvote.eu/developers) : API et exports CSV hebdomadaires ([github.com/HowTheyVote/data](https://github.com/HowTheyVote/data)), avec pays et parti national de chaque eurodéputé → filtrage de la délégation française.

## 4. Solution envisagée

### Pipeline automatisé

1. **Ingestion** : import périodique des scrutins AN et PE, du texte / amendement voté et de son exposé des motifs.
2. **Classement thématique** : un LLM rattache chaque scrutin à 0, 1 ou plusieurs objectifs d'une taxonomie (classement à deux niveaux possible : thème → objectif).
3. **Polarité** : le LLM détermine si voter « pour » fait avancer ou recule l'objectif **tel qu'affiché par le texte lui-même**, et produit une justification courte.
4. **Agrégation** : score par groupe / parti et par objectif, pondéré (vote sur l'ensemble d'un texte > article > amendement).
5. **Contrôle qualité** : validation humaine sur un échantillon, bouton « signaler une erreur », historique des reclassements.

### Stack retenue

Tout tient dans **un seul container Docker** :

- **Python 3.12 / FastAPI** : site public, back-office de validation, API JSON.
- **SQLite** (volume `/data`) pour le référentiel, les votes et les classements. À ce volume (~10 000 scrutins, ~1,5 M de votes nominatifs), MySQL et Elasticsearch ne sont pas nécessaires ; on pourra migrer plus tard si besoin.
- **APScheduler**, dans le même processus, pour le pipeline quotidien (ingestion puis classement).
- **LLM interchangeable** pour le classement thème + polarité : Claude (Anthropic), Mistral ou Gemini (Google), au choix via `LLM_PROVIDER`. Même prompt, même schéma JSON imposé pour les trois (prompt publié sur `/methode`).

### Modèle de données (première ébauche)

- `scrutins` (source AN/PE, date, type : ensemble / article / amendement, intitulé, texte, exposé)
- `votes` (scrutin, élu, position : pour / contre / abstention / absent)
- `elus`, `groupes` (AN / PE), `partis` + table de **correspondance groupe ↔ parti français**
- `objectifs` (taxonomie)
- `classements` (scrutin, objectif, polarité, confiance, justification, modèle, validé_par)

## 5. Difficultés identifiées

- **La polarité est un jugement politique.** Exemple : une baisse de charges réduit-elle la pauvreté ? La réponse dépend du camp. Si l'outil tranche sur les *effets*, il devient lui-même une opinion et sera accusé de partialité.
  → Parti pris : mesurer la **cohérence avec l'objectif affiché du texte** (« cet amendement vise à… »), et/ou la cohérence avec **le programme du parti lui-même**. Méthode publiée, justification visible pour chaque vote.
- **Un vote « contre » n'est pas forcément « contre l'objectif »** : amendement mal rédigé, déjà satisfait, désaccord sur le financement, rejet global d'un budget. → Pondération, distinction ensemble / amendement, contexte affiché.
- **Trous dans les données** : textes adoptés via 49.3 (pas de vote), votes à main levée non nominatifs, votes en commission. Ce sont souvent les textes clés (PLF, PLFSS). → Les signaler explicitement plutôt que les ignorer.
- **Correspondance des partis** entre groupes AN, groupes PE et partis français (et leurs évolutions dans le temps). → Petite table maintenue à la main, versionnée.
- **Neutralité et réputation** : les sites de suivi parlementaire ont déjà été accusés de biais. → Transparence totale de la méthode, des prompts et des données, possibilité de contester un classement.

## 6. MVP proposé

1. Assemblée nationale uniquement, 17e législature.
2. 4–5 objectifs.
3. Scores par **groupe** (pas par député).
4. Méthode publique + justification visible pour chaque scrutin classé.
5. Ajout du Parlement européen (délégation française via HowTheyVote) en étape 2.

## 7. Prochaines étapes

- [x] Script d'ingestion des scrutins AN (open data JSON), avec rattachement du texte des amendements
- [x] Définition de la taxonomie d'objectifs (`config/objectifs.yaml`) et des règles de polarité (`app/classify.py`)
- [ ] Prototype de classement LLM ~~sur un objectif~~ (code prêt) + **évaluation sur un échantillon validé à la main** (back-office prêt, échantillon à faire)
- [x] Table de correspondance groupes ↔ partis (`config/groupes.yaml`)
- [ ] Ingestion HowTheyVote (délégation française)
- [ ] Front public minimal

## 8. Lancer le projet

```bash
cp .env.example .env        # renseigner ANTHROPIC_API_KEY et ADMIN_PASSWORD
docker compose -f docker-compose.yml up -d --build
# Site : http://localhost:8090 · back-office : http://localhost:8090/admin  (port : APP_PORT)
```

> `-f docker-compose.yml` est nécessaire si la variable `COMPOSE_FILE` est définie dans le shell : elle prend le pas sur le fichier local.

Le pipeline tourne tout seul chaque jour (`PIPELINE_CRON`, 6 h 30 par défaut). Pour le lancer à la main :

```bash
docker compose -f docker-compose.yml exec app python -m app.cli ingest                 # télécharge et charge les données AN (~2 min la 1re fois)
docker compose -f docker-compose.yml exec app python -m app.cli classify --dry-run     # ce qui serait envoyé au LLM, sans appel
docker compose -f docker-compose.yml exec app python -m app.cli classify --limit 50    # classe 50 scrutins (ensembles et motions d'abord)
docker compose -f docker-compose.yml exec app python -m app.cli scores                 # matrice objectifs × groupes dans le terminal
docker compose -f docker-compose.yml exec app python -m app.cli prompt                 # prompt système du classement
```

Tests : `docker run --rm -u root -v "$PWD":/src -w /src votes-intentions-politiques sh -c "pip install -q pytest && pytest -q"`

### Organisation du code

| Fichier | Rôle |
|---|---|
| `app/ingest/an.py` | Téléchargement conditionnel (ETag) et parsing des scrutins, groupes et députés. Répare les groupes `PO0` de la source. |
| `app/ingest/amendements.py` | Retrouve le dispositif et l'exposé sommaire d'un amendement voté (~76 % des scrutins d'amendement) |
| `app/llm.py` | Fournisseurs LLM (Anthropic, Mistral, Gemini) derrière une même interface |
| `app/classify.py` | Prompt, appel au LLM, enregistrement. Ne remplace jamais une décision humaine (validé / rejeté). Historique conservé. |
| `app/scoring.py` | Agrégation pondérée → score 0–100 par groupe et par objectif |
| `app/web/` | Pages objectif / groupe / scrutin / méthode, signalements, back-office, API `/api/scores` |
| `config/*.yaml` | Taxonomie des objectifs, correspondance groupes ↔ partis |

### Choisir le fournisseur LLM

Dans `.env` :

```bash
LLM_PROVIDER=mistral          # anthropic | mistral | gemini
MISTRAL_API_KEY=...           # ou ANTHROPIC_API_KEY / GEMINI_API_KEY
LLM_MODEL=                    # vide = modèle par défaut du fournisseur
```

| Fournisseur | Modèle par défaut | Prix au million de tokens (entrée / sortie) |
|---|---|---|
| `anthropic` | `claude-opus-5-5` | 4 $ / 20 $ (Sonnet 5.5 : 2 $ / 10 $ ; Haiku 5.5 : 0,10 $ / 0,50 $) |
| `mistral` | `mistral-large-latest` | Large 4 : 0,70 $ / 2,10 $ (Small 4 : 0,15 $ / 0,60 $) |
| `gemini` | `gemini-3.8-flash` | 0,75 $ / 3,75 $ jusqu'au 31/12/2026, puis 1,50 $ / 7,50 $ |

Prix relevés en novembre 2026 : vérifier sur le site de chaque fournisseur. Mistral n'a pas d'accès API gratuit. Gemini a une offre gratuite limitée, mais les données envoyées peuvent alors servir à améliorer leurs produits.

Le modèle qui a produit chaque classement est enregistré et affiché. **Changer de fournisseur ou de modèle déclenche le reclassement** des scrutins au passage suivant (dans la limite de `CLASSIFY_LIMIT` par exécution). Les validations humaines sont conservées.

### Coût du classement (ordre de grandeur, à mesurer)

Pour l'ensemble des ~10 100 scrutins (~1 360 hors amendements, ~8 750 amendements) :

| Modèle | Corpus complet | Rythme courant (~20–50 scrutins/jour) |
|---|---|---|
| Claude Opus 5.5 | ~240 $ | ~1 $ / jour |
| Claude Sonnet 5.5 | ~120 $ | ~0,50 $ / jour |
| Mistral Large 4 | ~25 $ | quelques centimes |
| Gemini 3.8 Flash | ~40 $ | quelques centimes |

Les modèles qui « réfléchissent » avant de répondre (Claude, Gemini) facturent ce raisonnement comme de la sortie. Seule une mesure sur un échantillon donnera le vrai chiffre. Un scrutin déjà analysé n'est reclassé que si son contenu, le prompt, la taxonomie ou le modèle change.

## 9. Licence

Code sous licence [GNU AGPL-3.0](LICENSE) : toute version modifiée, y compris un site qui la réutilise, doit publier son code source.
Données de l'Assemblée nationale : [Licence Ouverte 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/).
