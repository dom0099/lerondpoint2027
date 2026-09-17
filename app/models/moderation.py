"""Le signalement d'une proposition publiée, et le journal d'audit de la modération.

Deux tables, deux natures opposées, et c'est ce qui justifie de les tenir dans le même
module : `signalement` est une **plainte** — elle a un statut, elle se traite, elle se
classe ; `journal_moderation` est un **constat** — il s'ajoute et ne bouge plus jamais.
Les lire côte à côte évite de confondre ce qui a été demandé et ce qui a été fait.

La liste fermée des motifs et la règle de routage vivent dans
`app/services/signalement.py` ; ce qui touche la base est dans
`app/services/signalement_file.py`.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DDL,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class StatutSignalement(str, enum.Enum):
    """Où en est le signalement, du point de vue du responsable.

    Trois valeurs et pas quatre : il n'existe pas d'état « rejeté ». Le signaleur reçoit
    toujours la même réponse — « merci, c'est enregistré » — et n'apprend jamais le sort
    de la proposition. Un état qui s'appellerait « rejeté » finirait par se montrer
    quelque part, et le signalement deviendrait un jeu où l'on compte les points.
    """

    recu = "recu"
    traite = "traite"
    classe_sans_suite = "classe_sans_suite"


class StatutReformulation(str, enum.Enum):
    """Où en est un échange de reformulation (MOD-10).

    Trois valeurs et pas quatre : il n'existe pas d'état « caduque ». Une proposition
    retirée entre-temps ne rend pas sa reformulation caduque *en base* — elle la rend
    inatteignable, ce que la garde du service dit au moment où l'on essaie. Un état que
    personne n'écrit finit toujours par mentir sur ce qu'il décrit.
    """

    proposee = "proposee"
    acceptee = "acceptee"
    refusee = "refusee"


class VerdictValidation(str, enum.Enum):
    """La réponse d'un participant sollicité pour valider une proposition (MOD-4).

    **Deux valeurs, et il n'y en aura jamais une troisième.** « Passer » n'est pas un
    verdict : écarter la carte n'écrit aucune ligne. Un troisième code — « sans avis »,
    « je ne sais pas » — transformerait une abstention en jugement, et le taux d'accord
    dont le MOD-6 a besoin compterait comme désaccord le fait de n'avoir pas voulu
    trancher.

    Le vocabulaire est celui du cadrage et pas un autre : **conforme** / **à revoir**,
    jamais « accepté » / « rejeté ». Les seconds laisseraient croire à un verdict qui
    décide du sort de la proposition — or la publication ne dépend de rien ici.
    """

    conforme = "conforme"
    a_revoir = "a_revoir"


class ActeModeration(str, enum.Enum):
    """Les actes journalisés. Quatre dans ce lot, pas un de plus.

    La liste est volontairement courte : ce journal doit pouvoir être relu par quelqu'un
    qui conteste une décision, pas servir de trace de débogage. Tout ce qui s'y ajoutera
    plus tard (reformulation, appel, habilitation) viendra avec le lot qui le produit.
    """

    retrait_conservatoire = "retrait_conservatoire"
    retrait_confirme = "retrait_confirme"
    retrait_annule = "retrait_annule"
    classement_sans_suite = "classement_sans_suite"
    #: MOD-3b. L'auteur d'une proposition retirée a demandé un réexamen. C'est un acte
    #: de l'auteur et non du responsable — le seul du journal à ce jour — d'où
    #: `auteur = "auteur de la proposition"` : on ne journalise pas son identité, qui
    #: est le plus souvent un jeton de session, et le jeton de recours n'y entre jamais.
    contestation_deposee = "contestation_deposee"

    #: MOD-10, les trois actes de la navette. Le commentaire de cette classe annonçait
    #: qu'ils viendraient « avec le lot qui les produit » : c'est celui-ci.
    #:
    #: Les deux derniers sont des actes de l'AUTEUR, comme `contestation_deposee` :
    #: même convention, même raison — son identité n'entre pas dans le journal.
    #:
    #: **`reformulation_acceptee` est le seul acte du journal qui modifie un texte déjà
    #: voté.** Il porte donc, dans son motif, le texte d'origine : c'est la trace qui
    #: rend relisible ce sur quoi les votes conservés ont réellement porté.
    reformulation_proposee = "reformulation_proposee"
    reformulation_acceptee = "reformulation_acceptee"
    reformulation_refusee = "reformulation_refusee"


#: Ce qu'écrit `auteur` quand l'acte n'a pas d'auteur humain.
AUTEUR_SYSTEME = "systeme"

#: Ce qu'écrit `auteur` pour une contestation. **Pas l'identité de l'auteur** : elle est
#: un jeton de session dans 98 % des cas, et la journaliser reviendrait à archiver un
#: identifiant de navigateur dans une table qu'on ne peut plus jamais modifier.
AUTEUR_DE_LA_PROPOSITION = "auteur de la proposition"


class Contestation(Base):
    """La demande de réexamen d'un auteur dont la proposition a été retirée (MOD-3b).

    **Ce qui rend la voie de recours réelle plutôt qu'une phrase en bas d'un message.**
    Dès qu'un retrait conservatoire devient visible par son auteur, l'exposé des motifs
    et le recours sont dus — c'est pourquoi cette table arrive avec le lot qui rend le
    retrait atteignable, et pas plus tard.

    Aucune décision automatique n'en découle, et la page qui l'alimente ne promet aucun
    délai : on n'écrit pas « sous 48 heures » tant que personne ne garantit 48 heures.
    Elle remonte en tête de `/moderation/signalements`, et c'est tout ce qu'elle fait.

    Le droit d'accès est le **jeton** porté par la proposition
    (`Statement.jeton_contestation`), jamais un compte : l'auteur est anonyme dans la
    quasi-totalité des cas. Le jeton n'est pas recopié ici — le connaître a permis
    d'écrire la ligne, le garder à côté n'ajouterait qu'un endroit de plus où il fuit.
    """

    __tablename__ = "contestation"

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("statement.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Ce que l'auteur écrit pour sa défense. 1 000 signes : deux fois la longueur d'une
    #: proposition, ce qui laisse la place d'expliquer sans ouvrir un canal de discussion.
    texte: Mapped[str] = mapped_column(String(1000), nullable=False)
    cree_le: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    #: Renseigné quand le responsable a regardé. Aucun statut plus fin : ce lot
    #: n'instruit pas les contestations, il les recueille et les rend visibles.
    traite_le: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Contestation {self.id} #{self.statement_id}>"


class Signalement(Base):
    """Ce qu'un lecteur émet sur une proposition déjà publiée.

    **Un seul signalement par (proposition, identité).** Une seconde tentative renvoie
    un succès idempotent et non une erreur : dire « vous avez déjà signalé » informerait
    le signaleur de l'état du dossier, ce que le §4 interdit — et, sur un contenu
    réellement choquant, un message d'erreur donne l'impression que rien n'a été pris.
    """

    __tablename__ = "signalement"
    __table_args__ = (
        # L'idempotence est portée par la BASE et non par une vérification applicative,
        # qui laisserait passer deux requêtes concurrentes (double clic, réseau lent).
        #
        # Réserve à connaître : en SQL, NULL n'est égal à rien, pas même à NULL. Cette
        # contrainte ne dédoublonne donc PAS les lignes à `participant_id` nul. Elle
        # tient quand même, parce qu'au dépôt le participant est toujours connu — un
        # visiteur sans compte en reçoit un, anonyme, exactement comme pour voter. La
        # colonne n'est nullable que pour survivre à la suppression d'un compte.
        UniqueConstraint("statement_id", "participant_id", name="uq_signalement_identite"),
        CheckConstraint(
            "motif_2 IS NULL OR motif_2 <> motif_1", name="ck_signalement_motifs_distincts"
        ),
        # La file du responsable lit « les signalements encore à traiter, les plus
        # anciens d'abord ». Partiel, comme l'index de la file de nommage : les lignes
        # traitées et classées ne sont jamais lues par ce chemin.
        Index(
            "ix_signalement_a_traiter",
            "cree_le",
            postgresql_where=text("statut = 'recu'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("statement.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NULLABLE, et ce n'est pas « le signalement anonyme ». **Un visiteur sans compte
    #: peut signaler** — le DSA l'impose — mais il est identifié comme pour voter : par
    #: la ligne `participant` attachée à son jeton de cookie (`app/auth/deps.py`).
    #: Aucune seconde identification n'a été inventée : ce qui plafonne, dédoublonne et
    #: se fusionne à la création d'un compte est déjà celle-là. La colonne est nullable
    #: parce que la suppression d'un compte efface son participant, et qu'un signalement
    #: ne doit pas disparaître avec la personne qui l'a émis.
    participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participant.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Deux colonnes plutôt qu'un JSON : le plafond de deux devient structurel, et les
    #: requêtes du rapport (§5) restent des `GROUP BY` ordinaires. Le code court, comme
    #: `conversation_theme.code`, sans clé étrangère : la liste vit en constante Python.
    motif_1: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    motif_2: Mapped[str | None] = mapped_column(String(32), nullable=True)

    texte_libre: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: La route **figée au moment du signalement**, et non recalculée à la lecture. Si
    #: la table des motifs change demain, ce qui a été décidé hier doit rester lisible
    #: tel qu'il a été décidé — c'est la première chose que demandera quelqu'un qui
    #: conteste.
    route: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    cree_le: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    #: Contexte pour la détection d'afflux coordonné (MOD-5). Condensés salés et
    #: tronqués, **jamais en clair**, purgés à 30 jours par
    #: `python -m app.cli purge-contexte-signalements`. Voir `services/signalement.py`.
    referent_hache: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ip_hachee: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: MOD-4. **Ce signalement a-t-il été provoqué par le site, ou déposé spontanément ?**
    #:
    #: Vrai quand il naît d'un « proposition à revoir » rendu par quelqu'un que le site
    #: avait lui-même sollicité au hasard. Faux pour un signalement ordinaire, déposé
    #: par quelqu'un qui a cliqué « Signaler » de sa propre initiative.
    #:
    #: **C'est la colonne la plus importante de ce lot, et elle ne pouvait pas attendre.**
    #: Sans elle, l'indice d'indépendance du MOD-5 et la détection de tempête compteraient
    #: comme afflux coordonné ce que le site a lui-même provoqué : cinq validations
    #: sollicitées sur la même proposition, dans la même heure, par cinq identités
    #: fraîches qui n'ont pas voté ce débat, c'est exactement la signature qu'ils
    #: cherchent. La détection se déclencherait sur son propre sondage.
    #:
    #: `app/services/independance_lecture.py` écarte donc les sollicités, et un test le
    #: tient. Le rapport du responsable, lui, les montre — il faut bien qu'on sache ce
    #: que la validation produit.
    #:
    #: NOT NULL avec un défaut à faux : les signalements déjà en base sont tous
    #: spontanés, puisque rien ne sollicitait personne avant ce lot.
    sollicite: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False, index=True
    )

    statut: Mapped[StatutSignalement] = mapped_column(
        Enum(StatutSignalement, name="statut_signalement"),
        default=StatutSignalement.recu,
        server_default=StatutSignalement.recu.value,
        nullable=False,
        index=True,
    )
    traite_par: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    traite_le: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: MOD-15. Quand l'auteur de la proposition a écarté le rappel né de ce signalement.
    #: NULL vaut « jamais », comme `statement.rappel_ferme_le` au MOD-6 et `traite_le`
    #: au MOD-13.
    #:
    #: **Elle est ici et non sur `statement`, délibérément.** Le rappel existe à cause
    #: d'un signalement : il doit s'éteindre avec celui-là et renaître avec le suivant.
    #: Sur la proposition, la fermeture vaudrait pour toujours, et un signalement déposé
    #: des mois plus tard n'avertirait plus personne. Voir la migration 0023.
    #:
    #: **C'est un geste de l'auteur, pas du responsable** : rien ici ne touche `statut`,
    #: qui dit où en est le dossier côté modération. Écarter un rappel n'est pas traiter
    #: un signalement, et rien n'est écrit au journal d'audit — lire n'est pas un acte,
    #: c'est la règle posée au MOD-13.
    rappel_ferme_le: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def motifs(self) -> list[str]:
        """Les motifs de ce signalement, un ou deux, dans l'ordre des colonnes."""
        return [self.motif_1] + ([self.motif_2] if self.motif_2 else [])

    def __repr__(self) -> str:
        return f"<Signalement {self.id} #{self.statement_id} {self.route}>"


class Validation(Base):
    """Ce qu'un participant sollicité au hasard répond sur la proposition d'un autre (MOD-4).

    **Une validation n'est pas un signalement, et les deux tables ne se confondent
    pas.** Un signalement est une plainte : quelqu'un a été dérangé par un texte et l'a
    dit. Une validation est une réponse à une QUESTION DU SITE : on a demandé à
    quelqu'un de regarder, il a regardé. Les ranger ensemble reviendrait à dire que le
    site se plaint tout seul de ses propres propositions — et c'est exactement l'erreur
    que la colonne `Signalement.sollicite` existe pour empêcher côté MOD-5.

    **Les « conforme » sont enregistrés eux aussi, et c'est le point du lot.** Ne garder
    que les « à revoir » ferait une table de plaintes de plus, qui ne rebrancherait rien :
    ce sont les « conforme » qui constituent la matière des habilitations du MOD-6 — un
    historique de jugements par personne, et un taux d'accord avec la décision finale.
    Une table qui ne garderait que les désaccords ne permettrait de calculer aucun taux.

    **Ce qu'elle ne porte pas.** Aucun lien vers le signalement qu'un « à revoir »
    engendre : les deux tables se joignent déjà sur `(statement_id, participant_id)`, qui
    est unique de part et d'autre, et une colonne de plus serait une seconde vérité à
    tenir. Aucun statut non plus — une validation ne se traite pas, ne se classe pas et
    ne se conteste pas. Elle est un constat, comme une ligne de journal ; ce qui se
    traite, c'est le signalement qu'elle produit le cas échéant.

    **Aucune donnée de contexte** — ni référent, ni IP, contrairement au signalement.
    Elles n'y serviraient à rien : le MOD-5 cherche des afflux coordonnés, et une
    validation ne peut pas être coordonnée puisque c'est le site qui choisit à qui il la
    propose et sur quoi. Collecter deux empreintes de plus « au cas où » serait
    précisément ce que le §7 du MOD-5 s'interdisait.
    """

    __tablename__ = "validation"
    __table_args__ = (
        # **Une seule validation par (proposition, identité).** Portée par la base et non
        # par une vérification applicative, qui laisserait passer deux requêtes
        # concurrentes — un double clic sur « Conforme » suffirait à compter deux avis.
        #
        # Même réserve qu'au signalement : NULL n'est égal à rien en SQL, donc la
        # contrainte ne dédoublonne pas les lignes à `participant_id` nul. Elle tient
        # quand même, parce qu'au moment de la réponse le participant est toujours connu ;
        # la colonne n'est nullable que pour survivre à la suppression d'un compte.
        UniqueConstraint("statement_id", "participant_id", name="uq_validation_identite"),
        # Le tirage demande « combien de validations porte déjà cette proposition ». Un
        # index sur la seule colonne `statement_id` répond, et il sert à chaque carte.
        Index("ix_validation_proposition", "statement_id"),
        # Le plafond par personne lit « les validations de cette identité depuis 24 h ».
        Index("ix_validation_identite_date", "participant_id", "cree_le"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("statement.id", ondelete="CASCADE"), nullable=False
    )
    #: NULLABLE, et pour la raison exacte du signalement : la suppression d'un compte
    #: efface son participant, et la validation ne doit pas disparaître avec la personne
    #: qui l'a rendue — sans quoi le taux d'accord d'un débat se réécrirait tout seul.
    #:
    #: **Tout participant, compte ou non.** Décision de dom, 15 septembre 2026 :
    #: l'identification est celle du vote et du signalement, la ligne `participant`
    #: attachée au jeton de cookie. C'est la seule qui produise du volume — 164
    #: participants sur 167 n'ont pas de compte. Le jour où le MOD-6 habilitera
    #: quelqu'un, il ne retiendra que les comptes vérifiés, et la jointure suffit : rien
    #: n'a besoin d'être recopié ici.
    participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participant.id", ondelete="SET NULL"), nullable=True
    )

    verdict: Mapped[VerdictValidation] = mapped_column(
        Enum(VerdictValidation, name="verdict_validation"), nullable=False, index=True
    )

    cree_le: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<Validation {self.id} #{self.statement_id} {self.verdict}>"


class JournalModeration(Base):
    """Le journal d'audit de la modération. **En ajout seul.**

    Aucune route, aucune fonction de service, aucune commande ne peut modifier ou
    supprimer une ligne — et ce n'est pas qu'une intention : un déclencheur PostgreSQL
    (`JOURNAL_EN_AJOUT_SEUL`, plus bas) refuse tout `UPDATE` et tout `DELETE` sur cette
    table, y compris depuis `psql`. `tests/test_signalement.py` le vérifie.

    **Aucune clé étrangère, nulle part, et c'est la conséquence directe de la règle.**
    Un `ON DELETE CASCADE` supprimerait des lignes ; un `ON DELETE SET NULL` en
    modifierait. Supprimer une proposition ou un compte effacerait donc précisément la
    trace de ce qui lui est arrivé — le jour où quelqu'un conteste, c'est ce qu'on
    cherche. `cible_id` et `auteur` sont donc des valeurs recopiées, pas des liens.
    """

    __tablename__ = "journal_moderation"
    __table_args__ = (
        Index("ix_journal_moderation_cible", "cible_type", "cible_id"),
        Index("ix_journal_moderation_horodatage", "horodatage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    acte: Mapped[ActeModeration] = mapped_column(
        Enum(ActeModeration, name="acte_moderation"), nullable=False, index=True
    )
    #: « statement », « signalement ». Une chaîne et non une énumération : le jour où un
    #: acte porte sur une cible d'un type neuf, le journal doit pouvoir l'enregistrer
    #: sans migration — un journal qui refuse d'écrire est un journal qui ment.
    cible_type: Mapped[str] = mapped_column(String(32), nullable=False)
    cible_id: Mapped[int] = mapped_column(Integer, nullable=False)
    #: L'adresse du compte qui a agi, ou `systeme` pour un acte automatique. Une valeur
    #: recopiée : le retrait conservatoire est TOUJOURS l'œuvre du système, et une clé
    #: étrangère vers `user` serait nulle exactement là où l'information compte le plus.
    auteur: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Ce qui a motivé l'acte : un code de motif, ou une phrase. Nullable — annuler un
    #: retrait n'a pas de motif au sens de la liste fermée.
    motif: Mapped[str | None] = mapped_column(String(500), nullable=True)
    horodatage: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<JournalModeration {self.id} {self.acte} {self.cible_type}#{self.cible_id}>"


class Reformulation(Base):
    """Une reformulation proposée à l'auteur d'une proposition signalée (MOD-10).

    **C'est un échange, pas un champ.** Quelqu'un propose, l'auteur répond. Il y a donc un
    émetteur, une date, un texte, une réponse et une date de réponse — et l'échange
    survit à son acceptation. Écrasé dans la proposition, il n'en resterait que le
    résultat, et c'est exactement ce qu'il ne faut pas perdre.

    **Un seul reformulateur pour l'instant, donc un seul candidat.** dom a tranché le
    15 septembre 2026 : en attendant des arbitres tirés au sort (MOD-7, MOD-12), c'est le
    responsable qui propose. Plusieurs propositions concurrentes sur un même texte
    seraient une élection, et une élection demande le module d'agrégation de classements
    du MOD-9 — qui attend toujours son premier appelant, et dont un test garde l'absence.
    """

    __tablename__ = "reformulation"
    __table_args__ = (
        # Une seule EN ATTENTE par proposition. Partiel, comme `ix_signalement_a_traiter` :
        # les échanges clos ne sont jamais lus par ce chemin, et rien n'interdit qu'une
        # proposition en connaisse plusieurs à la suite.
        Index(
            "uq_reformulation_en_attente",
            "statement_id",
            unique=True,
            postgresql_where=text("statut = 'proposee'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("statement.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Le texte tel qu'il était **au moment de la proposition**.
    #:
    #: **C'est la colonne qui rend tenable la décision de dom** : les votes déjà portés
    #: sur une proposition reformulée restent valables. Cela n'est honnête qu'à une
    #: condition — que le texte sur lequel ces gens ont réellement voté reste lisible.
    #: Recopié ici au moment de la proposition, et non reconstruit après coup.
    texte_origine: Mapped[str] = mapped_column(Text, nullable=False)

    #: Ce qui est proposé à l'auteur. Même plafond qu'une proposition ordinaire : une
    #: reformulation qui ne tiendrait pas dans le champ de dépôt ne serait pas une
    #: reformulation de la même chose.
    texte_propose: Mapped[str] = mapped_column(String(500), nullable=False)

    propose_par: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    propose_le: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    statut: Mapped[StatutReformulation] = mapped_column(
        Enum(StatutReformulation, name="statut_reformulation"),
        default=StatutReformulation.proposee,
        server_default=StatutReformulation.proposee.value,
        nullable=False,
    )
    #: Quand l'auteur a répondu. NULL vaut « pas encore », comme partout ailleurs dans ce
    #: chantier (`rappel_ferme_le` au MOD-6, `traite_le` au MOD-13).
    repondu_le: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Reformulation {self.id} #{self.statement_id} {self.statut}>"


#: Le déclencheur qui rend l'ajout seul opposable au serveur, et non à la relecture.
#:
#: Posé par un événement SQLAlchemy plutôt que par la seule migration : les tests
#: construisent leur schéma avec `Base.metadata.create_all` (voir `tests/conftest.py`),
#: qui ne rejoue aucune migration. Sans cela, la garantie existerait en production et
#: pas là où on la vérifie — ce qui est la pire des deux situations.
JOURNAL_EN_AJOUT_SEUL: tuple[str, str] = (
    """
    CREATE OR REPLACE FUNCTION journal_moderation_ajout_seul() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'journal_moderation est en ajout seul : ni UPDATE ni DELETE';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER journal_moderation_ajout_seul
        BEFORE UPDATE OR DELETE ON journal_moderation
        FOR EACH ROW EXECUTE FUNCTION journal_moderation_ajout_seul()
    """,
)

# Deux ordres et deux événements, et non un seul bloc : le pilote asyncpg prépare
# chaque énoncé, et refuse d'en préparer un qui en contient plusieurs
# (« cannot insert multiple commands into a prepared statement »).
for _ordre in JOURNAL_EN_AJOUT_SEUL:
    event.listen(
        JournalModeration.__table__,
        "after_create",
        DDL(_ordre).execute_if(dialect="postgresql"),
    )
