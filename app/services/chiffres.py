"""Les chiffres publics d'un débat : lecture, saisie, et mise en forme des dates.

Un chiffre est saisi **à la main** par un modérateur. Aucune récupération automatique :
ni data.gouv.fr, ni l'API de l'Insee. Une API publique change de schéma sans prévenir,
et le jour où elle le fait, un site politique publie un chiffre faux avec une source
officielle en dessous — le pire des deux mondes. Un modérateur qui recopie un chiffre
le lit ; un script ne lit rien.

Ce module tient les deux règles qui font la valeur de la page :

  1. **deux dates, jamais une** — celle de la donnée, celle de la vérification ;
  2. **aucun jugement de fraîcheur automatique** (voir `MOIS` plus bas).
"""

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, ConversationSource
from app.services.liens import LienInvalide, valider_lien

TITRE_MAX = 300
VALEUR_MAX = 120

#: Les mois en toutes lettres. Une date de donnée se lit, elle ne se déchiffre pas :
#: « 01/2024 » et « 2024-01-01 » demandent tous deux un effort que « janvier 2024 »
#: n'exige pas, sur une page dont l'objet est de dissiper une confusion.
MOIS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


def en_toutes_lettres(quand: date | None) -> str | None:
    """« 12 mars 2024 ». None reste None — l'absence se dit ailleurs, pas ici."""
    if quand is None:
        return None
    return f"{quand.day} {MOIS[quand.month - 1]} {quand.year}"


#: Un chiffre en pourcentage entier, ex. « 40 % », « 40% », « 40 pour cent »,
#: « 40 pourcents » — cette dernière forme est celle de plusieurs chiffres déjà en
#: base (relevés avant ce chantier), le pluriel fautif compris.
_POURCENTAGE = re.compile(r"^\s*(\d{1,3})\s*(?:%|pour ?cents?)\s*$", re.IGNORECASE)


def pourcentage(valeur: str) -> int | None:
    """0-100 si `valeur` EST un pourcentage entier, sinon None.

    `valeur` est du texte libre saisi par un modérateur (« 40 % », mais aussi
    « 1 284 » ou « 35 € ») : rien ne garantit que c'est un pourcentage. Sert
    uniquement à décider si le petit graphique en gaufre du chantier I a un sens ici
    — un chiffre qui n'en est pas un reste affiché en grand, sans graphique inventé.
    """
    trouve = _POURCENTAGE.match(valeur)
    if not trouve:
        return None
    nombre = int(trouve.group(1))
    return nombre if 0 <= nombre <= 100 else None


def date_saisie(valeur: str | None) -> date | None:
    """Lit une date d'un champ `type="date"` (AAAA-MM-JJ). Vide = pas de date.

    Une saisie illisible est traitée comme absente plutôt que refusée : le champ est
    facultatif, et un navigateur qui n'implémente pas `type="date"` laisse passer du
    texte libre. Refuser aurait fait perdre le reste du formulaire pour un champ dont
    l'absence est prévue.
    """
    valeur = (valeur or "").strip()
    if not valeur:
        return None
    try:
        return date.fromisoformat(valeur)
    except ValueError:
        return None


def valider_chiffre(
    titre: str, valeur: str, url: str
) -> tuple[str, str, str]:
    """Contrôle une saisie de modérateur. Lève `ValueError` avec un message français.

    L'adresse passe par le **même** validateur que les liens de participants : un
    chiffre officiel accompagné d'un `javascript:` reste un `javascript:`, et le
    modérateur n'est pas plus à l'abri d'un copier-coller malheureux qu'un visiteur.
    """
    titre = (titre or "").strip()
    valeur = (valeur or "").strip()
    if not titre:
        raise ValueError("un intitulé est nécessaire")
    if len(titre) > TITRE_MAX:
        raise ValueError(f"intitulé trop long (maximum {TITRE_MAX} caractères)")
    if not valeur:
        raise ValueError("un chiffre est nécessaire")
    if len(valeur) > VALEUR_MAX:
        raise ValueError(f"chiffre trop long (maximum {VALEUR_MAX} caractères)")
    try:
        lien = valider_lien(url)
    except LienInvalide as exc:
        raise ValueError(str(exc)) from None
    if lien is None:
        raise ValueError("une source est nécessaire : un chiffre sans source n'en est pas un")
    return titre, valeur, lien.url


async def combien(session: AsyncSession, conversation: Conversation) -> int:
    """Compte les chiffres d'un débat, pour décider d'afficher le lien vers la page.

    Un lien vers une page vide est une impasse : il promet des chiffres, et n'en montre
    aucun. Compter coûte une requête ; la promesse non tenue coûte la confiance.
    """
    from sqlalchemy import func

    return await session.scalar(
        select(func.count(ConversationSource.id)).where(
            ConversationSource.conversation_id == conversation.id
        )
    ) or 0


async def chiffres_de(
    session: AsyncSession, conversation: Conversation
) -> list[ConversationSource]:
    """Les chiffres d'un débat, dans l'ordre voulu par le modérateur.

    L'identifiant clôt l'ordre, comme pour la liste des débats au G10 : `position`
    n'est pas unique, et deux chiffres au même rang sortiraient sinon dans un ordre
    qui change d'une requête à l'autre.
    """
    return list(
        await session.scalars(
            select(ConversationSource)
            .where(ConversationSource.conversation_id == conversation.id)
            .order_by(ConversationSource.position, ConversationSource.id)
        )
    )
