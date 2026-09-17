"""L'endpoint que le formulaire de saisie interroge pendant la frappe (MOD-2).

**Pourquoi un aller-retour au serveur plutôt que du JavaScript autonome.** Le
détecteur, c'est `app/services/detection.py` et ses 253 désignations. Le refaire en
JavaScript créerait **deux détecteurs à tenir d'accord** — et le jour où ils
divergeraient, la ligne affichée pendant la frappe ne dirait plus la même chose que le
rapport qui sert à décider. Une seule implémentation, donc, et un aller-retour. Il
coûte 0,3 ms de calcul : moins qu'à peu près tout le reste du site.

**Cet endpoint ne touche pas la base. Du tout.** Pas de session, pas de dépendance
`get_session`, aucune écriture — et c'est délibéré, pas une économie :

- il est appelé **pendant la frappe**, donc bien plus souvent qu'une page. Le brancher
  sur le compteur de débit de `app/services/rate_limit.py` le ferait **écrire une ligne
  en base à chaque frappe**, ce qui transformerait un garde-fou en la charge dont il
  protège ;
- n'ayant ni lecture ni écriture, il n'expose rien. Le pire qu'on puisse en faire est
  de consommer du temps de calcul — borné par le plafond de longueur ci-dessous, et
  moins cher que de demander la page d'accueil.

**Il ne rend aucun verdict**, comme le module qu'il enveloppe : une liste de signaux,
et c'est au navigateur — puis à la personne — de décider quoi en faire. Le choix de
n'afficher que `cible_personnes` est pris dans `app/static/detection.js`, à un seul
endroit, et pas ici : l'endpoint reste fidèle à `analyser()`.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import detection
from app.services.conversations import MAX_STATEMENT_LENGTH

router = APIRouter(tags=["detection"])


class TexteASignaler(BaseModel):
    text: str


class SignalRendu(BaseModel):
    """Un signal, tel que le navigateur le reçoit.

    Mêmes champs que `detection.Signal`, aux mêmes noms : aucune traduction entre les
    deux, donc aucun endroit où elles pourraient se désaccorder.
    """

    code: str
    extrait: str
    debut: int
    fin: int


class SignauxRendus(BaseModel):
    signaux: list[SignalRendu]


@router.post("/api/detection", response_model=SignauxRendus)
async def signaler(payload: TexteASignaler) -> SignauxRendus:
    """Les signaux de forme d'un texte en cours de frappe.

    Refuse au-delà du plafond de saisie plutôt que de tronquer : un texte plus long que
    ce que le formulaire accepte ne peut pas venir du formulaire, et l'accepter
    reviendrait à offrir un calculateur de regex à qui passe par là.
    """
    if len(payload.text) > MAX_STATEMENT_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Texte trop long (max {MAX_STATEMENT_LENGTH}).",
        )
    return SignauxRendus(
        signaux=[
            SignalRendu(code=s.code, extrait=s.extrait, debut=s.debut, fin=s.fin)
            for s in detection.analyser(payload.text)
        ]
    )
