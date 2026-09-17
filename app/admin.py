"""Admin générique (`sqladmin`) monté sur /admin.

C'est le filet de sécurité : CRUD auto-généré sur toutes les tables, réservé aux
comptes `is_superuser`. Les écrans réellement utilisés au quotidien — éditeur de
conversation et file de modération — sont sur-mesure dans app/routers/moderation.py.

**Chaque vue déclare `form_columns`.** Sans cette liste, sqladmin construit son
formulaire d'édition à partir de TOUTES les colonnes du modèle : `is_meta` (qui
multiplie par ~10 la priorité de routage d'une proposition), `is_seed`, `slug`,
`proposed_by_participant_id` ou `hashed_password` devenaient modifiables d'un clic,
sans intitulé explicite et sans qu'aucun écran ne l'ait demandé. La règle est donc :
on n'expose ici que ce qu'un modérateur peut légitimement corriger à la main.
"""

from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth.service import authenticate, issue_session_cookie, user_from_cookie
from app.auth.users import SESSION_COOKIE_NAME
from app.config import settings
from app.db import get_engine, get_sessionmaker
from app.models import Conversation, Participant, Statement, User

LOGIN_PATH = "/moderation/login"


class SuperuserOnly(AuthenticationBackend):
    """Réutilise le même cookie de session que le reste du site.

    Se connecter à /moderation ouvre donc aussi /admin, et réciproquement : un seul
    identifiant, un seul cookie, pas de session parallèle à maintenir.
    """

    async def login(self, request: Request) -> bool | RedirectResponse:
        form = await request.form()
        async with get_sessionmaker()() as session:
            user = await authenticate(
                session, str(form.get("username", "")), str(form.get("password", ""))
            )
            if user is None or not user.is_superuser:
                return False
            response = RedirectResponse(request.url_for("admin:index"), status_code=302)
            await issue_session_cookie(response, user)
            return response

    async def logout(self, request: Request) -> RedirectResponse:
        response = RedirectResponse(LOGIN_PATH, status_code=302)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    async def authenticate(self, request: Request) -> bool | RedirectResponse:
        async with get_sessionmaker()() as session:
            user = await user_from_cookie(session, request.cookies)
        if user is not None and user.is_superuser:
            return True
        return RedirectResponse(LOGIN_PATH, status_code=302)


class UserAdmin(ModelView, model=User):
    name = "Compte"
    name_plural = "Comptes"
    column_list = [User.email, User.is_active, User.is_verified, User.is_superuser]
    column_searchable_list = [User.email]
    # `is_active` est le levier d'éjection d'un compte compromis ou abusif : le
    # décocher coupe l'accès immédiatement, sans redémarrage. `hashed_password` est
    # hors de portée — un hachage saisi à la main rendrait le compte inutilisable.
    form_columns = [User.email, User.is_active, User.is_verified, User.is_superuser]


class ParticipantAdmin(ModelView, model=Participant):
    name = "Participant"
    name_plural = "Participants"
    column_list = [Participant.id, Participant.user_id, Participant.created_at]
    # Un participant n'a rien à éditer : `anon_token` est un secret de session et
    # `user_id` est le rattachement au compte, posé par la fusion. La consultation
    # (et la suppression, qui reste un geste explicite) suffit.
    can_edit = False


class ConversationAdmin(ModelView, model=Conversation):
    name = "Conversation"
    name_plural = "Conversations"
    column_list = [
        Conversation.id,
        Conversation.slug,
        Conversation.title,
        Conversation.state,
        Conversation.moderation_mode,
    ]
    column_searchable_list = [Conversation.title, Conversation.slug]
    # `slug` reste hors formulaire : c'est l'URL publique, la changer casse les liens
    # déjà partagés. `proposed_by_participant_id` et `owner_user_id` sont de la
    # traçabilité, pas des réglages.
    form_columns = [
        Conversation.title,
        Conversation.description,
        Conversation.state,
        Conversation.moderation_mode,
        Conversation.moderation_status,
        Conversation.is_public,
        Conversation.allow_participant_statements,
    ]


class StatementAdmin(ModelView, model=Statement):
    name = "Proposition"
    name_plural = "Propositions"
    column_list = [
        Statement.id,
        Statement.conversation_id,
        Statement.text,
        Statement.moderation_status,
        Statement.is_seed,
    ]
    # Corriger une faute et trancher une modération : c'est tout ce qu'on fait à la
    # main sur une proposition. `is_meta` et `is_seed` restent en base et servent au
    # pipeline, mais ne s'attrapent plus par accident — on les exposera délibérément,
    # avec un intitulé clair, le jour où des questions de cadrage seront demandées.
    form_columns = [Statement.text, Statement.moderation_status]


def mount_admin(app) -> Admin:
    admin = Admin(
        app,
        engine=get_engine(),
        title="Chantier C — admin",
        authentication_backend=SuperuserOnly(secret_key=settings.secret_key),
    )
    for view in (UserAdmin, ParticipantAdmin, ConversationAdmin, StatementAdmin):
        admin.add_view(view)
    return admin
