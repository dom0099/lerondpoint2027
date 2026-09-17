"""Authentification hors du flux FastAPI-Users.

Trois consommateurs partagent ce module : la page de connexion du modérateur,
l'admin générique `sqladmin`, et la CLI. Ils ne peuvent pas passer par les
dépendances de routes, mais ne doivent pas pour autant réimplémenter la
vérification de mot de passe.
"""

from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.users import SESSION_COOKIE_NAME, UserManager, get_jwt_strategy
from app.models import User


def user_manager_for(session: AsyncSession) -> UserManager:
    return UserManager(SQLAlchemyUserDatabase(session, User))


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    """Vérifie un couple e-mail / mot de passe. Renvoie None si invalide."""
    manager = user_manager_for(session)
    user = await manager.user_db.get_by_email(email)
    if user is None:
        # Hachage à vide : le temps de réponse ne doit pas révéler si l'adresse existe.
        manager.password_helper.hash(password)
        return None

    verified, updated_hash = manager.password_helper.verify_and_update(
        password, user.hashed_password
    )
    if not verified:
        return None
    if updated_hash is not None:
        user.hashed_password = updated_hash
        await session.commit()
    return user if user.is_active else None


async def user_from_cookie(session: AsyncSession, cookies: dict) -> User | None:
    """Retrouve l'utilisateur porté par le cookie de session, ou None."""
    token = cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return await get_jwt_strategy().read_token(token, user_manager_for(session))


async def issue_session_cookie(response, user: User) -> None:
    """Pose le cookie de session sur une réponse quelconque (redirection incluse)."""
    from app.auth.users import auth_backend

    strategy = get_jwt_strategy()
    login_response = await auth_backend.login(strategy, user)
    for key, value in login_response.raw_headers:
        if key.lower() == b"set-cookie":
            response.raw_headers.append((key, value))


async def establish_session(session, user: User, request, response) -> None:
    """Ouvre une session : rattachement (ou fusion), cookie, nettoyage.

    Point de passage UNIQUE de toute connexion faite hors du routeur fastapi-users.
    Sans lui, la connexion par formulaire poserait bien le cookie mais sauterait le
    rattachement du participant anonyme — donc la fusion des votes de C3.
    """
    from app.auth.cookies import clear_anon_cookie, read_anon_cookie
    from app.services import participants as participants_service

    await participants_service.claim_for_user(
        session, user, read_anon_cookie(request)
    )
    await issue_session_cookie(response, user)
    clear_anon_cookie(response)
