# Chantier C — interface de vote sur-mesure

Backend et interface de vote avec comptes participants, niveaux et badges, analyse
d'opinion par [red-dwarf](https://github.com/polis-community/red-dwarf).

Remplace à terme l'usage fonctionnel de Pol.is. **La pile Pol.is (`~/polis`) continue de
tourner intacte** : ce projet est strictement séparé (projet Docker Compose, réseau,
base et volumes distincts) et ne la modifie jamais.

## Démarrer

```bash
cp .env.example .env       # puis renseigner POSTGRES_PASSWORD, SECRET_KEY, SMTP_*
docker compose up -d --build
docker compose exec api alembic upgrade head
```

L'API n'écoute que sur `127.0.0.1:8001` — rien n'est exposé publiquement tant que
l'étape C8 (domaine + TLS + nginx) n'a pas été validée. Pour y accéder depuis un poste :

```bash
ssh -L 8001:127.0.0.1:8001 <vps>
curl http://127.0.0.1:8001/health
```

## Commandes

```bash
docker compose exec api pytest                    # tests
docker compose exec api alembic upgrade head      # migrations
docker compose exec api alembic revision --autogenerate -m "…"
docker compose logs -f api                        # journaux
psql postgresql://chantierc:<mdp>@localhost:5433/chantierc
```

## Structure

```
app/config.py      configuration (env / .env)
app/db.py          moteur async, sessions, base déclarative
app/models/        modèles SQLAlchemy (C1 : comptes, C2 : conversations)
app/routers/       routes FastAPI
migrations/        Alembic
tests/             pytest
```

## Journal

`CHANTIER_C_LOG.md` — une entrée par étape : ce qui est fait, ce qui est prouvé, ce qui
ne l'est pas. Distinct du `DEPLOY_LOG.md` de Pol.is, puisque c'est un autre système.
