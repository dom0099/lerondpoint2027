"""Cookie du participant anonyme.

Le jeton est signé (itsdangerous) : un visiteur ne peut ni forger un jeton, ni
énumérer ceux des autres. La signature est vérifiée à chaque lecture ; un cookie
altéré est traité comme absent.
"""

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

from app.auth.users import ANON_COOKIE_NAME
from app.config import settings

_SALT = "anon-participant"
_MAX_AGE = 60 * 60 * 24 * 365


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.secret_key, salt=_SALT)


def read_anon_cookie(request: Request) -> str | None:
    raw = request.cookies.get(ANON_COOKIE_NAME)
    if not raw:
        return None
    try:
        value = _serializer().loads(raw)
    except BadSignature:
        return None
    return value if isinstance(value, str) else None


def write_anon_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        ANON_COOKIE_NAME,
        _serializer().dumps(token),
        max_age=_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.environment != "dev",
    )


def clear_anon_cookie(response: Response) -> None:
    """Retire le cookie anonyme.

    Appelé à la connexion : le participant anonyme vient soit d'être rattaché au
    compte, soit d'être fusionné puis supprimé. Dans les deux cas le jeton n'a plus
    de sens. Le conserver ferait pointer la déconnexion vers une ligne disparue —
    `get_current_participant` sait retomber sur ses pieds, mais autant ne pas laisser
    traîner un cookie mort.
    """
    response.delete_cookie(ANON_COOKIE_NAME)
