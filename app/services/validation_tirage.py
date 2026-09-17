"""Le tirage d'une proposition à valider, et l'enregistrement de la réponse (MOD-4).

Tout ce qui touche la base. La règle, la cadence et les deux plafonds sont dans
`app/services/validation.py`, qui ne connaît aucune session — c'est ce qui permet de
relire le dispositif sans lire de SQL, comme pour `signalement.py` / `signalement_file.py`.

**Le tirage porte quatre gardes, et aucune n'est décorative :**

  1. jamais sa propre proposition — on ne se donne pas un avis à soi-même ;
  2. jamais une proposition qu'on a déjà signalée — la contrainte d'unicité
     `(proposition, identité)` du signalement refuserait le second, et la personne
     répondrait « à revoir » dans le vide ;
  3. jamais une proposition qu'on a déjà validée — même raison, côté `validation` ;
  4. jamais une proposition qui porte déjà cinq validations — au-delà, on use
     l'attention des gens sur ce qui est tranché.

**Le tirage reste dans le débat que la personne est en train de voter.** Le cadrage ne
le disait pas, et la raison est celle-ci : « mal formulé / hors sujet » est l'un des dix
motifs, et « hors sujet » ne veut rien dire sans la question du débat. Servir une
proposition venue d'ailleurs demanderait d'afficher son débat, son intitulé et son
cadrage sur une carte qui s'intercale entre deux votes — soit une page, pas une carte.
Ici, la question est déjà à l'écran et la personne vient de la lire dix fois.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Conversation,
    ModerationStatus,
    Participant,
    Signalement,
    Statement,
    Validation,
    VerdictValidation,
)
from app.services import signalement_file
from app.services import validation as regles
from app.services.validation import ValidationInvalide, Verdict


class RienATirer(Exception):
    """Aucune proposition ne peut être proposée à cette personne, maintenant.

    Ce n'est **pas** une erreur : c'est le cas ordinaire d'un petit débat où tout a déjà
    été validé, ou d'une personne qui a atteint son plafond du jour. L'appelant sert la
    proposition à voter suivante et ne dit rien — annoncer « plus rien à valider »
    apprendrait à qui le lit combien de validations porte le débat, donc où frapper.
    """


async def _sollicitations_du_jour(
    session: AsyncSession, participant_id: int, *, maintenant: datetime
) -> int:
    """Combien de validations cette identité a rendues sur la fenêtre glissante.

    Glissante et non calée sur minuit : un plafond qui se remet à zéro à une heure
    connue se contourne en attendant cette heure-là.
    """
    depuis = maintenant - timedelta(hours=regles.FENETRE_HEURES)
    return (
        await session.scalar(
            select(func.count())
            .select_from(Validation)
            .where(
                Validation.participant_id == participant_id,
                Validation.cree_le >= depuis,
            )
        )
        or 0
    )


async def a_proposer(
    session: AsyncSession,
    conversation: Conversation,
    participant: Participant,
    *,
    sauf_id: int | None = None,
    maintenant: datetime | None = None,
) -> Statement | None:
    """La proposition à faire valider, ou `None`. **Aucune écriture.**

    `sauf_id` est la proposition que la personne vient de voter : on ne lui demande pas
    de juger le texte qu'elle vient de trancher. Ce serait lui faire relire sa propre
    décision, et le « conforme » qui suivrait un « d'accord » ne dirait plus rien.
    """
    maintenant = maintenant or datetime.now(timezone.utc)

    if (
        await _sollicitations_du_jour(session, participant.id, maintenant=maintenant)
        >= regles.SOLLICITATIONS_PAR_JOUR
    ):
        return None

    deja_signalees = select(Signalement.statement_id).where(
        Signalement.participant_id == participant.id
    )
    deja_validees = select(Validation.statement_id).where(
        Validation.participant_id == participant.id
    )
    # Les propositions rassasiées. Un `GROUP BY ... HAVING` plutôt qu'un sous-select
    # corrélé : une seule passe sur `ix_validation_proposition`, et le plan ne dépend
    # pas du nombre de candidates.
    rassasiees = (
        select(Validation.statement_id)
        .group_by(Validation.statement_id)
        .having(func.count() >= regles.VALIDATIONS_PAR_PROPOSITION)
    )

    candidates = select(Statement).where(
        Statement.conversation_id == conversation.id,
        Statement.moderation_status == ModerationStatus.approved,
        # **Le piège du NULL, et il n'est pas théorique.** Écrire simplement
        # `author_participant_id != participant.id` écarterait toutes les propositions
        # d'amorce : en SQL, `NULL <> 3` vaut NULL, donc faux, donc la ligne ne passe
        # pas. Les amorces sont écrites par la modération, elles ne sont donc la
        # proposition de personne — rien ne justifie de les soustraire au regard des
        # participants, et sur un débat jeune elles sont l'essentiel de ce qu'il y a
        # à lire.
        or_(
            Statement.author_participant_id.is_(None),
            Statement.author_participant_id != participant.id,
        ),
        Statement.id.notin_(deja_signalees),
        Statement.id.notin_(deja_validees),
        Statement.id.notin_(rassasiees),
    )
    if sauf_id is not None:
        candidates = candidates.where(Statement.id != sauf_id)

    # **Tiré au sort, uniformément.** Pas de pondération, contrairement au tirage des
    # propositions à voter (`services/votes.py`), et c'est délibéré : pondérer par la
    # priorité ferait valider en premier ce qui clive le plus, c'est-à-dire exactement
    # ce dont un jugement de conformité doit être indépendant. Le corpus d'un débat se
    # compte en dizaines de lignes ; `ORDER BY random()` y coûte moins qu'une requête
    # de comptage suivie d'un décalage.
    return await session.scalar(candidates.order_by(func.random()).limit(1))


async def enregistrer(
    session: AsyncSession,
    *,
    statement: Statement,
    participant: Participant,
    verdict: Verdict,
    motifs: list[str] | None = None,
    texte_libre: str | None = None,
) -> tuple[Validation, bool]:
    """Enregistre la réponse. Renvoie (validation, nouvelle).

    `nouvelle` vaut faux quand cette identité avait déjà validé cette proposition : c'est
    alors un **succès idempotent**, comme au dépôt d'un signalement, et pour la même
    raison — un double clic n'est pas une faute, et le répondant reçoit de toute façon la
    même phrase.

    **Un « à revoir » dépose un signalement ordinaire**, qui suit le chemin habituel : la
    route est celle des motifs cochés, et une ligne rouge déclenche le retrait
    conservatoire dans la même transaction. Le seul écart tient en un mot : il est marqué
    `sollicite`, parce que le site l'a provoqué.

    **L'ordre est le bon, et il compte.** La validation est écrite d'abord : si le dépôt
    du signalement échoue sur une règle (motif inconnu, texte libre manquant), rien n'est
    enregistré du tout — l'exception remonte avant le commit. L'inverse laisserait des
    signalements sans la validation qui les explique.
    """
    if regles.exige_des_motifs(verdict):
        if not motifs:
            raise ValidationInvalide(
                "« Proposition à revoir » demande d'indiquer ce qui ne va pas."
            )
    elif motifs:
        raise ValidationInvalide(
            "« Proposition conforme » ne s'accompagne d'aucun motif."
        )

    if statement.moderation_status is not ModerationStatus.approved:
        # La proposition a été retirée entre le moment où la carte a été servie et celui
        # où la personne a répondu. Rien à enregistrer : le sondage porte sur ce qui est
        # en circulation, et une validation sur un texte déjà sorti fausserait les taux.
        raise signalement_file.PropositionNonSignalable(
            "Cette proposition n'est plus en circulation."
        )

    insertion = (
        pg_insert(Validation)
        .values(
            statement_id=statement.id,
            participant_id=participant.id,
            verdict=verdict.value,
        )
        .on_conflict_do_nothing(constraint="uq_validation_identite")
        .returning(Validation.id)
    )
    nouvel_id = await session.scalar(insertion)
    if nouvel_id is None:
        # La course a été perdue, ou la personne avait déjà répondu. Succès idempotent :
        # on ne dépose surtout pas un second signalement par ce chemin.
        await session.commit()
        ligne = await session.scalar(
            select(Validation).where(
                Validation.statement_id == statement.id,
                Validation.participant_id == participant.id,
            )
        )
        return ligne, False

    if verdict is Verdict.a_revoir:
        await signalement_file.deposer(
            session,
            statement=statement,
            participant=participant,
            motifs=motifs,
            texte_libre=texte_libre,
            sollicite=True,
            # Ni référent ni adresse : voir la note de la classe `Validation`. Un
            # signalement sollicité ne peut pas être coordonné, et le MOD-5 l'écarte
            # de toute façon — deux empreintes de plus seraient deux données
            # personnelles collectées sans usage, et à purger.
        )
        # `deposer` a déjà validé, écrit, retiré le cas échéant et committé.
        return await session.get(Validation, nouvel_id), True

    await session.commit()
    return await session.get(Validation, nouvel_id), True


# --- ce que la validation a produit, pour le rapport du responsable ---------------


async def mesure(session: AsyncSession) -> dict:
    """Les chiffres de `rapport-validations`. **Lecture seule.**

    Il y a peu à compter, et c'est voulu : ce lot n'a pas d'écran de pilotage. Ce qu'on
    veut savoir tient en quatre questions — combien de gens ont répondu, ce qu'ils ont
    répondu, combien de propositions ont été regardées, et ce que les « à revoir » ont
    déclenché.
    """
    total = await session.scalar(select(func.count()).select_from(Validation)) or 0

    par_verdict_lignes = await session.execute(
        select(Validation.verdict, func.count()).group_by(Validation.verdict)
    )
    par_verdict = {verdict: combien for verdict, combien in par_verdict_lignes}

    repondants = (
        await session.scalar(
            select(func.count(func.distinct(Validation.participant_id)))
        )
        or 0
    )
    propositions_vues = (
        await session.scalar(select(func.count(func.distinct(Validation.statement_id))))
        or 0
    )

    # Ce que les « à revoir » ont déclenché, côté signalement. Le décompte par route dit
    # si le sondage remonte des lignes rouges ou des questions de forme — ce n'est pas la
    # même chose à lire, ni le même dispositif à défendre.
    par_route_lignes = await session.execute(
        select(Signalement.route, func.count())
        .where(Signalement.sollicite.is_(True))
        .group_by(Signalement.route)
    )
    par_route = {route: combien for route, combien in par_route_lignes}

    spontanes = (
        await session.scalar(
            select(func.count())
            .select_from(Signalement)
            .where(Signalement.sollicite.is_(False))
        )
        or 0
    )

    return {
        "total": total,
        "conformes": par_verdict.get(VerdictValidation.conforme, 0),
        "a_revoir": par_verdict.get(VerdictValidation.a_revoir, 0),
        "repondants": repondants,
        "propositions_vues": propositions_vues,
        "signalements_sollicites_par_route": par_route,
        "signalements_spontanes": spontanes,
    }
