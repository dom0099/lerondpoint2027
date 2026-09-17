"""L'API de la validation aléatoire par les participants (MOD-4).

**Ce que cette API ne fait pas, et c'est ce qui la définit.** Elle ne tient aucune file,
ne retarde aucune publication, n'annonce aucun délai et ne dit jamais à celui qui répond
ce que sa réponse a produit. Une proposition paraît immédiatement (MOD-14) ; si elle est
tirée au sort, quelqu'un la regarde, et sinon rien ne se passe. C'est un contrôle par
sondage **après** publication.

**Il n'y a pas de route qui serve une proposition à valider**, et c'est délibéré. La
carte arrive dans la réponse du vote qui la déclenche (`POST …/votes`), une fois toutes
les sept propositions votées. Une route `GET /api/validations/suivante` serait tirable à
volonté : il suffirait de la rappeler jusqu'à tomber sur la proposition qu'on veut faire
retirer, ce qui transformerait un tirage au sort en choix. Le tirage doit rester la
main du site, pas celle du client.

**La même réponse dans tous les cas** — « Merci, c'est enregistré. » —, exactement comme
pour un signalement. Dès que celui qui répond apprend le sort de ce qu'il a jugé, la
validation devient un jeu où l'on compte les points, et elle se retourne contre les gens
qu'elle doit protéger.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_participant
from app.db import get_session
from app.models import Participant, Statement
from app.services import signalement as regles_signalement
from app.services import signalement_file
from app.services import validation as regles
from app.services import validation_tirage

router = APIRouter(prefix="/api/validations", tags=["validations"])
logger = logging.getLogger("app.validations")


class ValidationRendue(BaseModel):
    """Toujours le même corps. Aucun identifiant, aucun décompte, aucun sort."""

    message: str = regles.ACCUSE_DE_RECEPTION


class ValidationCreate(BaseModel):
    statement_id: int
    #: « conforme » ou « a_revoir ». Rien d'autre : « passer » n'est pas un verdict et
    #: n'arrive jamais jusqu'ici — écarter la carte n'appelle simplement pas cette route.
    verdict: str
    #: Les mêmes cases que pour un signalement, et seulement avec « a_revoir ».
    #: Obligatoires alors : un « à revoir » sans motif est une plainte sans objet, dont
    #: le responsable ne pourrait rien faire.
    motifs: list[str] | None = Field(default=None)
    #: Obligatoire avec « autre », refusé avec tous les autres — la règle du signalement,
    #: appliquée par le même code.
    texte_libre: str | None = None


@router.post("", response_model=ValidationRendue, status_code=201)
async def repondre(
    payload: ValidationCreate,
    session: AsyncSession = Depends(get_session),
    participant: Participant = Depends(get_current_participant),
) -> ValidationRendue:
    """Enregistre la réponse à une sollicitation. **Tout participant, compte ou non.**

    L'identification est celle du vote et du signalement : `get_current_participant`
    crée au besoin une ligne `participant` attachée à un jeton de cookie. Aucune seconde
    identification n'a été inventée — c'est ce qui fait que les plafonds, le
    dédoublonnage et la fusion à la création d'un compte marchent déjà.

    201 dans tous les cas où la réponse est prise, **y compris quand elle l'avait déjà
    été** : l'enregistrement est idempotent, et un double clic n'est pas une erreur.
    """
    statement = await session.get(Statement, payload.statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="Proposition inconnue")

    try:
        verdict = regles.verdict(payload.verdict)
        await validation_tirage.enregistrer(
            session,
            statement=statement,
            participant=participant,
            verdict=verdict,
            motifs=payload.motifs,
            texte_libre=payload.texte_libre,
        )
    except (regles.ValidationInvalide, regles_signalement.SignalementInvalide) as erreur:
        # 422 : la requête est mal formée (verdict inconnu, motifs manquants ou de trop,
        # texte libre absent). C'est un défaut de l'appelant, pas une décision sur le
        # fond — et le message est en français, destiné à être affiché tel quel.
        raise HTTPException(status_code=422, detail=str(erreur)) from None
    except signalement_file.PropositionNonSignalable:
        # 404 et non 409 : la proposition a été retirée entre la carte et la réponse.
        # Elle n'existe plus pour un lecteur, et un 409 apprendrait qu'elle a été
        # retirée — donc que des signalements ont porté.
        raise HTTPException(status_code=404, detail="Proposition inconnue") from None
    except signalement_file.PlafondAtteint as erreur:  # pragma: no cover - filet
        # Le chemin sollicité ne consulte pas le plafond horaire du signalement (voir
        # `signalement_file.deposer`). Ce filet reste au cas où cela changerait : la
        # réponse resterait celle d'un succès, comme au MOD-3b.
        logger.info("validation refusée par plafond : %s", erreur)

    return ValidationRendue()
