"""Le rappel qu'un auteur voit à sa prochaine visite (MOD-6, lot de repli).

**Le mur que le MOD-3b a révélé sans le nommer.** 164 participants sur 167 n'ont pas de
compte, donc pas d'adresse. Tous les messages didactiques écrits depuis le début du
chantier — l'exposé des motifs d'un retrait, « voici comment la reformuler » — sont
aujourd'hui **indélivrables**, et la navette du MOD-10 buterait sur le même mur.

Le jeton de `/contester` était la moitié de la réponse : il rend le recours ouvrable sans
compte. Il manquait l'autre moitié — **comment l'auteur apprend qu'il existe**. C'est ce
module.

**MOD-15 — la seconde forme, et ce qu'elle débloque.** Le module ne portait qu'un cas :
la proposition retirée. Il en porte deux. Le nouveau est le signalement *rattrapable* —
« mal formulé », « doublon » — sur une proposition qui, elle, **reste en ligne**.

Ce lot n'écrit aucun texte neuf, et c'est le point : le message existe depuis le MOD-3b
(`GABARIT_RATTRAPABLE`), il a été arbitré, réécrit une fois pour qu'il cesse de promettre
un effet que le code ne produit pas, et relu par le responsable sur son écran. Il n'avait
simplement **aucun moyen d'atteindre son destinataire**. Ce lot est l'acheminement, pas
la rédaction.

**Ce qu'il n'est pas.** Ce n'est pas la navette du MOD-10 : rien ne propose de
reformulation, rien ne recueille un accord, rien ne remplace un texte par un autre. Il
dit à l'auteur ce qui lui est reproché et le laisse libre. La navette outillée attend
toujours d'avoir été menée une fois à la main (MOD-10a).

Quatre règles, et chacune répond à quelque chose qu'on a déjà payé ailleurs :

  - **l'identification est celle qui porte déjà ses votes** (`participant`, jeton de
    cookie). Aucune création de compte, aucune adresse demandée, aucune identification
    nouvelle — c'est la même décision qu'au MOD-3a sur le signalement anonyme ;
  - **le rappel ne bloque rien.** Il se ferme, et il ne revient pas ;
  - **il n'apparaît jamais pendant une session de vote.** Le flux de vote est resté
    intouchable depuis le MOD-3b, et ce n'est pas un rappel didactique qui va l'entamer ;
  - **il ne dit rien qu'un tiers ne puisse voir.** Il s'adresse à l'auteur d'une
    proposition, sur sa propre proposition : aucun signalant, aucun décompte, aucun
    détail de qui a signalé quoi.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Conversation,
    ModerationStatus,
    Participant,
    Reformulation,
    Signalement,
    Statement,
    StatutReformulation,
    StatutSignalement,
)
from app.services import signalement as regles

#: Les deux formes. Ce sont des chaînes et non un `enum` parce qu'elles ne vont qu'au
#: gabarit : rien n'est stocké, rien n'est comparé en base, et un `enum` de plus dans
#: `app.models` laisserait croire qu'une colonne le porte quelque part.
FORME_RETRAIT = "retrait"
FORME_REFORMULATION = "reformulation"
#: MOD-10. Une reformulation est **proposée** et attend la réponse de l'auteur. C'est la
#: seule des trois formes qui pose une question au lieu d'informer.
FORME_NAVETTE = "navette"


@dataclass(frozen=True, slots=True)
class Rappel:
    """Une chose qui attend quelque chose de l'auteur. Deux formes depuis le MOD-15."""

    statement: Statement
    #: Le motif retenu, en toutes lettres. « propos_haineux » n'est pas un exposé.
    motif_libelle: str
    #: L'exposé des motifs, tel qu'il partirait dans un message.
    expose: str
    #: L'adresse du recours, jeton compris. `None` si le jeton manque — ce qui ne
    #: devrait pas arriver, `_retirer` le posant, mais une proposition retirée avant le
    #: MOD-3b n'en aurait pas. **Toujours `None` pour la forme « reformulation »** :
    #: rien n'a été retiré, donc aucun jeton n'a été posé, donc l'adresse ne mènerait
    #: nulle part.
    lien_contestation: str | None
    #: `FORME_RETRAIT` ou `FORME_REFORMULATION`. Le gabarit s'en sert pour la phrase de
    #: tête ; c'est la seule chose qui distingue visuellement les deux blocs, et elle
    #: suffit — le reste (l'exposé, le bouton de fermeture) est commun.
    forme: str = FORME_RETRAIT
    #: L'adresse du débat, pour y proposer une version plus claire. `None` pour un
    #: retrait : une proposition retirée pour ligne rouge ne se reprend pas, et le MOD-3b
    #: a fait réécrire un gabarit entier pour cesser de le laisser croire.
    lien_reprise: str | None = None
    #: MOD-10 — la reformulation proposée, pour la forme « navette ». `None` ailleurs.
    texte_propose: str | None = None
    #: MOD-10 — l'identifiant de l'échange, sur lequel postent les deux boutons.
    reformulation_id: int | None = None

    @property
    def statement_id(self) -> int:
        return self.statement.id

    @property
    def chemin_fermeture(self) -> str:
        """Où poste le bouton « ne plus afficher ».

        Deux adresses et non une, parce que les deux formes ne ferment pas la même
        chose : le retrait écrit sur la proposition (MOD-6), la reformulation écrit sur
        les signalements qui l'ont provoquée (MOD-15). Une seule adresse aurait obligé
        la route à deviner laquelle des deux on écarte — et à se tromper le jour où une
        proposition porte les deux.
        """
        if self.forme == FORME_REFORMULATION:
            return f"/rappels/reformulation/{self.statement_id}/fermer"
        return f"/rappels/{self.statement_id}/fermer"


async def en_attente(
    session: AsyncSession, participant: Participant | None
) -> list[Rappel]:
    """Ce qui attend quelque chose de ce participant, et qu'il n'a pas encore fermé.

    Deux formes depuis le MOD-15, dans cet ordre : ses propositions **retirées**, puis
    ses propositions **encore en ligne mais signalées de façon rattrapable**. L'ordre
    n'est pas une préférence d'affichage — un retrait est plus grave, et son exposé des
    motifs est **dû** au titre du DSA là où le second est un service rendu.

    **Deux requêtes, et non une**, contrairement à ce que promettait le MOD-6. Elles ne
    partent pas de la même table et aucune ne peut se déduire de l'autre. Toutes deux
    partent de `author_participant_id`, indexé par sa clé étrangère, et ne rendent rien
    pour un visiteur sans historique — ce qui reste le cas courant sur les pages qui les
    appellent. Un visiteur sans identité du tout n'en déclenche aucune.
    """
    if participant is None:
        return []

    navettes = await _navettes(session, participant)
    # Une proposition qui porte une navette n'affiche pas, en plus, l'avertissement qui
    # l'a provoquée : deux blocs pour un même texte, dont l'un pose une question et
    # l'autre l'ignore. La navette dit tout ce que l'autre disait, et demande en plus.
    deja_vues = {rappel.statement_id for rappel in navettes}
    autres = [
        rappel
        for rappel in await _reformulations(session, participant)
        if rappel.statement_id not in deja_vues
    ]
    return await _retraits(session, participant) + navettes + autres


async def _retraits(session: AsyncSession, participant: Participant) -> list[Rappel]:
    """La forme d'origine (MOD-6) : la proposition a été retirée, et il n'y a rien à
    reprendre."""
    retirees = list(
        await session.scalars(
            select(Statement)
            .where(
                Statement.author_participant_id == participant.id,
                Statement.moderation_status == ModerationStatus.retire,
                Statement.rappel_ferme_le.is_(None),
            )
            .order_by(Statement.retire_le.desc())
        )
    )

    rappels = []
    for statement in retirees:
        code = statement.retire_motif or ""
        connu = code in regles.CODES
        rappels.append(
            Rappel(
                statement=statement,
                motif_libelle=regles.libelle(code) if connu else "Non précisé",
                expose=regles.message_a_l_auteur(
                    regles.Route.RETRAIT_CONSERVATOIRE,
                    [code] if connu else ["autre"],
                    jeton=statement.jeton_contestation,
                ),
                lien_contestation=(
                    regles.lien_de_contestation(statement.jeton_contestation)
                    if statement.jeton_contestation
                    else None
                ),
                forme=FORME_RETRAIT,
            )
        )
    return rappels


async def _navettes(session: AsyncSession, participant: Participant) -> list[Rappel]:
    """La forme du MOD-10 : une reformulation est proposée, et **on attend une réponse**.

    C'est la seule des trois formes qui demande quelque chose. Elle passe donc avant
    l'avertissement simple, et elle n'a **pas de bouton « ne plus afficher »** : refuser
    est la façon de la faire disparaître, et c'est un clic. Un troisième bouton qui
    l'écarterait sans répondre laisserait l'échange ouvert pour toujours du côté du
    responsable, qui croirait attendre une réponse qui ne viendra jamais.
    """
    lignes = (
        await session.execute(
            select(Reformulation, Statement, Conversation.slug)
            .join(Statement, Statement.id == Reformulation.statement_id)
            .join(Conversation, Conversation.id == Statement.conversation_id)
            .where(
                Statement.author_participant_id == participant.id,
                Statement.moderation_status == ModerationStatus.approved,
                Reformulation.statut == StatutReformulation.proposee,
            )
            .order_by(Reformulation.propose_le.desc())
        )
    ).all()

    return [
        Rappel(
            statement=statement,
            motif_libelle=regles.libelle("mal_formule"),
            # Pas de gabarit de message ici : les trois gabarits du MOD-3b décrivent une
            # plainte en cours d'examen, et ce n'est plus l'état des choses. Ce qu'il y a
            # à lire, c'est le texte proposé lui-même — le gabarit le met en regard de
            # l'actuel, ce qu'aucune phrase ne remplace.
            expose="",
            lien_contestation=None,
            forme=FORME_NAVETTE,
            lien_reprise=f"/c/{slug}",
            texte_propose=reformulation.texte_propose,
            reformulation_id=reformulation.id,
        )
        for reformulation, statement, slug in lignes
    ]


async def _reformulations(
    session: AsyncSession, participant: Participant
) -> list[Rappel]:
    """La forme du MOD-15 : la proposition est **encore en ligne**, et on lui reproche
    quelque chose qui se répare.

    Quatre conditions, et chacune écarte un rappel qui mentirait :

      - **la route est rattrapable** (`ROUTES_RATTRAPABLES`, dérivée des gabarits) : on
        n'avertit que là où l'auteur peut faire quelque chose. Un `A_QUALIFIER` ne lui
        demande rien, et l'avertir reviendrait à l'inquiéter pour rien ;
      - **le signalement est encore `recu`.** Classé sans suite, il ne concerne plus
        personne ; traité, la décision est ailleurs. Dire « un examen est en cours » sur
        un dossier clos serait faux ;
      - **la proposition est toujours publiée.** Retirée entre-temps, c'est la forme
        « retrait » qui parle, et elle dit quelque chose de plus grave. Deux blocs sur la
        même proposition se contrediraient — l'un « elle reste en ligne », l'autre
        « elle a été retirée » ;
      - **le rappel n'a pas été écarté** par l'auteur.
    """
    lignes = (
        await session.execute(
            select(Signalement, Statement, Conversation.slug)
            .join(Statement, Statement.id == Signalement.statement_id)
            .join(Conversation, Conversation.id == Statement.conversation_id)
            .where(
                Statement.author_participant_id == participant.id,
                Statement.moderation_status == ModerationStatus.approved,
                Signalement.route.in_([r.value for r in regles.ROUTES_RATTRAPABLES]),
                Signalement.statut == StatutSignalement.recu,
                Signalement.rappel_ferme_le.is_(None),
            )
            .order_by(Signalement.cree_le.desc())
        )
    ).all()

    # **Un dossier par proposition, pas un par plainte.** C'est la règle du MOD-13 pour
    # les contestations, et elle vaut ici pour la même raison : trois blocs identiques
    # feraient croire à trois reproches distincts là où il y a une proposition à
    # reprendre. Les motifs se cumulent, le bloc reste unique.
    groupes: dict[int, list] = {}
    contexte: dict[int, tuple] = {}
    for signalement, statement, slug in lignes:
        groupes.setdefault(statement.id, []).append(signalement)
        contexte.setdefault(statement.id, (statement, slug))

    rappels = []
    for statement_id, signalements in groupes.items():
        statement, slug = contexte[statement_id]
        # Les motifs de toutes les plaintes, dédoublonnés, dans l'ordre d'arrivée :
        # `decidant` choisira le plus grave, comme il le fait au dépôt.
        motifs: list[str] = []
        for signalement in signalements:
            for code in signalement.motifs:
                if code in regles.CODES and code not in motifs:
                    motifs.append(code)
        if not motifs:
            # Un signalement dont aucun motif n'est plus connu de la liste. Il reste dans
            # la file du responsable, mais on n'écrit pas à l'auteur un exposé qu'on ne
            # sait plus formuler.
            continue

        # La route est prise sur la première plainte. Ce n'est pas un choix par défaut :
        # `ROUTES_RATTRAPABLES` est dérivée de l'identité du gabarit, donc toutes les
        # routes de cet ensemble produisent le MÊME texte. Prendre l'une ou l'autre est
        # démontrablement équivalent, et le jour où ce ne le serait plus, c'est que
        # l'ensemble aurait cessé d'être dérivé.
        route = regles.Route(signalements[0].route)
        rappels.append(
            Rappel(
                statement=statement,
                motif_libelle=regles.libelle(regles.decidant(motifs).code),
                # **Sans voie de recours, et c'est un défaut évité de justesse.** Le
                # lien de contestation exige un jeton, posé au seul retrait. Sur une
                # proposition encore en ligne il n'y en a pas, et le gabarit serait
                # tombé sur l'adresse générique — une porte qui n'ouvre pas.
                expose=regles.message_a_l_auteur(route, motifs, avec_recours=False),
                lien_contestation=None,
                forme=FORME_REFORMULATION,
                lien_reprise=f"/c/{slug}",
            )
        )
    return rappels


async def fermer(
    session: AsyncSession, participant: Participant, statement_id: int
) -> bool:
    """Ferme un rappel. Rend faux si la proposition n'est pas à ce participant.

    **Le contrôle d'appartenance n'est pas une politesse** : sans lui, n'importe qui
    pourrait fermer le rappel de n'importe qui en devinant un identifiant de
    proposition — et l'auteur ne saurait jamais que sa proposition a été retirée.
    """
    statement = await session.get(Statement, statement_id)
    if statement is None or statement.author_participant_id != participant.id:
        return False
    statement.rappel_ferme_le = datetime.now(timezone.utc)
    await session.commit()
    return True


async def fermer_reformulation(
    session: AsyncSession, participant: Participant, statement_id: int
) -> bool:
    """Écarte le rappel né des signalements rattrapables d'une proposition.

    **Écrit sur les signalements, pas sur la proposition.** C'est ce qui fait qu'un
    signalement déposé demain reparlera : la fermeture porte sur les plaintes déjà vues,
    pas sur le texte. Voir la migration 0023.

    **Toutes celles du groupe, en un geste.** L'écran n'a montré qu'un bloc pour la
    proposition (`_reformulations`) : n'en fermer qu'une ferait réapparaître le même bloc
    à la visite suivante, et l'auteur croirait le bouton cassé.

    Même contrôle d'appartenance qu'au MOD-6, et il vaut ici pour une raison de plus : le
    signaleur n'est pas l'auteur. Sans lui, celui qui vient de signaler pourrait faire
    taire l'avertissement destiné à celui qu'il signale.

    Rien n'est écrit au journal d'audit : écarter un rappel n'est pas un acte de
    modération, et le `statut` du signalement ne bouge pas — le dossier reste `recu` dans
    la file du responsable.
    """
    statement = await session.get(Statement, statement_id)
    if statement is None or statement.author_participant_id != participant.id:
        return False

    signalements = list(
        await session.scalars(
            select(Signalement).where(
                Signalement.statement_id == statement_id,
                Signalement.rappel_ferme_le.is_(None),
            )
        )
    )
    if not signalements:
        return False

    maintenant = datetime.now(timezone.utc)
    for signalement in signalements:
        signalement.rappel_ferme_le = maintenant
    await session.commit()
    return True
