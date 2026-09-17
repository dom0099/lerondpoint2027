"""Les décisions de nommage des groupes d'opinion (chantier E1).

**Une ligne par DÉCISION, pas par groupe.** La table est un journal, et c'est délibéré :
ce que le E1 doit produire n'est pas un état mais un CHIFFRE — combien de fois la règle
« dès que nécessaire » se déclenche réellement sur de vrais débats. C'est ce chiffre qui
commande l'infrastructure de tout le chantier E, et personne ne le connaît aujourd'hui
(voir `chantier-e-decisions.md`, §2 et §8). Un état écrasé à chaque tour ne l'aurait pas
donné ; un journal le donne en comptant ses lignes.

L'état, lui, se lit comme la DERNIÈRE ligne d'un groupe : c'est elle qui porte les
déclarations ayant justifié le nom en cours, et l'instant d'où court le délai de garde.

**Aucun nom n'est écrit ici pour l'instant, et la colonne existe quand même.** Le E1
tourne sans LLM : il enregistre qu'un groupe *aurait été* nommé, et pourquoi. `nom` et
`justification` restent nuls jusqu'au E4, qui les remplira sans migration ni changement
de forme — le point de rendez-vous entre un calcul qui tourne toutes les dix minutes et
un nom qui change rarement est déjà celui-ci.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.conversation import ModerationStatus


class MotifNommage(str, enum.Enum):
    """Pourquoi la règle s'est déclenchée. Les deux seuls cas du §5 du E0.

    Les compter séparément est ce qui rendra la mesure lisible : un débat jeune produit
    surtout des `nouveau` (chaque groupe est nommé une fois), un débat installé ne
    produit que des `changement`. Confondus, les deux régimes donneraient une moyenne
    qui ne décrirait ni l'un ni l'autre.
    """

    nouveau = "nouveau"        # jamais nommé, et son identité tient depuis assez longtemps
    changement = "changement"  # ce qui le caractérise a changé


class GroupNaming(Base):
    __tablename__ = "group_naming"

    id: Mapped[int] = mapped_column(primary_key=True)

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    #: L'identité STABLE du groupe (appariement du C6), jamais l'étiquette de k-means.
    #: C'est ce qui fait qu'un nom survit à un recalcul : « groupe B » reste le même
    #: groupe d'un calcul à l'autre, seule son étiquette brute change.
    stable_group_id: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Le calcul qui a déclenché la décision. Sert à remonter à la source quand un nom
    #: surprend : on retrouve les votes, la projection et la `repness` de ce tour-là.
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="SET NULL"), nullable=True
    )

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    motif: Mapped[MotifNommage] = mapped_column(
        Enum(MotifNommage, name="motif_nommage"), nullable=False
    )

    #: Ce qui caractérisait le groupe au moment de cette décision : une liste de
    #: couples `[identifiant de déclaration, sens]`, du plus au moins représentatif.
    #: C'est l'ensemble que le tour suivant comparera pour savoir si ce qui caractérise
    #: le groupe a changé — donc la donnée dont dépend toute la règle.
    #:
    #: **Le sens fait partie de la clé, et ce n'est pas une précaution théorique.**
    #: Relevé sur le débat du permis à 16 ans (calcul 27, 6 septembre) : les deux
    #: groupes d'opinion partagent quatre déclarations représentatives sur cinq, avec
    #: des sens exactement inverses. Un groupe qui retournerait sa position sur toutes
    #: ses déclarations — le changement le plus radical qui soit — laisserait donc
    #: l'ensemble des identifiants rigoureusement identique. Comparés nus, ils
    #: n'auraient rien déclenché, et le nom serait resté pendant que le groupe devenait
    #: son contraire.
    #:
    #: Stockés en JSONB et non dans une table de liaison : on ne les interroge jamais
    #: un par un, on compare deux ensembles entiers. Une table de liaison aurait ajouté
    #: cinq lignes et une jointure par décision pour un service que `set()` rend mieux.
    declarations: Mapped[list] = mapped_column(JSONB, nullable=False)

    #: **Cette table EST la file d'attente du E3, et `nom IS NULL` est son état
    #: « en attente ».** Aucune table de file n'a été ajoutée : la décision de nommer et
    #: la demande de nom sont le même fait, et les séparer aurait créé deux vérités à
    #: tenir d'accord. Un consommateur unique vide la file à son rythme.
    #:
    #: Remplis au E4, quand le modèle répond. Nuls pendant tout le E1 : la ligne disait
    #: alors « ce groupe aurait été nommé maintenant, à cause de ceci ».
    nom: Mapped[str | None] = mapped_column(String(120), nullable=True)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Quand le nom a été produit. **C'est la mesure que le E3 a rendue obligatoire** :
    #: `named_at - decided_at` est le délai entre l'empilement d'une demande et sa
    #: production, et le seuil de réexamen de l'architecture est écrit dessus — au-delà
    #: de deux heures de médiane sur une semaine, la file ne tient plus le rythme.
    #:
    #: Deux heures parce que c'est l'ordre de grandeur du délai de modération humaine :
    #: en deçà, le nommage reste invisible dans la chaîne ; au-delà, il en devient le
    #: goulot. Sans cette colonne, le seuil serait un vœu.
    named_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Combien de fois on a essayé de produire ce nom. Une ligne qui échoue trois fois
    #: est abandonnée plutôt que réessayée sans fin : sans ce compteur, une déclaration
    #: qui fait suffoquer le modèle bloquerait la file pour toutes les autres, et
    #: l'échec silencieux exigé par le plan deviendrait un silence total.
    tentatives: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    #: La dernière erreur rencontrée, pour qu'un abandon soit explicable après coup.
    #: Le E1 avait la même exigence sur `analysis_run.error_text` : un modérateur doit
    #: pouvoir savoir POURQUOI l'affichage ne bouge pas.
    erreur: Mapped[str | None] = mapped_column(Text, nullable=True)

    ###### Modération avant affichage (chantier E5) ######
    #: **`nom` n'est jamais modifié, et c'est tout l'intérêt de cette seconde colonne.**
    #: Le modérateur peut corriger avant de valider ; ce qu'il écrit va ici, ce que le
    #: modèle a produit reste là-bas. Trois raisons, dans l'ordre :
    #:
    #: 1. la mention publique du E6 doit pouvoir distinguer « généré par IA, validé par
    #:    un modérateur » de « généré par IA, corrigé et validé » — sans les deux
    #:    colonnes, elle mentirait un peu, et sur un outil de dialogue citoyen une
    #:    mention de transparence approximative vaut moins que pas de mention ;
    #: 2. mesurer la qualité réelle du modèle demande de savoir ce qu'il a écrit, pas ce
    #:    qu'un humain en a fait. Écraser `nom` effacerait la seule trace utilisable ;
    #: 3. un correctif systématique sur les mêmes tournures est un signal sur l'invite,
    #:    et il ne se lit que par comparaison.
    nom_valide: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: `pending` tant qu'un humain n'a pas tranché. **Rien ne s'affiche avant
    #: `approved`** : c'est la décision du 3 septembre, et c'est ce que le E6 lira.
    statut: Mapped[ModerationStatus] = mapped_column(
        Enum(ModerationStatus, name="moderation_status", create_type=False),
        nullable=False,
        server_default=ModerationStatus.pending.value,
    )
    #: UUID et non entier : les comptes viennent de `fastapi-users`, dont la clé est un
    #: UUID depuis le C1. `SET NULL` plutôt que `CASCADE` — un modérateur qui supprime
    #: son compte ne doit pas emporter les décisions qu'il a prises, sans quoi
    #: l'historique de modération se réécrirait tout seul.
    modere_par_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    modere_le: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def corrige(self) -> bool:
        """Le modérateur a-t-il réécrit le nom, ou seulement validé celui du modèle ?"""
        return bool(
            self.nom_valide and self.nom and self.nom_valide.strip() != self.nom.strip()
        )


#: L'index sert la seule requête chaude : « la dernière décision de ce groupe », posée
#: une fois par groupe et par calcul. `decided_at` décroissant y est inclus pour que le
#: `LIMIT 1` se serve dans l'index plutôt que de trier les décisions d'un groupe âgé.
Index(
    "ix_group_naming_derniere",
    GroupNaming.conversation_id,
    GroupNaming.stable_group_id,
    GroupNaming.decided_at.desc(),
)
