"""L'API de signalement d'une proposition publiée (MOD-3a, élargie au MOD-3b).

Depuis le MOD-3b, cette API est celle qu'appelle la fenêtre de signalement de la carte
de vote (`app/templates/public/conversation.html`). Elle reste utilisable seule.

**Le signaleur reçoit toujours la même réponse : « merci, c'est enregistré ».** Jamais
« votre signalement a été rejeté », jamais le sort de la proposition, jamais « vous
aviez déjà signalé ». C'est la règle du §4 du cadrage, et elle a une raison pratique
autant que morale : dès qu'un signaleur apprend ce que son signalement a produit, le
signalement devient un jeu où l'on compte les points, et il se retourne contre les gens
qu'il doit protéger.

**Le MOD-3b a étendu cette règle au plafond de débit**, qui répondait encore 429 avec le
chiffre en clair. Un refus qui s'annonce apprend qu'il existe une limite et à quelle
cadence elle se remplit : il ne reste plus qu'à l'attendre. Le plafond est donc
silencieux — même corps, même code qu'un succès.

Il ne reste qu'un refus visible, et il ne dit rien du sort d'une proposition : 422 quand
la requête est mal formée (trop de motifs, motif inconnu, texte libre manquant), 404
quand la proposition n'existe pas — ou n'existe plus pour un lecteur.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import client_ip, get_current_participant
from app.db import get_session
from app.models import Participant, Statement
from app.services import signalement as regles
from app.services import signalement_file as file_service

router = APIRouter(prefix="/api/signalements", tags=["signalements"])
logger = logging.getLogger("app.signalements")

#: La seule réponse qu'un signaleur reçoit, quoi qu'il advienne de sa plainte.
ACCUSE_DE_RECEPTION = "Merci, c'est enregistré."


class MotifRendu(BaseModel):
    code: str
    libelle: str
    famille: str
    #: Exposée pour que l'écran de demain sache quelles cases demandent une explication,
    #: pas pour qu'il annonce ce qui va se passer. `route` ne doit JAMAIS être montrée
    #: au signaleur : elle dirait « cette case fait retirer », et c'est l'information
    #: qui transforme la liste en menu.
    texte_libre_attendu: bool


class SignalementDepose(BaseModel):
    """Toujours le même corps. Aucun identifiant, aucun statut, aucun décompte."""

    message: str = ACCUSE_DE_RECEPTION


class SignalementCreate(BaseModel):
    statement_id: int
    #: Un ou deux codes de motif. Trois et plus : 422, jamais de troncature silencieuse.
    motifs: list[str] = Field(min_length=1)
    #: Obligatoire avec « autre », refusé avec tous les autres.
    texte_libre: str | None = None


@router.get("/motifs", response_model=list[MotifRendu])
async def motifs() -> list[MotifRendu]:
    """La liste fermée, dans son ordre d'affichage.

    Exposée pour que l'écran du MOD-3b n'ait pas à recopier dix libellés dans un
    gabarit — une liste recopiée est une liste qui divergera. La **route** de chaque
    motif n'en fait volontairement pas partie : voir `MotifRendu`.
    """
    return [
        MotifRendu(
            code=motif.code,
            libelle=motif.libelle,
            famille=motif.famille.value,
            texte_libre_attendu=motif.code == regles.MOTIF_TEXTE_LIBRE,
        )
        for motif in regles.MOTIFS
    ]


@router.post("", response_model=SignalementDepose, status_code=201)
async def deposer(
    payload: SignalementCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    participant: Participant = Depends(get_current_participant),
) -> SignalementDepose:
    """Dépose un signalement. **Un visiteur sans compte peut signaler** — le DSA l'impose.

    L'identification est celle des votes anonymes : `get_current_participant` crée au
    besoin une ligne `participant` attachée à un jeton de cookie. Aucune seconde
    identification n'a été inventée, et c'est ce qui fait que le plafond horaire, le
    dédoublonnage et la fusion à la création d'un compte marchent déjà.

    201 dans tous les cas où la plainte est prise, **y compris quand elle l'avait déjà
    été** : le dépôt est idempotent, et une seconde tentative n'est pas une erreur.
    """
    statement = await session.get(Statement, payload.statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="Proposition inconnue")

    try:
        await file_service.deposer(
            session,
            statement=statement,
            participant=participant,
            motifs=payload.motifs,
            texte_libre=payload.texte_libre,
            referent=request.headers.get("referer"),
            adresse_ip=client_ip(request),
        )
    except regles.SignalementInvalide as erreur:
        # 422 : la requête est mal formée (trop de motifs, motif inconnu, texte libre
        # manquant ou de trop). C'est un défaut de l'appelant, pas une décision sur le
        # fond — et le message est en français, destiné à être affiché tel quel.
        raise HTTPException(status_code=422, detail=str(erreur)) from None
    except file_service.PropositionNonSignalable:
        # 404 et non 409 : une proposition retirée n'existe pas pour un lecteur. Un 409
        # lui apprendrait qu'elle a été retirée, donc que des signalements ont porté —
        # exactement ce que le §4 interdit de laisser entendre.
        raise HTTPException(status_code=404, detail="Proposition inconnue") from None
    except file_service.PlafondAtteint as erreur:
        # **La même réponse qu'un succès, et c'est un changement voulu du MOD-3b.**
        # Le MOD-3a répondait 429 avec « vous avez signalé 10 propositions en moins
        # d'une heure » : ce message apprend à qui le lit qu'un plafond existe, à quelle
        # cadence il se remplit, et donc comment l'attendre ou le contourner. Le
        # signalement est refusé en silence ; seul le journal d'exécution le sait.
        logger.info("signalement refusé par plafond : %s", erreur)

    return SignalementDepose()
