"""Dépendance d'identité participant.

`get_current_participant` est LA porte d'entrée de toute action attribuée à quelqu'un.
L'endpoint de vote de l'étape C3 utilisera cette dépendance telle quelle — c'est
pourquoi elle n'exige ni compte ni vérification :

  - visiteur sans compte      -> un participant anonyme est créé, cookie signé posé ;
  - compte non vérifié        -> le participant du compte ;
  - compte vérifié            -> idem.

Autrement dit, un compte non vérifié peut voter. C'est une décision de cadrage
assumée, couverte par tests/test_participants.py.
"""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.cookies import read_anon_cookie
from app.auth.users import current_user_optional
from app.db import get_session
from app.models import Participant, User
from app.services import participants as participants_service
from app.services import rate_limit


def client_ip(request: Request) -> str | None:
    """Adresse de l'appelant, telle que la voit l'application.

    Derrière le proxy, c'est uvicorn qui reconstruit `request.client` à partir de
    `X-Forwarded-For`, et seulement si l'appel vient d'une source déclarée dans
    `--forwarded-allow-ips` (voir docker-compose.yml). Sans cette option, tous les
    participants partageraient l'adresse du proxy — et donc un unique seau.
    """
    return request.client.host if request.client else None


async def enforce_login_rate_limit(
    request: Request, session: AsyncSession = Depends(get_session)
) -> None:
    """Plafonne les tentatives de connexion des routes d'API (réponse 429).

    Les pages HTML de connexion appellent `rate_limit.login_allowed` directement :
    elles doivent réafficher leur formulaire avec un message, pas renvoyer du JSON.
    """
    if not await rate_limit.login_allowed(session, client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail=(
                "Trop de tentatives de connexion depuis cette adresse. "
                "Réessayez dans une minute."
            ),
            headers={"Retry-After": "60"},
        )


async def get_current_participant(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> Participant:
    if user is not None:
        participant = await participants_service.by_user(session, user)
        if participant is None:
            # Cas d'un compte créé hors du parcours web (CLI superuser, par exemple).
            participant = await participants_service.claim_for_user(
                session, user, read_anon_cookie(request)
            )
        return participant

    token = read_anon_cookie(request)
    if token:
        participant = await participants_service.by_anon_token(session, token)
        if participant is not None:
            return participant

    participant = await participants_service.create_anonymous(session)
    # Le cookie est déposé sur `request.state`, pas sur une `Response` injectée :
    # quand une route renvoie elle-même une Response (toutes les pages HTML),
    # FastAPI ne fusionne PAS les en-têtes posés par les dépendances. Le middleware
    # `set_anon_cookie` de app/main.py l'écrit sur la réponse réellement émise.
    request.state.anon_cookie = participant.anon_token
    return participant
