"""Point d'entrée de l'API du chantier C."""

import logging
import socket

import asyncpg
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import InterfaceError, OperationalError

from app import __version__
from app.config import check_production_safety
from fastapi import Request

from app.admin import mount_admin
from app.auth.cookies import write_anon_cookie
from app.services import statiques
from app.routers import (
    auth,
    conversations,
    detection,
    health,
    me,
    moderation,
    public,
    signalements,
    validations,
    votes,
)

# uvicorn ne configure que ses propres journaux : sans cela, les messages INFO de
# l'application (envois d'e-mails notamment) seraient silencieusement perdus.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)

check_production_safety()

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")
statiques.enregistrer_globals(templates)

#: Délai annoncé au client avant de réessayer. Un redémarrage de PostgreSQL prend
#: quelques secondes ; annoncer une minute ferait fuir un visiteur pour rien.
RETRY_AFTER_SECONDS = 15

#: Ce qui signifie « la base ne répond pas », par opposition à « le code est faux ».
#:
#: La liste a été établie en mesurant ce qui remonte réellement, et elle a corrigé une
#: idée fausse : avec le pilote asyncpg, un échec d'ÉTABLISSEMENT de connexion n'est
#: pas enveloppé par SQLAlchemy. Base arrêtée -> `ConnectionRefusedError` brute ;
#: base inconnue ou identifiants refusés -> exception `asyncpg` brute. Un gestionnaire
#: posé sur le seul `DBAPIError` aurait donc laissé passer en 500 le cas le plus
#: courant — l'arrêt de la base.
#:
#: À l'inverse, `IntegrityError`, `ProgrammingError` et `DataError` sont volontairement
#: ABSENTS : ce sont des erreurs de code (contrainte violée, SQL faux), elles doivent
#: rester des 500 et non se déguiser en panne passagère.
DATABASE_OUTAGE = (
    # Connexion perdue en cours de requête, verrou, serveur saturé.
    OperationalError,
    # Connexion déjà refermée sous les pieds de la requête.
    InterfaceError,
    # Refus côté serveur avant tout enveloppement : base absente, mot de passe
    # refusé, « the database system is starting up ».
    asyncpg.PostgresError,
    # Port fermé ou connexion coupée : le cas d'un `docker compose stop db`.
    # L'envoi d'e-mails ne passe pas par là — aiosmtplib lève ses propres
    # exceptions SMTP, qui ne sont pas des ConnectionError.
    ConnectionError,
    # Nom d'hôte irrésolu : le réseau Docker a disparu sous l'application.
    socket.gaierror,
)

app = FastAPI(
    title="Chantier C — API de vote",
    version=__version__,
    description=(
        "Backend du système de vote sur-mesure : comptes participants, votes, "
        "niveaux et badges, analyse red-dwarf."
    ),
)

@app.middleware("http")
async def set_anon_cookie(request: Request, call_next):
    """Écrit le cookie du participant anonyme sur la réponse réellement émise.

    Nécessaire parce que FastAPI ne recopie les en-têtes posés par une dépendance
    que lorsqu'il construit lui-même la réponse. Une route qui renvoie directement
    une `Response` — c'est le cas de toutes les pages HTML — perdrait le cookie, et
    chaque visite créerait un participant orphelin de plus.
    """
    response = await call_next(request)
    token = getattr(request.state, "anon_cookie", None)
    if token:
        write_anon_cookie(response, token)
    return response


async def database_unavailable(request: Request, exc: Exception):
    """Une base injoignable est une indisponibilité (503), pas un bug (500).

    Sans ce gestionnaire, une coupure de base remontait en 500 brut : indistinguable
    d'une erreur applicative pour qui lit les journaux, et illisible pour le visiteur.
    Le 503 dit la vérité — le service reviendra — et `Retry-After` le dit aussi aux
    robots et aux sondes.

    **Aucune reprise automatique.** Rejouer une requête interrompue dupliquerait des
    effets de bord déjà produits : un e-mail parti, une proposition enregistrée, un
    badge décerné. Le cas courant — une connexion coupée entre deux requêtes — est
    déjà couvert par `pool_pre_ping` (app/db.py), qui teste la connexion avant de la
    prêter. Ce qui reste ici, c'est la vraie coupure : elle se signale, elle ne se
    contourne pas.
    """
    logger.error(
        "base indisponible sur %s %s : %s: %s",
        request.method,
        request.url.path,
        type(exc).__name__,
        exc,
    )
    headers = {"Retry-After": str(RETRY_AFTER_SECONDS)}
    message = (
        "Le service est momentanément indisponible : la base de données ne répond "
        "pas. Réessayez dans quelques instants — rien de ce que vous avez déjà "
        "enregistré n'est perdu."
    )
    # Les appels de l'interface de vote attendent du JSON ; leur renvoyer une page
    # HTML les ferait échouer au parsage, donc afficher un message générique au lieu
    # de celui-ci.
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=503, headers=headers)
    return templates.TemplateResponse(
        request,
        "public/indisponible.html",
        {"message": message},
        status_code=503,
        headers=headers,
    )


for _classe in DATABASE_OUTAGE:
    app.add_exception_handler(_classe, database_unavailable)


# Feuille de style, polices et icônes, servies par l'application.
#
# Volontairement PAS par nginx : le fichier de configuration du serveur web est
# partagé avec la pile Pol.is, et le chantier F n'y touche pas. Le débit en jeu est
# de l'ordre de 80 ko de polices mis en cache par le navigateur — uvicorn s'en
# charge sans qu'on ait à en discuter.
class StatiquesEnCache(StaticFiles):
    """`StaticFiles`, plus l'en-tête `Cache-Control` qui manquait.

    Deux régimes, décidés par la présence de l'empreinte dans l'adresse :

    - **avec `?v=`** (la feuille de style, le script du détecteur, via
      `url_statique`) : un an, `immutable`. C'est sans risque parce que l'adresse
      change dès que le contenu change — voir `app/services/statiques.py`.
    - **sans `?v=`** (polices, icônes, images, y compris celles que `styles.css`
      appelle en `url(...)`) : une heure, puis revalidation sur l'`ETag` déjà
      servi. C'est plus court que ce que faisait le navigateur jusqu'ici : sans
      aucun `Cache-Control`, il appliquait une heuristique fondée sur l'âge du
      fichier, qui pouvait garder une image plusieurs jours. Une heure borne le
      délai au lieu de le laisser dépendre de la date du fichier.

    Ces fichiers-là pourraient eux aussi passer par `url_statique` ; ce n'est pas
    fait faute de l'avoir demandé, et l'heure de cache suffit à les rendre
    prévisibles.
    """

    #: Un an. La valeur usuelle pour une adresse portant une empreinte ; au-delà,
    #: les navigateurs plafonnent d'eux-mêmes.
    CACHE_AVEC_EMPREINTE = "public, max-age=31536000, immutable"

    #: Une heure, sans `immutable` : le navigateur revalidera ensuite.
    CACHE_SANS_EMPREINTE = "public, max-age=3600"

    async def get_response(self, path: str, scope) -> Response:
        reponse = await super().get_response(path, scope)
        # Ne rien poser sur un 404 ou un 405 : une erreur ne se met pas en cache.
        if reponse.status_code == 200:
            empreinte_demandee = b"v=" in scope.get("query_string", b"")
            reponse.headers["Cache-Control"] = (
                self.CACHE_AVEC_EMPREINTE
                if empreinte_demandee
                else self.CACHE_SANS_EMPREINTE
            )
        return reponse


app.mount("/static", StatiquesEnCache(directory="app/static"), name="static")


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(me.router)
app.include_router(conversations.router)
app.include_router(detection.router)
app.include_router(votes.router)
app.include_router(signalements.router)
app.include_router(validations.router)
app.include_router(moderation.router)
app.include_router(public.router)

# Admin générique : filet de sécurité réservé aux is_superuser.
mount_admin(app)
