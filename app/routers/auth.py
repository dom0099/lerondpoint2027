"""Routes de comptes, fournies par fastapi-users.

  POST /auth/login              connexion (pose le cookie de session)
  POST /auth/logout             déconnexion
  POST /auth/register           inscription (déclenche l'e-mail de vérification)
  POST /auth/forgot-password    demande de réinitialisation
  POST /auth/reset-password     réinitialisation effective
  POST /auth/request-verify-token / POST /auth/verify
  GET|PATCH /users/me
"""

from fastapi import APIRouter, Depends

from app.auth.deps import enforce_login_rate_limit
from app.auth.users import auth_backend, fastapi_users
from app.schemas import UserCreate, UserRead, UserUpdate

router = APIRouter()

# Le plafond est posé sur le routeur entier plutôt que sur la seule route de
# connexion : `get_auth_router` fournit aussi `/auth/logout`, et une dépendance de
# routeur reste vraie si fastapi-users en ajoute une autre demain. Le coût d'un
# `logout` compté dans le seau est nul.
router.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix="/auth",
    tags=["auth"],
    dependencies=[Depends(enforce_login_rate_limit)],
)
router.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix="/auth",
    tags=["auth"],
)
router.include_router(
    fastapi_users.get_reset_password_router(), prefix="/auth", tags=["auth"]
)
router.include_router(
    fastapi_users.get_verify_router(UserRead), prefix="/auth", tags=["auth"]
)
router.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate), prefix="/users", tags=["users"]
)
