"""Le dépôt d'un signalement, le retrait conservatoire, la file du responsable.

Tout ce qui touche la base. La liste fermée des motifs, la règle de routage et les
gabarits de message sont dans `app/services/signalement.py`, qui ne connaît aucune
session — c'est ce qui permet de relire la règle sans lire de SQL, et de la tester sans
base.

**Un signalement n'est pas un verdict.** C'est une plainte : elle ouvre un examen, elle
ne le conclut pas. Le seul endroit de tout le chantier où un signalement agit seul est la
ligne rouge (`_retirer`), et c'est parce que le coût d'attendre y est plus élevé que le
coût de se tromper — un retrait conservatoire s'annule en un clic, une diffusion ne
s'annule pas. Attendre que 20 % des lecteurs signalent un appel à la violence, c'est
l'avoir laissé tourner.
"""

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AUTEUR_DE_LA_PROPOSITION,
    AUTEUR_SYSTEME,
    ActeModeration,
    Contestation,
    Conversation,
    ModerationStatus,
    Participant,
    Signalement,
    Statement,
    StatutSignalement,
)
from app.services import journal_moderation
from app.services import rate_limit
from app.services import signalement as regles
from app.services.signalement import Route, SignalementInvalide

#: Durée de conservation des données de contexte (référent, IP). Au-delà, les deux
#: colonnes passent à NULL — le signalement, lui, reste. Voir `purger_contexte` et la
#: commande `purge-contexte-signalements`.
JOURS_CONTEXTE = 30

#: Gravité des routes, de la plus grave à la moins grave. Sert à trier la file : c'est
#: l'ordre de déclaration de `Route`, qui suit lui-même l'ordre des motifs.
_GRAVITE: dict[Route, int] = {route: rang for rang, route in enumerate(Route)}


class PropositionNonSignalable(Exception):
    """La proposition visée n'est pas (ou n'est plus) en circulation.

    Aucun signalement sur une proposition déjà retirée : le retrait a eu lieu, la
    plainte n'a plus d'objet, et accepter la ligne ferait gonfler des compteurs qui
    servent à mesurer autre chose.
    """


class PlafondAtteint(Exception):
    """Un des deux plafonds horaires est franchi. Voir `services/rate_limit.py`.

    **Son message ne sort jamais du serveur** — il est destiné au journal d'exécution,
    pas au signaleur. Depuis le MOD-3b, la route répond à un plafond exactement ce
    qu'elle répond à un succès : « Merci, c'est enregistré. » Dire à quelqu'un qu'il
    vient d'atteindre un plafond, c'est lui apprendre qu'il en existe un, à quelle
    cadence il se remplit, et donc comment le contourner.
    """


# --- le dépôt --------------------------------------------------------------------


async def deposer(
    session: AsyncSession,
    *,
    statement: Statement,
    participant: Participant,
    motifs: list[str],
    texte_libre: str | None = None,
    referent: str | None = None,
    adresse_ip: str | None = None,
    sollicite: bool = False,
) -> tuple[Signalement, bool]:
    """Enregistre un signalement. Renvoie (signalement, nouveau).

    `nouveau` vaut faux quand cette identité avait déjà signalé cette proposition :
    c'est alors un **succès idempotent**, pas une erreur. Le signaleur reçoit la même
    réponse dans les deux cas — « merci, c'est enregistré » — et l'appelant n'a donc
    rien à en faire d'autre que de la mesure.

    L'ordre des contrôles n'est pas indifférent :

      1. la règle (motifs, texte libre) — elle ne coûte rien et ne touche pas la base ;
      2. l'état de la proposition ;
      3. **le doublon, avant le plafond** : re-signaler ce qu'on a déjà signalé ne doit
         pas consommer de quota, sinon un double clic coûte une des dix lignes de
         l'heure ;
      4. le plafond ;
      5. l'insertion, en `ON CONFLICT DO NOTHING` — deux requêtes concurrentes de la
         même identité passeraient toutes deux le contrôle 3.

    Le retrait conservatoire, s'il a lieu, tombe dans la MÊME transaction que le
    signalement qui le déclenche.

    **`sollicite` (MOD-4) : le site a-t-il provoqué cette plainte ?** Vrai quand elle naît
    d'un « proposition à revoir » rendu par quelqu'un que le site avait tiré au sort. Deux
    conséquences, et elles sont nommées ici parce que c'est le seul endroit où on les voit
    ensemble :

      - **le plafond horaire ne s'applique pas.** Reprocher à quelqu'un de répondre trop
        souvent à une question qu'on lui pose soi-même n'aurait pas de sens ; et ce n'est
        pas une porte dérobée, parce que ce chemin porte ses propres plafonds, plus serrés
        — trois sollicitations par jour et par identité, et une seule réponse par
        proposition. Le plafond du signalement spontané, lui, reste entier ;
      - **la ligne est marquée**, et le MOD-5 l'écarte de ses indicateurs. C'est le point
        qui ne pouvait pas attendre le lot : sans cette colonne, la détection d'afflux
        coordonné se déclencherait sur le sondage du site lui-même.

    Tout le reste est identique : même règle de motifs, même route, même retrait
    conservatoire sur une ligne rouge. Un signalement sollicité est un signalement.
    """
    route = regles.router(motifs)
    texte = regles.valider_texte_libre(motifs, texte_libre)

    if statement.moderation_status is not ModerationStatus.approved:
        raise PropositionNonSignalable(
            "Cette proposition n'est pas en circulation : il n'y a rien à signaler."
        )

    existant = await session.scalar(
        select(Signalement).where(
            Signalement.statement_id == statement.id,
            Signalement.participant_id == participant.id,
        )
    )
    if existant is not None:
        return existant, False

    # Condensés salés et tronqués, jamais en clair. Le référent n'est gardé que s'il est
    # EXTERNE : un référent interne serait le cas de presque toutes les lignes et ne
    # dirait rien d'un afflux coordonné.
    adresse_condensee = regles.condenser(adresse_ip)

    if not sollicite and not await rate_limit.signalement_allowed(
        session, participant.id, adresse_condensee
    ):
        raise PlafondAtteint(
            f"Plafond horaire atteint pour le participant {participant.id}."
        )

    insertion = (
        pg_insert(Signalement)
        .values(
            statement_id=statement.id,
            participant_id=participant.id,
            motif_1=motifs[0],
            motif_2=motifs[1] if len(motifs) > 1 else None,
            texte_libre=texte,
            route=route.value,
            referent_hache=regles.condenser(regles.origine_externe(referent)),
            ip_hachee=adresse_condensee,
            sollicite=sollicite,
        )
        .on_conflict_do_nothing(constraint="uq_signalement_identite")
        .returning(Signalement.id)
    )
    nouvel_id = await session.scalar(insertion)
    if nouvel_id is None:
        # La course a été perdue : l'autre requête a écrit la ligne. C'est le même
        # succès idempotent que le contrôle 3, atteint par un autre chemin.
        await session.commit()
        ligne = await session.scalar(
            select(Signalement).where(
                Signalement.statement_id == statement.id,
                Signalement.participant_id == participant.id,
            )
        )
        return ligne, False

    if route is Route.RETRAIT_CONSERVATOIRE:
        await _retirer(session, statement, motifs=motifs, auteur=AUTEUR_SYSTEME)

    await session.commit()
    return await session.get(Signalement, nouvel_id), True


# --- le retrait conservatoire ----------------------------------------------------

#: Octets tirés au sort pour un jeton de recours. 32 octets font 43 signes en base64
#: URL, soit 256 bits : c'est le jeton qui remplace le mot de passe d'un compte que
#: l'auteur n'a pas, et il doit tenir seul. Même ordre de grandeur que le jeton anonyme
#: de `participants.new_anon_token`, qui protège l'historique de vote d'une personne.
JETON_OCTETS = 32


def nouveau_jeton() -> str:
    """Un jeton de recours, tiré de `secrets`. Jamais d'un identifiant, jamais d'une date.

    `secrets.token_urlsafe` et non `uuid4()` : un UUID se lit comme un identifiant et
    finit par être exposé ailleurs « puisque c'en est un ». Celui-ci ne désigne rien
    qu'un droit d'accès.
    """
    return secrets.token_urlsafe(JETON_OCTETS)


async def _retirer(
    session: AsyncSession, statement: Statement, *, motifs: list[str], auteur: str
) -> None:
    """Sort la proposition de la circulation, et journalise. **Sans commit.**

    Déclenché par le PREMIER signalement de ligne rouge : sans seuil, sans délai, sans
    quorum. Rien n'est supprimé — le statut change, trois colonnes se remplissent, les
    votes déjà émis restent en base et restent comptés dans les groupes d'opinion.

    Le motif retenu est celui qui a décidé de la route, pas le premier saisi : c'est lui
    qu'exposera le message à l'auteur.
    """
    decidant = min(motifs, key=lambda code: regles.CODES.index(code))
    statement.moderation_status = ModerationStatus.retire
    statement.retire_le = datetime.now(timezone.utc)
    statement.retire_motif = decidant
    statement.retire_par = auteur
    # Le jeton de recours est posé ICI, au moment même du retrait, et pas à la première
    # demande : c'est le retrait qui ouvre le droit, et un jeton créé plus tard serait un
    # droit qui dépend de ce que quelqu'un a pensé à faire. Jamais réémis — un jeton qui
    # tourne invaliderait une adresse déjà transmise à l'auteur.
    if statement.jeton_contestation is None:
        statement.jeton_contestation = nouveau_jeton()
    await journal_moderation.ajouter(
        session,
        acte=ActeModeration.retrait_conservatoire,
        cible_type=journal_moderation.CIBLE_PROPOSITION,
        cible_id=statement.id,
        auteur=auteur,
        motif=decidant,
    )


# --- les trois gestes du responsable ---------------------------------------------


async def confirmer_retrait(
    session: AsyncSession, statement: Statement, *, auteur: str, user_id=None
) -> None:
    """Le responsable confirme : la proposition reste retirée, les signalements sont traités."""
    if statement.moderation_status is not ModerationStatus.retire:
        raise PropositionNonSignalable("Cette proposition n'est pas retirée.")
    statement.retire_par = auteur
    await _clore(session, statement, StatutSignalement.traite, user_id)
    await journal_moderation.ajouter(
        session,
        acte=ActeModeration.retrait_confirme,
        cible_type=journal_moderation.CIBLE_PROPOSITION,
        cible_id=statement.id,
        auteur=auteur,
        motif=statement.retire_motif,
    )
    await session.commit()


async def annuler_retrait(
    session: AsyncSession, statement: Statement, *, auteur: str, user_id=None
) -> None:
    """Le responsable annule : la proposition **revient en circulation**.

    Les trois colonnes de retrait sont remises à NULL : elles décrivent un retrait en
    cours, pas un historique — l'historique, c'est le journal, qui garde les deux actes.
    """
    if statement.moderation_status is not ModerationStatus.retire:
        raise PropositionNonSignalable("Cette proposition n'est pas retirée.")
    motif = statement.retire_motif
    statement.moderation_status = ModerationStatus.approved
    statement.retire_le = None
    statement.retire_motif = None
    statement.retire_par = None
    # Le jeton de recours tombe avec le retrait : il n'y a plus rien à contester, et une
    # adresse qui resterait ouvrable inviterait l'auteur à défendre une proposition qu'on
    # vient de lui rendre. Les contestations déjà déposées, elles, restent — c'est ce qui
    # a motivé l'annulation.
    statement.jeton_contestation = None
    await _clore(session, statement, StatutSignalement.traite, user_id)
    await journal_moderation.ajouter(
        session,
        acte=ActeModeration.retrait_annule,
        cible_type=journal_moderation.CIBLE_PROPOSITION,
        cible_id=statement.id,
        auteur=auteur,
        motif=motif,
    )
    await session.commit()


async def classer_sans_suite(
    session: AsyncSession, statement: Statement, *, auteur: str, user_id=None
) -> None:
    """Le responsable classe : les signalements sont clos, la proposition ne bouge pas.

    Une ligne de journal **par signalement classé**, et non une par proposition : c'est
    chaque plainte qui est classée, et c'est chacune qu'il faudra pouvoir montrer si son
    auteur demande ce qu'elle est devenue.
    """
    for signalement in await _en_attente(session, statement.id):
        await journal_moderation.ajouter(
            session,
            acte=ActeModeration.classement_sans_suite,
            cible_type=journal_moderation.CIBLE_SIGNALEMENT,
            cible_id=signalement.id,
            auteur=auteur,
            motif=", ".join(signalement.motifs),
        )
    await _clore(session, statement, StatutSignalement.classe_sans_suite, user_id)
    await session.commit()


async def _en_attente(session: AsyncSession, statement_id: int) -> list[Signalement]:
    return list(
        await session.scalars(
            select(Signalement).where(
                Signalement.statement_id == statement_id,
                Signalement.statut == StatutSignalement.recu,
            )
        )
    )


async def _clore(
    session: AsyncSession,
    statement: Statement,
    statut: StatutSignalement,
    user_id,
) -> None:
    """Passe tous les signalements encore reçus de cette proposition à `statut`.

    `traite_par` est l'identifiant du compte qui a tranché, quand il y en a un. Le
    journal, lui, garde l'adresse en clair : l'un sert à joindre une décision à un
    compte vivant, l'autre à rester lisible quand le compte n'existe plus.
    """
    await session.execute(
        update(Signalement)
        .where(
            Signalement.statement_id == statement.id,
            Signalement.statut == StatutSignalement.recu,
        )
        .values(
            statut=statut, traite_le=datetime.now(timezone.utc), traite_par=user_id
        )
    )


# --- la contestation (MOD-3b) ------------------------------------------------------

#: Longueur du texte qu'un auteur peut écrire pour sa défense. Deux fois une proposition :
#: de quoi expliquer, pas de quoi ouvrir une correspondance.
CONTESTATION_MAX = 1000


class ContestationInvalide(ValueError):
    """Le texte de la contestation est vide ou trop long. Message affichable tel quel."""


async def proposition_contestable(
    session: AsyncSession, jeton: str
) -> Statement | None:
    """La proposition que ce jeton ouvre, ou `None`.

    **Le jeton est le seul droit d'accès**, et il est comparé tel quel : il n'y a pas de
    compte sur lequel s'appuyer, l'auteur étant anonyme dans 98 % des cas. Un jeton
    inconnu, vide, ou pointant sur une proposition qui n'est plus retirée rend `None`, et
    l'appelant répond 404 — jamais 403 : un 403 confirmerait que le jeton a existé.
    """
    if not jeton:
        return None
    statement = await session.scalar(
        select(Statement).where(Statement.jeton_contestation == jeton)
    )
    if statement is None or statement.moderation_status is not ModerationStatus.retire:
        return None
    return statement


async def deposer_contestation(
    session: AsyncSession, statement: Statement, texte: str
) -> Contestation:
    """Enregistre une demande de réexamen, et la journalise.

    Aucune décision n'en découle, aucun délai n'est promis : la ligne remonte en tête de
    l'écran du responsable, et c'est tout ce que ce lot fait d'elle.

    Rien n'empêche d'en déposer plusieurs, délibérément : un auteur qui se ravise ou
    complète son explication ne doit pas se heurter à une porte fermée, et le volume est
    borné par le fait qu'il faut détenir le jeton.
    """
    propre = (texte or "").strip()
    if not propre:
        raise ContestationInvalide("Expliquez en quelques mots ce que vous contestez.")
    if len(propre) > CONTESTATION_MAX:
        raise ContestationInvalide(
            f"Votre texte fait {len(propre)} caractères, le maximum est "
            f"{CONTESTATION_MAX}."
        )

    contestation = Contestation(statement_id=statement.id, texte=propre)
    session.add(contestation)
    await journal_moderation.ajouter(
        session,
        acte=ActeModeration.contestation_deposee,
        cible_type=journal_moderation.CIBLE_PROPOSITION,
        cible_id=statement.id,
        # Ni l'identité ni le jeton : l'une est un jeton de session, l'autre est la clé
        # du recours. Le journal ne se modifie jamais, donc rien n'en ressort non plus.
        auteur=AUTEUR_DE_LA_PROPOSITION,
        motif=statement.retire_motif,
    )
    await session.commit()
    await session.refresh(contestation)
    return contestation


#: Les trois états dans lesquels le responsable peut trouver une proposition contestée.
#: Ce ne sont pas des états en base : ils se déduisent de la proposition, et ils existent
#: parce qu'une contestation ne dit rien à elle seule — la même phrase se lit autrement
#: selon que la proposition est encore retirée ou déjà revenue en ligne.
CONTESTATION_RETIREE = "retiree"
CONTESTATION_REMISE_EN_LIGNE = "remise_en_ligne"
CONTESTATION_PROPOSITION_DISPARUE = "disparue"


@dataclass
class DossierConteste:
    """Les contestations d'UNE proposition, et l'état où le responsable la trouve.

    **Une proposition et non une contestation**, pour la même raison que `LigneFile` :
    un auteur qui écrit trois fois défend un seul retrait. Trois cartes feraient croire à
    trois dossiers, et le schéma autorise explicitement les dépôts multiples — voir
    `deposer_contestation`, qui les accueille délibérément.
    """

    statement: Statement | None
    #: Toutes les contestations de la proposition, lues comprises, la plus ancienne
    #: d'abord. Les lues sont gardées : sans elles, le responsable relirait une
    #: troisième demande sans savoir que les deux premières disaient déjà la même chose.
    contestations: list[Contestation] = field(default_factory=list)

    @property
    def non_lues(self) -> list[Contestation]:
        return [c for c in self.contestations if c.traite_le is None]

    @property
    def deja_lues(self) -> list[Contestation]:
        return [c for c in self.contestations if c.traite_le is not None]

    @property
    def etat(self) -> str:
        """Ce qu'est devenue la proposition depuis que l'auteur a écrit.

        **Le cas qui ne doit pas rester un angle mort est le deuxième.** Annuler un
        retrait remet la proposition en ligne et ferme la voie de recours (le jeton passe
        à NULL), mais les contestations déjà déposées restent en base — c'est écrit dans
        `annuler_retrait`. Sans cet état, elles continueraient de s'afficher comme des
        demandes en souffrance, et le responsable rouvrirait un dossier que lui-même a
        déjà tranché en faveur de l'auteur.
        """
        if self.statement is None:
            return CONTESTATION_PROPOSITION_DISPARUE
        if self.statement.moderation_status is ModerationStatus.retire:
            return CONTESTATION_RETIREE
        return CONTESTATION_REMISE_EN_LIGNE

    @property
    def plus_ancienne(self) -> datetime:
        return min(c.cree_le for c in self.contestations)


async def _grouper_par_proposition(
    session: AsyncSession, contestations: list[Contestation]
) -> list[DossierConteste]:
    """Regroupe des contestations par proposition, en gardant l'ordre d'arrivée."""
    dossiers: dict[int, DossierConteste] = {}
    for contestation in contestations:
        dossier = dossiers.get(contestation.statement_id)
        if dossier is None:
            dossier = DossierConteste(
                statement=await session.get(Statement, contestation.statement_id)
            )
            dossiers[contestation.statement_id] = dossier
        dossier.contestations.append(contestation)
    return list(dossiers.values())


async def contestations_a_lire(session: AsyncSession) -> list[DossierConteste]:
    """Les dossiers portant au moins une contestation NON LUE, le plus ancien d'abord.

    La plus ancienne d'abord et non la plus récente : c'est une file d'attente de
    personnes, et une file d'attente se sert dans l'ordre d'arrivée.

    Le dossier rendu porte **toutes** les contestations de la proposition, y compris
    celles déjà lues : ce qui fait sortir un dossier de cette liste est qu'il ne reste
    rien à y lire, pas qu'on efface ce qui a été lu.
    """
    non_lues = list(
        await session.scalars(
            select(Contestation)
            .where(Contestation.traite_le.is_(None))
            .order_by(Contestation.cree_le)
        )
    )
    dossiers = await _grouper_par_proposition(session, non_lues)
    for dossier in dossiers:
        dossier.contestations = await _contestations_de(
            session, dossier.contestations[0].statement_id
        )
    return dossiers


async def _contestations_de(
    session: AsyncSession, statement_id: int
) -> list[Contestation]:
    return list(
        await session.scalars(
            select(Contestation)
            .where(Contestation.statement_id == statement_id)
            .order_by(Contestation.cree_le)
        )
    )


async def contestations_des_propositions(
    session: AsyncSession, statement_ids: list[int]
) -> dict[int, list[Contestation]]:
    """Les contestations de ces propositions, lues comprises, par identifiant.

    C'est ce qui alimente la carte de la proposition dans la file, **là où les trois
    gestes se prennent**. Les lues y restent : sur la carte, une contestation n'est pas
    une tâche à faire, c'est la défense de l'auteur — elle doit être sous les yeux de
    qui confirme ou annule, qu'elle ait déjà été ouverte ou non.
    """
    if not statement_ids:
        return {}
    lignes = list(
        await session.scalars(
            select(Contestation)
            .where(Contestation.statement_id.in_(statement_ids))
            .order_by(Contestation.cree_le)
        )
    )
    par_proposition: dict[int, list[Contestation]] = {}
    for ligne in lignes:
        par_proposition.setdefault(ligne.statement_id, []).append(ligne)
    return par_proposition


async def marquer_contestation_lue(
    session: AsyncSession, contestation_id: int
) -> bool:
    """Horodate la lecture d'une contestation. Faux si elle n'existe pas.

    **Une contestation déjà lue n'est pas ré-horodatée.** La date qui a une valeur est
    celle du premier regard — c'est elle qui dira un jour combien de temps un auteur a
    attendu d'être lu. La réécrire à chaque passage en ferait la date du dernier clic,
    qui ne renseigne sur rien.

    **Rien n'est écrit au journal d'audit, et c'est délibéré.** Le journal enregistre des
    ACTES de modération, qui changent le sort d'une proposition ; lire n'en est pas un.
    Y verser « a lu » mêlerait une trace d'attention à des décisions opposables, et
    gonflerait d'une ligne par consultation une table qu'on ne peut plus jamais nettoyer.
    Le jour où ce que le responsable a lu devra être opposable, c'est un acte de
    RÉPONSE qu'il faudra journaliser, pas un accusé de lecture.
    """
    contestation = await session.get(Contestation, contestation_id)
    if contestation is None:
        return False
    if contestation.traite_le is None:
        contestation.traite_le = datetime.now(timezone.utc)
        await session.commit()
    return True


# --- la file du responsable ------------------------------------------------------


@dataclass
class LigneFile:
    """Une PROPOSITION signalée, pas un signalement.

    Une proposition signalée dix fois est une ligne et non dix : ce qui se décide est le
    sort de la proposition, une fois. Dix lignes feraient dix décisions à prendre pour
    un seul contenu, et neuf occasions de se contredire.
    """

    statement: Statement
    conversation: Conversation | None
    signalements: list[Signalement] = field(default_factory=list)

    @property
    def route(self) -> Route:
        """La route la plus grave parmi celles des signalements reçus.

        La gravité l'emporte ici aussi : une proposition signalée neuf fois pour doublon
        et une fois pour incitation à la violence se traite comme une ligne rouge.
        """
        return min(
            (Route(s.route) for s in self.signalements), key=lambda r: _GRAVITE[r]
        )

    @property
    def motifs_decomptes(self) -> list[tuple[str, int]]:
        """(code, combien), du plus signalé au moins signalé, à gravité égale dans
        l'ordre de la liste fermée."""
        decompte: dict[str, int] = {}
        for signalement in self.signalements:
            for code in signalement.motifs:
                decompte[code] = decompte.get(code, 0) + 1
        return sorted(
            decompte.items(), key=lambda paire: (-paire[1], regles.CODES.index(paire[0]))
        )

    @property
    def signaleurs_distincts(self) -> int:
        """Des personnes, pas des signalements — même si l'unicité en base les fait
        aujourd'hui coïncider. Le jour où elle sautera, c'est ce chiffre qu'on voudra."""
        return len({s.participant_id for s in self.signalements})

    @property
    def plus_ancien(self) -> datetime:
        return min(s.cree_le for s in self.signalements)

    @property
    def age(self) -> timedelta:
        return datetime.now(timezone.utc) - self.plus_ancien

    @property
    def retiree(self) -> bool:
        return self.statement.moderation_status is ModerationStatus.retire

    @property
    def message(self) -> str:
        """Le message qui SERAIT envoyé à l'auteur. Rien ne part dans ce lot.

        Le lien de recours porte le vrai jeton quand la proposition est retirée — c'est
        l'adresse exacte que l'auteur ouvrirait. Sur une proposition qui n'a pas été
        retirée il n'y a pas de jeton, et le message montre l'adresse générique : il n'y
        a rien à contester, et fabriquer un jeton pour l'affichage en créerait un que
        personne n'a le droit d'utiliser.
        """
        return regles.message_a_l_auteur(
            self.route,
            self.motifs_de_la_route(),
            jeton=self.statement.jeton_contestation,
        )

    def motifs_de_la_route(self) -> list[str]:
        """Les motifs qui portent la route retenue, dédoublonnés."""
        route = self.route
        codes = [
            code
            for s in self.signalements
            if Route(s.route) is route
            for code in s.motifs
        ]
        return sorted(set(codes), key=regles.CODES.index)

    @property
    def textes_libres(self) -> list[str]:
        """Les explications écrites sous « Autre raison ». Seul texte de signaleur montré."""
        return [s.texte_libre for s in self.signalements if s.texte_libre]


async def file(session: AsyncSession) -> list[LigneFile]:
    """Les propositions signalées et non traitées, groupées, **triées par gravité puis
    par ancienneté**.

    La gravité d'abord : une ligne rouge vue il y a trois minutes passe devant un doublon
    vu hier. L'ancienneté ensuite, à gravité égale, parce que c'est ce qui empêche une
    plainte de rester au fond indéfiniment.
    """
    signalements = list(
        await session.scalars(
            select(Signalement)
            .where(Signalement.statut == StatutSignalement.recu)
            .order_by(Signalement.cree_le)
        )
    )
    lignes: dict[int, LigneFile] = {}
    for signalement in signalements:
        ligne = lignes.get(signalement.statement_id)
        if ligne is None:
            statement = await session.get(Statement, signalement.statement_id)
            if statement is None:  # pragma: no cover - la cascade l'empêche
                continue
            ligne = LigneFile(
                statement=statement,
                conversation=await session.get(Conversation, statement.conversation_id),
            )
            lignes[signalement.statement_id] = ligne
        ligne.signalements.append(signalement)

    return sorted(lignes.values(), key=lambda l: (_GRAVITE[l.route], l.plus_ancien))


async def reperes(session: AsyncSession) -> tuple[int, timedelta | None]:
    """Les deux chiffres de la tête de page : combien de signalements non traités, et
    l'âge du plus ancien.

    Deux repères et pas cinq : ce sont ceux qui disent vite si le dispositif tient. Un
    nombre qui monte sans qu'aucun ne descende, ou un âge qui dépasse la journée, se
    voient d'un coup d'œil ; une moyenne ne se voit pas.
    """
    combien = await session.scalar(
        select(func.count(Signalement.id)).where(
            Signalement.statut == StatutSignalement.recu
        )
    )
    plus_ancien = await session.scalar(
        select(func.min(Signalement.cree_le)).where(
            Signalement.statut == StatutSignalement.recu
        )
    )
    age = datetime.now(timezone.utc) - plus_ancien if plus_ancien else None
    return combien or 0, age


async def non_traites(session: AsyncSession) -> int:
    """Le seul compteur, pour la barre de la file de modération."""
    combien, _ = await reperes(session)
    return combien


# --- la purge des données de contexte --------------------------------------------


async def purger_contexte(
    session: AsyncSession, jours: int = JOURS_CONTEXTE
) -> int:
    """Efface le référent et l'IP des signalements de plus de `jours`. Renvoie le compte.

    **Les deux colonnes passent à NULL, et rien d'autre ne bouge** : le signalement
    reste, son motif reste, sa route reste, son statut reste. Ce qui s'efface est ce qui
    a une durée de conservation — la donnée personnelle captée pour prévenir les abus.

    Une durée de conservation qui n'est bornée que par le hasard d'un nouvel accès n'est
    pas une durée de conservation : c'est le même raisonnement que
    `rate_limit.purge_expired`, et c'est pour cela que cette purge est une commande
    à mettre en tâche planifiée plutôt qu'un nettoyage opportuniste.
    """
    limite = datetime.now(timezone.utc) - timedelta(days=jours)
    resultat = await session.execute(
        update(Signalement)
        .where(
            Signalement.cree_le < limite,
            (Signalement.referent_hache.is_not(None))
            | (Signalement.ip_hachee.is_not(None)),
        )
        .values(referent_hache=None, ip_hachee=None)
    )
    await session.commit()
    return resultat.rowcount or 0


# --- la mesure, pour `rapport-signalements` ---------------------------------------


async def mesure(session: AsyncSession) -> dict:
    """Tout ce que le rapport affiche, en une passe. Aucune écriture."""
    total = await session.scalar(select(func.count(Signalement.id))) or 0

    par_motif: dict[str, int] = {code: 0 for code in regles.CODES}
    for colonne in (Signalement.motif_1, Signalement.motif_2):
        lignes = await session.execute(
            select(colonne, func.count()).where(colonne.is_not(None)).group_by(colonne)
        )
        for code, combien in lignes:
            par_motif[code] = par_motif.get(code, 0) + combien

    par_route = {
        route: await session.scalar(
            select(func.count(Signalement.id)).where(Signalement.route == route.value)
        )
        or 0
        for route in Route
    }
    par_statut = {
        statut: await session.scalar(
            select(func.count(Signalement.id)).where(Signalement.statut == statut)
        )
        or 0
        for statut in StatutSignalement
    }

    avec_compte = (
        await session.scalar(
            select(func.count(Signalement.id))
            .join(Participant, Participant.id == Signalement.participant_id)
            .where(Participant.user_id.is_not(None))
        )
        or 0
    )

    delais = list(
        await session.scalars(
            select(
                func.extract("epoch", Signalement.traite_le - Signalement.cree_le)
            ).where(Signalement.traite_le.is_not(None))
        )
    )

    plus_signalees = list(
        await session.execute(
            select(Signalement.statement_id, func.count(Signalement.id).label("combien"))
            .group_by(Signalement.statement_id)
            .order_by(func.count(Signalement.id).desc(), Signalement.statement_id)
            .limit(10)
        )
    )
    propositions = []
    for statement_id, combien in plus_signalees:
        statement = await session.get(Statement, statement_id)
        propositions.append((statement_id, combien, statement))

    combien_non_traites, age = await reperes(session)
    return {
        "total": total,
        "par_motif": par_motif,
        "par_route": par_route,
        "par_statut": par_statut,
        "avec_compte": avec_compte,
        "sans_compte": total - avec_compte,
        "delais_secondes": sorted(float(d) for d in delais),
        "plus_signalees": propositions,
        "non_traites": combien_non_traites,
        "age_du_plus_ancien": age,
        "avec_contexte": await session.scalar(
            select(func.count(Signalement.id)).where(
                Signalement.ip_hachee.is_not(None)
            )
        )
        or 0,
    }


__all__ = [
    "JOURS_CONTEXTE",
    "age_lisible",
    "LigneFile",
    "PlafondAtteint",
    "PropositionNonSignalable",
    "SignalementInvalide",
    "annuler_retrait",
    "classer_sans_suite",
    "confirmer_retrait",
    "deposer",
    "file",
    "mesure",
    "non_traites",
    "purger_contexte",
    "reperes",
]


def age_lisible(age: timedelta | None) -> str | None:
    """Une durée en français court : « 3 min », « 5 h », « 2 jours ».

    Approximative, et c'est voulu : ce que dit ce repère est « est-ce que ça traîne »,
    pas « depuis combien de secondes exactement ». Une précision à la seconde donnerait
    un chiffre qui bouge à chaque rechargement, et qu'on cesserait de lire.
    """
    if age is None:
        return None
    minutes = int(age.total_seconds() // 60)
    if minutes < 1:
        return "à l'instant"
    if minutes < 60:
        return f"{minutes} min"
    heures = minutes // 60
    if heures < 24:
        return f"{heures} h"
    jours = heures // 24
    return "1 jour" if jours == 1 else f"{jours} jours"
