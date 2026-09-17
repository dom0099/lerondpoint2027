"""La navette de reformulation (MOD-10).

**Ce que la navette fait, en une phrase :** le responsable propose à l'auteur d'une
proposition signalée « mal formulée » une version plus claire ; l'auteur accepte ou
refuse ; s'il accepte, **son texte est remplacé et ses votes restent.**

## Les deux arbitrages de dom, le 15 septembre 2026

**1. Les votes portés sur une proposition reformulée restent valables, comme si rien
n'avait changé.** C'est une décision, prise contre la recommandation écrite au plan
MOD-10a, et elle a un coût qu'il vaut mieux nommer que taire : deux personnes qui ont
voté « d'accord » sur une phrase se retrouvent d'accord avec une autre phrase, qu'elles
n'ont pas lue.

Trois choses rendent ce coût tenable, et elles sont dans le code, pas seulement ici :

  - **l'auteur consent.** Le texte n'est jamais remplacé par le responsable seul. Un
    refus laisse la proposition exactement où elle était ;
  - **le texte d'origine est conservé** (`Reformulation.texte_origine`), donc ce sur quoi
    les votes ont réellement porté reste lisible ;
  - **l'acte est journalisé avec le texte d'origine dans son motif**, dans une table en
    ajout seul. C'est le seul acte du journal qui modifie un texte déjà voté, et il est
    le seul à porter l'ancien.

Ce qui reste à découvert, et qui appartient à dom : **le votant n'est averti de rien.**
C'est la conséquence directe de « comme si rien n'avait changé », et c'est écrit au
journal du lot plutôt que corrigé en douce.

**2. En attendant des arbitres tirés au sort, c'est le responsable qui propose.** Donc un
seul reformulateur, donc **un seul candidat**, donc pas d'élection — et le module
d'agrégation du MOD-9 n'est toujours appelé de nulle part. Son test-sentinelle reste en
place ; il tombera au lot qui amènera les arbitres (MOD-7, MOD-12), pas à celui-ci.

## Ce que ce module ne fait pas

Il ne retire rien, ne classe rien, n'écrit aucun message. Il ouvre un échange et le
referme. Le sort du signalement suit l'échange — clos par une acceptation, laissé ouvert
par un refus, parce qu'un refus rend la main au responsable au lieu de la lui prendre.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ActeModeration,
    Conversation,
    ModerationStatus,
    Participant,
    Reformulation,
    Signalement,
    Statement,
    StatutReformulation,
    StatutSignalement,
)
from app.services import journal_moderation
from app.services.conversations import MAX_STATEMENT_LENGTH
from app.services.signalement import Route

#: Combien de temps une reformulation attend la réponse de son auteur avant de
#: disparaître. **Quinze jours, décision de dom du 15 septembre 2026.**
#:
#: Pourquoi un délai plutôt qu'une attente sans fin : un échange ouvert indéfiniment
#: verrouille la proposition — l'index unique partiel interdit d'en proposer une autre
#: tant que la première attend — et laisse le responsable croire qu'il attend une réponse
#: qui ne viendra jamais. C'est précisément ce qui est arrivé à la proposition n° 142, dont
#: l'auteur n'est pas revenu.
#:
#: **C'est une règle, pas une décision de modération**, et c'est ce qui autorise une
#: horloge à l'appliquer : rien n'est décidé du sort de la proposition, qui reste
#: exactement ce qu'elle était. Seule la proposition de réécriture s'efface.
DELAI_VALIDATION = timedelta(days=15)

#: L'auteur, dans le journal d'audit. Même convention qu'au MOD-3b pour
#: `contestation_deposee` : on journalise l'acte, pas l'identité — celle d'un auteur est
#: le plus souvent un jeton de session.
AUTEUR_JOURNAL = "auteur de la proposition"


class ReformulationImpossible(ValueError):
    """Porte une phrase française destinée à être affichée telle quelle."""


async def signalements_a_reformuler(
    session: AsyncSession, statement_id: int
) -> list[Signalement]:
    """Les signalements encore reçus qui demandent une reformulation.

    C'est cette liste, et non la route la plus grave de la file, qui dit si une navette a
    lieu d'être : une proposition signalée pour incitation à la violence **et** pour
    mauvaise formulation se retire, elle ne se reformule pas — mais le jour où le retrait
    est annulé, la demande de reformulation, elle, est toujours là.
    """
    return list(
        await session.scalars(
            select(Signalement).where(
                Signalement.statement_id == statement_id,
                Signalement.statut == StatutSignalement.recu,
                Signalement.route == Route.A_REFORMULER.value,
            )
        )
    )


async def en_attente(
    session: AsyncSession, statement_id: int
) -> Reformulation | None:
    """La reformulation qui attend une réponse sur cette proposition, s'il y en a une."""
    return await session.scalar(
        select(Reformulation).where(
            Reformulation.statement_id == statement_id,
            Reformulation.statut == StatutReformulation.proposee,
        )
    )


async def proposer(
    session: AsyncSession,
    statement: Statement,
    texte: str,
    *,
    auteur: str,
    user_id=None,
) -> Reformulation:
    """Le responsable propose une version plus claire. **Rien n'est remplacé ici.**

    Six refus, et chacun empêche une navette qui n'aurait pas de sens :
    """
    texte = texte.strip()

    if statement.moderation_status is not ModerationStatus.approved:
        # Une proposition retirée n'a pas à être reformulée : ce qui lui est reproché
        # n'est pas sa formulation, et l'auteur a une voie de recours, pas une navette.
        raise ReformulationImpossible(
            "Cette proposition n'est pas publiée : il n'y a rien à reformuler."
        )

    if not await signalements_a_reformuler(session, statement.id):
        # Sans plainte ouverte, la navette serait une retouche éditoriale du responsable
        # sur le texte de quelqu'un d'autre. Ce n'est pas la même chose, et cela ne doit
        # pas passer par cette porte.
        raise ReformulationImpossible(
            "Aucun signalement en attente ne demande de reformuler cette proposition."
        )

    if await en_attente(session, statement.id) is not None:
        # L'index unique partiel le tiendrait aussi ; le dire ici rend l'écran lisible
        # plutôt que de laisser remonter une violation de contrainte.
        raise ReformulationImpossible(
            "Une reformulation attend déjà la réponse de l'auteur."
        )

    if not texte:
        raise ReformulationImpossible("La reformulation ne peut pas être vide.")

    if len(texte) > MAX_STATEMENT_LENGTH:
        raise ReformulationImpossible(
            f"Une proposition fait {MAX_STATEMENT_LENGTH} caractères au plus, "
            f"et celle-ci en fait {len(texte)}."
        )

    if texte == statement.text.strip():
        raise ReformulationImpossible(
            "Cette reformulation est identique au texte actuel."
        )

    # Le doublon est refusé ICI plutôt qu'à l'acceptation, pour que ce soit le
    # responsable qui le voie et non l'auteur : `uq_statement_text` porte sur
    # (conversation_id, text), et une acceptation qui échouerait sur une contrainte
    # laisserait l'auteur devant un message qu'il ne peut pas corriger.
    if await _texte_deja_pris(session, statement, texte):
        raise ReformulationImpossible(
            "Ce texte est déjà porté par une autre proposition de ce débat."
        )

    reformulation = Reformulation(
        statement_id=statement.id,
        texte_origine=statement.text,
        texte_propose=texte,
        propose_par=user_id,
    )
    session.add(reformulation)

    await journal_moderation.ajouter(
        session,
        acte=ActeModeration.reformulation_proposee,
        cible_type=journal_moderation.CIBLE_PROPOSITION,
        cible_id=statement.id,
        auteur=auteur,
        motif=texte,
    )
    await session.commit()
    await session.refresh(reformulation)
    return reformulation


async def accepter(
    session: AsyncSession, participant: Participant, reformulation_id: int
) -> bool:
    """L'auteur accepte : **son texte est remplacé, ses votes restent.**

    Rend faux si l'échange n'est pas le sien, n'attend plus de réponse, ou si la
    proposition a cessé d'être publiée entre-temps. Un refus rend faux et n'écrit rien —
    c'est la garde qui remplace l'état « caduque » qu'on n'a pas créé.

    **Les votes ne sont pas touchés, et c'est tout le lot.** Aucune ligne de `vote` n'est
    lue, comptée ni effacée ici : c'est ce que veut dire « comme si rien n'avait changé ».
    """
    reformulation, statement = await _echange_de(session, participant, reformulation_id)
    if reformulation is None:
        return False

    if await _texte_deja_pris(session, statement, reformulation.texte_propose):
        # Une autre proposition a pris ce texte entre la proposition et la réponse.
        # `uq_statement_text` refuserait de toute façon ; on préfère rendre faux que
        # faire tomber une contrainte au visage de l'auteur.
        return False

    ancien = statement.text
    statement.text = reformulation.texte_propose
    reformulation.statut = StatutReformulation.acceptee
    reformulation.repondu_le = datetime.now(timezone.utc)

    # Les plaintes qui demandaient la reformulation sont traitées : elles ont obtenu ce
    # qu'elles demandaient. Les autres — s'il y en a — ne le sont pas : une proposition
    # signalée aussi pour fausse information reste à qualifier.
    await session.execute(
        update(Signalement)
        .where(
            Signalement.statement_id == statement.id,
            Signalement.statut == StatutSignalement.recu,
            Signalement.route == Route.A_REFORMULER.value,
        )
        .values(statut=StatutSignalement.traite, traite_le=datetime.now(timezone.utc))
    )

    # **Le seul acte du journal qui modifie un texte déjà voté**, et le seul à porter
    # l'ancien. Sans cette ligne, « les votes restent valables » ne serait plus
    # vérifiable par personne.
    await journal_moderation.ajouter(
        session,
        acte=ActeModeration.reformulation_acceptee,
        cible_type=journal_moderation.CIBLE_PROPOSITION,
        cible_id=statement.id,
        auteur=AUTEUR_JOURNAL,
        motif=f"texte d'origine : {ancien}",
    )
    await session.commit()
    return True


async def refuser(
    session: AsyncSession, participant: Participant, reformulation_id: int
) -> bool:
    """L'auteur refuse : **rien ne bouge**, et le signalement reste ouvert.

    Laisser la plainte ouverte est le point : un refus rend la main au responsable — qui
    peut classer sans suite, ou reproposer — au lieu de la lui prendre. Clore le dossier
    sur un refus ferait du silence de l'auteur une décision de modération.
    """
    reformulation, statement = await _echange_de(session, participant, reformulation_id)
    if reformulation is None:
        return False

    reformulation.statut = StatutReformulation.refusee
    reformulation.repondu_le = datetime.now(timezone.utc)

    await journal_moderation.ajouter(
        session,
        acte=ActeModeration.reformulation_refusee,
        cible_type=journal_moderation.CIBLE_PROPOSITION,
        cible_id=statement.id,
        auteur=AUTEUR_JOURNAL,
    )
    await session.commit()
    return True


async def _echange_de(
    session: AsyncSession, participant: Participant, reformulation_id: int
) -> tuple[Reformulation | None, Statement | None]:
    """L'échange, s'il attend une réponse DE CE participant sur une proposition publiée.

    **Le contrôle d'appartenance n'est pas une politesse** : sans lui, n'importe qui
    pourrait accepter à la place de l'auteur une réécriture de ses propres mots en
    devinant un identifiant. C'est la même garde qu'au MOD-6 pour la fermeture d'un
    rappel, et elle porte ici sur bien plus qu'un encart.
    """
    reformulation = await session.get(Reformulation, reformulation_id)
    if reformulation is None or reformulation.statut is not StatutReformulation.proposee:
        return None, None

    statement = await session.get(Statement, reformulation.statement_id)
    if statement is None or statement.author_participant_id != participant.id:
        return None, None
    if statement.moderation_status is not ModerationStatus.approved:
        return None, None
    return reformulation, statement


async def _texte_deja_pris(
    session: AsyncSession, statement: Statement, texte: str
) -> bool:
    """`uq_statement_text` porte sur (conversation_id, text). On interroge la même paire."""
    autre = await session.scalar(
        select(Statement.id).where(
            Statement.conversation_id == statement.conversation_id,
            Statement.text == texte,
            Statement.id != statement.id,
        )
    )
    return autre is not None


# --- le registre, et l'expiration ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class LigneRegistre:
    """Une reformulation, avec de quoi la lire sans rouvrir la proposition.

    Le registre existe parce que l'écran des signalements ne montre **que ce qui est à
    faire** : une reformulation acceptée en sort avec la plainte qu'elle a close, et il
    ne reste aucune trace visible de ce qui a été proposé ni de ce que l'auteur a répondu.
    Le journal d'audit la porte, mais il n'est pas un écran de lecture.
    """

    reformulation: Reformulation
    statement: Statement
    conversation_titre: str
    conversation_slug: str

    @property
    def expire_le(self) -> datetime | None:
        """Quand cette reformulation disparaîtra faute de réponse. `None` si elle a
        déjà reçu la sienne."""
        if self.reformulation.statut is not StatutReformulation.proposee:
            return None
        return self.reformulation.propose_le + DELAI_VALIDATION

    @property
    def restant(self) -> timedelta | None:
        """Ce qu'il reste à l'auteur pour répondre. Peut être négatif juste avant le
        passage du worker — l'écran le dira plutôt que d'afficher une durée fausse."""
        echeance = self.expire_le
        if echeance is None:
            return None
        return echeance - datetime.now(timezone.utc)


async def registre(session: AsyncSession) -> list[LigneRegistre]:
    """Toutes les reformulations, les plus anciennement proposées d'abord.

    **Les trois états, et non les deux demandés.** dom a nommé « celles validées » et
    « celles en attente » ; les refusées sont là aussi. Un registre qui masquerait les
    refus donnerait à lire une suite de succès, c'est-à-dire exactement le contraire de
    ce qu'un registre sert à faire — et le refus est une réponse de l'auteur, la seule
    trace qu'il ait jamais laissée dans ce dispositif.

    L'ordre est celui de la proposition et non celui de la réponse : c'est la date à
    laquelle le responsable a écrit, et c'est elle qu'il cherche.
    """
    lignes = (
        await session.execute(
            select(Reformulation, Statement, Conversation)
            .join(Statement, Statement.id == Reformulation.statement_id)
            .join(Conversation, Conversation.id == Statement.conversation_id)
            .order_by(Reformulation.propose_le.desc())
        )
    ).all()
    return [
        LigneRegistre(
            reformulation=reformulation,
            statement=statement,
            conversation_titre=conversation.title,
            conversation_slug=conversation.slug,
        )
        for reformulation, statement, conversation in lignes
    ]


async def combien_en_attente(session: AsyncSession) -> int:
    """Combien de reformulations attendent encore leur auteur.

    **Calculé par le même filtre que la section du registre qu'il annonce**, et non par
    un `count(*)` plus économe : c'est la règle posée au MOD-3a pour les compteurs de la
    barre de modération. Un chiffre qui ne correspondrait pas à la liste ouverte ferait
    chercher pour rien.
    """
    return len(
        [
            ligne
            for ligne in await registre(session)
            if ligne.reformulation.statut is StatutReformulation.proposee
        ]
    )


async def purger_expirees(session: AsyncSession) -> int:
    """Supprime les reformulations sans réponse depuis plus de `DELAI_VALIDATION`.

    **Supprimées, et non marquées d'un état de plus.** C'est ce que dom a demandé, et
    c'est tenable ici pour une raison précise : le journal d'audit garde la ligne
    `reformulation_proposee`, en ajout seul. Ce qui a été proposé reste donc opposable,
    et l'absence d'un `reformulation_acceptee` en face dit qu'elle n'a jamais été
    validée. On efface un brouillon, pas une trace.

    **Rien n'est écrit au journal pour l'expiration elle-même.** Le journal enregistre
    des actes qui changent le sort d'une proposition ; une échéance qui passe n'en est
    pas un, et la proposition reste exactement ce qu'elle était. C'est la règle du
    MOD-13 — lire n'est pas un acte — appliquée à une horloge.

    Rend le nombre de lignes supprimées.
    """
    limite = datetime.now(timezone.utc) - DELAI_VALIDATION
    resultat = await session.execute(
        delete(Reformulation).where(
            Reformulation.statut == StatutReformulation.proposee,
            Reformulation.propose_le < limite,
        )
    )
    await session.commit()
    return resultat.rowcount or 0


async def acceptees_par_proposition(
    session: AsyncSession, statement_ids: list[int]
) -> dict[int, Reformulation]:
    """La dernière reformulation ACCEPTÉE de chacune de ces propositions (MOD-17).

    **En une requête pour la page entière**, comme `liens_verification.signalements()`
    pour les liens morts : c'est le même besoin — une mention servie avec un lot de
    propositions, qui ne doit pas coûter une requête par carte.

    **La dernière, et non la première.** Une proposition peut connaître plusieurs
    navettes à la suite ; ce que le lecteur veut savoir est *ce que disait le texte
    avant le dernier changement*, pas ce qu'il disait il y a six mois. L'histoire
    complète reste au registre et au journal d'audit, qui sont faits pour ça.

    Rend un dictionnaire vide pour une liste vide, sans interroger la base : la plupart
    des pages n'ont aucune proposition reformulée, et c'est le cas qui doit coûter zéro.
    """
    if not statement_ids:
        return {}
    lignes = list(
        await session.scalars(
            select(Reformulation)
            .where(
                Reformulation.statement_id.in_(statement_ids),
                Reformulation.statut == StatutReformulation.acceptee,
            )
            .order_by(Reformulation.repondu_le.asc())
        )
    )
    # Les plus anciennes d'abord, donc la dernière écrasée par la suivante : le
    # dictionnaire finit par ne porter que la plus récente de chaque proposition.
    return {ligne.statement_id: ligne for ligne in lignes}
