"""Câblage fastapi-users : gestionnaire de comptes, backend cookie/JWT, dépendances.

DÉCISION DE CADRAGE — la vérification d'e-mail n'est PAS contraignante en v1.
L'e-mail de vérification est bien envoyé et `is_verified` est peuplé dès le premier
jour, mais aucune route ne l'exige : les comptes sont optionnels à ce stade, et
imposer « consultez votre boîte » avant le premier vote réintroduirait exactement la
friction que le caractère optionnel cherche à éviter. Passer à l'obligatoire plus tard
se fera en basculant REQUIRE_VERIFICATION, sans rattrapage de données.
"""

import hashlib
import uuid
from collections.abc import AsyncIterator

import jwt
from fastapi import Depends, Request
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin
from fastapi_users.authentication import (
    AuthenticationBackend,
    CookieTransport,
    JWTStrategy,
)
from fastapi_users.exceptions import UserAlreadyExists, UserAlreadyVerified
from fastapi_users.jwt import decode_jwt, generate_jwt
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import email
from app.config import settings
from app.db import get_session
from app.models import User
from app.services import participants as participants_service

#: Bascule unique de la contrainte de vérification. Voir la note d'en-tête.
#: Un test de non-régression vérifie que ce drapeau vaut bien False (tests/test_accounts.py).
REQUIRE_VERIFICATION = False

ANON_COOKIE_NAME = "cpt_anon"
SESSION_COOKIE_NAME = "cpt_session"
SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 30


async def get_user_db(
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[SQLAlchemyUserDatabase]:
    yield SQLAlchemyUserDatabase(session, User)


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = settings.secret_key
    verification_token_secret = settings.secret_key

    async def create(self, *args, **kwargs) -> User:
        """Traduit la course d'inscription en refus explicite.

        fastapi-users lit l'adresse puis crée le compte : deux requêtes
        simultanées passent toutes deux la lecture, et la seconde violait
        l'index unique — remontée en HTTP 500 alors que le compte existait bien.
        """
        try:
            return await super().create(*args, **kwargs)
        except IntegrityError as exc:
            await self.user_db.session.rollback()
            raise UserAlreadyExists from exc

    async def on_after_register(self, user: User, request: Request | None = None) -> None:
        """Rattache le participant anonyme courant, puis envoie la vérification.

        L'ordre compte : le rattachement ne doit pas dépendre de la réussite de
        l'envoi d'e-mail.
        """
        anon_token = _read_anon_token(request)
        await participants_service.claim_for_user(
            self.user_db.session, user, anon_token
        )
        try:
            await self.request_verify(user, request)
        except UserAlreadyVerified:
            pass

    async def on_after_login(self, user, request=None, response=None) -> None:
        """Rattachement (ou fusion) à la connexion.

        Quelqu'un peut voter en anonyme puis se connecter à un compte existant :
        `claim_for_user` verse alors ses votes dans ceux du compte. Le cookie
        anonyme est ensuite retiré, son jeton étant devenu caduc.
        """
        anon_token = _read_anon_token(request)
        await participants_service.claim_for_user(
            self.user_db.session, user, anon_token
        )
        if response is not None:
            from app.auth.cookies import clear_anon_cookie

            clear_anon_cookie(response)

    async def on_after_forgot_password(
        self, user: User, token: str, request: Request | None = None
    ) -> None:
        link = f"{settings.public_base_url}/compte/reinitialisation?token={token}"
        await email.send_email(
            to=user.email,
            subject="Réinitialisation de votre mot de passe",
            body=(
                "Bonjour,\n\n"
                "Vous avez demandé à réinitialiser votre mot de passe. "
                "Suivez ce lien :\n\n"
                f"{link}\n\n"
                "Si vous n'êtes pas à l'origine de cette demande, ignorez ce message : "
                "votre mot de passe reste inchangé.\n\n"
                "Le Rond-Point 2027\n"
            ),
        )

    async def on_after_request_verify(
        self, user: User, token: str, request: Request | None = None
    ) -> None:
        link = f"{settings.public_base_url}/compte/verification?token={token}"
        await email.send_email(
            to=user.email,
            subject="Confirmez votre adresse e-mail",
            body=(
                "Bienvenue,\n\n"
                "Confirmez votre adresse e-mail en suivant ce lien :\n\n"
                f"{link}\n\n"
                "Ce n'est pas obligatoire pour participer — vous pouvez voter dès "
                "maintenant. La confirmation nous permet de vous réinitialiser votre "
                "mot de passe en cas d'oubli.\n\n"
                "Le Rond-Point 2027\n"
            ),
        )


def _read_anon_token(request: Request | None) -> str | None:
    """Lit le jeton anonyme du cookie signé, sans faire confiance à son contenu."""
    if request is None:
        return None
    from app.auth.cookies import read_anon_cookie

    return read_anon_cookie(request)


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncIterator[UserManager]:
    yield UserManager(user_db)


cookie_transport = CookieTransport(
    cookie_name=SESSION_COOKIE_NAME,
    cookie_max_age=SESSION_LIFETIME_SECONDS,
    # Passera à True à l'étape C8, quand le site sera servi en HTTPS.
    cookie_secure=settings.environment != "dev",
    cookie_httponly=True,
    cookie_samesite="lax",
)


#: Nom de la revendication portant l'empreinte du mot de passe dans le jeton.
PASSWORD_CLAIM = "pwh"


def password_fingerprint(hashed_password: str) -> str:
    """Empreinte courte et non réversible du hachage du mot de passe.

    C'est le hachage qui est empreinté, jamais le mot de passe : le jeton ne
    contient donc rien qui n'existe déjà en base, et huit octets suffisent — il
    s'agit de détecter un changement, pas de résister à une recherche de collision
    choisie, puisque l'attaquant qui contrôlerait le contenu du jeton devrait
    d'abord connaître la clé de signature.
    """
    return hashlib.blake2s(hashed_password.encode(), digest_size=8).hexdigest()


class SessionStrategy(JWTStrategy):
    """JWT de session portant l'empreinte du mot de passe du compte.

    Sans elle, un JWT ne porte que `sub`, `aud` et `exp` : il reste donc valable
    jusqu'à son expiration, et **changer son mot de passe n'éjectait pas la session
    d'un attaquant** déjà connecté. Avec elle, la comparaison faite à chaque lecture
    fait tomber toutes les sessions dès que le hachage change — la victime reprend la
    main en changeant son mot de passe, y compris sur ses propres sessions ouvertes
    ailleurs, ce qui est le comportement attendu d'une reprise en main.

    C'est la même mécanique que fastapi-users applique déjà à ses jetons de
    réinitialisation, appliquée ici au jeton de session : aucune colonne, aucune
    migration, rien à purger.

    Deux conséquences assumées :
      - les sessions ouvertes avant la mise en service n'ont pas la revendication et
        tombent une fois, à la première requête ;
      - un ré-hachage opportuniste (`verify_and_update`, lors d'un changement de
        paramètres d'argon2) invalide de même les sessions du compte concerné. Le cas
        est rare et se répare tout seul à la connexion suivante.

    L'autre levier reste la désactivation du compte (`is_active` depuis /admin), qui
    coupe l'accès sans rien demander à la personne visée.
    """

    async def write_token(self, user) -> str:
        data = {
            "sub": str(user.id),
            "aud": self.token_audience,
            PASSWORD_CLAIM: password_fingerprint(user.hashed_password),
        }
        return generate_jwt(
            data, self.encode_key, self.lifetime_seconds, algorithm=self.algorithm
        )

    async def read_token(self, token, user_manager):
        # La lecture standard fait le gros du travail (signature, audience,
        # expiration, compte actif). On ne rajoute que la comparaison d'empreinte —
        # le second décodage porte sur un jeton déjà vérifié, il ne coûte qu'un HMAC.
        user = await super().read_token(token, user_manager)
        if user is None:
            return None
        try:
            data = decode_jwt(
                token, self.decode_key, self.token_audience, algorithms=[self.algorithm]
            )
        except jwt.PyJWTError:  # pragma: no cover — déjà décodé juste au-dessus
            return None
        if data.get(PASSWORD_CLAIM) != password_fingerprint(user.hashed_password):
            return None
        return user


def get_jwt_strategy() -> SessionStrategy:
    return SessionStrategy(
        secret=settings.secret_key, lifetime_seconds=SESSION_LIFETIME_SECONDS
    )


auth_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

current_user = fastapi_users.current_user(
    active=True, verified=REQUIRE_VERIFICATION
)
current_user_optional = fastapi_users.current_user(
    active=True, verified=REQUIRE_VERIFICATION, optional=True
)
current_superuser = fastapi_users.current_user(active=True, superuser=True)
