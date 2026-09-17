"""Création et promotion de comptes hors du parcours web.

Séparé de app/cli.py pour être testable sans passer par la base de développement :
la CLI n'est qu'une enveloppe autour de `ensure_superuser`.
"""

from fastapi_users.exceptions import UserAlreadyVerified
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import user_manager_for
from app.models import User
from app.schemas import UserCreate
from app.services import participants as participants_service


async def ensure_superuser(
    session: AsyncSession,
    email: str,
    password: str,
    display_name: str | None = None,
    reset_password: bool = False,
) -> tuple[User, str]:
    """Crée un modérateur, promeut ou réinitialise le compte existant.

    Renvoie (compte, action) où action vaut "created", "promoted",
    "password_reset" ou "unchanged" — afin que l'appelant puisse le dire à
    l'opérateur sans redéduire l'état.

    `reset_password` existe pour un cas précis : un mot de passe qui a fuité doit
    pouvoir être changé sans supprimer le compte, ce qui emporterait son
    participant et donc son historique de votes.
    """
    manager = user_manager_for(session)
    existing = await manager.user_db.get_by_email(email)

    if existing is not None:
        actions = []
        if reset_password:
            existing.hashed_password = manager.password_helper.hash(password)
            actions.append("password_reset")
        if not existing.is_superuser:
            existing.is_superuser = True
            actions.append("promoted")
        if not actions:
            return existing, "unchanged"
        await session.commit()
        await session.refresh(existing)
        # « mot de passe réinitialisé » prime : c'est l'information la plus
        # importante à afficher à l'opérateur.
        return existing, actions[0]

    try:
        user = await manager.create(
            UserCreate(
                email=email,
                password=password,
                is_superuser=True,
                # Créé en local par un administrateur : il n'y a rien à confirmer.
                is_verified=True,
                display_name=display_name,
            ),
            safe=False,
        )
    except UserAlreadyVerified:  # pragma: no cover - filet
        user = await manager.user_db.get_by_email(email)
    return user, "created"


#: Ce que la suppression emporte, et ce qu'elle laisse — la liste vaut contrat, elle
#: est reprise mot pour mot sur la page de confirmation et dans la politique de
#: confidentialité. Elle découle des `ondelete` déclarés dans app/models/ :
#:
#:   emporté   : le compte (adresse e-mail, nom, hachage du mot de passe), le
#:               participant rattaché, TOUS ses votes, ses projections d'analyse,
#:               ses badges et ses points.
#:   conservé  : les propositions et conversations publiées, **détachées de leur
#:               auteur** (`author_participant_id` et `proposed_by_participant_id`
#:               passent à NULL).
#:
#: Pourquoi les textes publiés restent : d'autres personnes ont voté dessus. Les
#: retirer réécrirait la consultation de tout le monde ; les détacher suffit à ce que
#: plus rien ne les relie à quelqu'un.


async def delete_account(session: AsyncSession, user: User) -> None:
    """Efface le compte et tout ce qui identifie la personne.

    **Le participant est supprimé explicitement, avant le compte.** Supprimer le seul
    `user` ne suffit pas : l'ORM voit la relation `User.participant`, et sa réaction
    par défaut est de mettre `participant.user_id` à NULL — pas de laisser la base
    appliquer son `ON DELETE CASCADE`. Mesuré : le compte disparaissait, mais le
    participant survivait détaché, **avec tous ses votes**. Exactement la promesse que
    cette route est censée tenir.

    Une fois la ligne `participant` supprimée, la base emporte d'elle-même les votes et
    les projections d'analyse (aucune relation ORM ne s'interpose sur celles-là), et la
    suppression du compte emporte les badges.
    """
    participant = await participants_service.by_user(session, user)
    if participant is not None:
        await session.delete(participant)
    await session.delete(user)
    await session.commit()


async def delete_anonymous_participant(session: AsyncSession, participant) -> None:
    """Efface un participant sans compte, identifié par son cookie signé.

    La possession du cookie est la seule preuve d'identité qu'un anonyme puisse
    fournir — mais c'en est une, et elle suffit : sans cette route, le droit à
    l'effacement serait inapplicable pour la majorité des participants, qui n'ont
    pas de compte.
    """
    await session.delete(participant)
    await session.commit()
