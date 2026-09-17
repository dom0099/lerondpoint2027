"""Écrans de modération sur-mesure.

Deux besoins seulement justifient des pages dédiées plutôt que l'admin générique :
créer/éditer une conversation avec ses propositions d'amorce (rare) et vider la file
de modération (quotidien, doit tenir en un clic). Tout le reste vit dans /admin.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import email
from app.auth.service import authenticate, establish_session, user_from_cookie
from app.auth.users import SESSION_COOKIE_NAME
from app.config import settings
from app.db import get_session
from app.models import (
    Conversation,
    ConversationSource,
    ConversationState,
    GroupNaming,
    ModerationStatus,
    Statement,
    User,
)
from app.analysis.matching import group_name
from app.analysis.thresholds import approved_statement_count, bound_for
from app.auth.deps import client_ip
from app.services import conversations as service
from app.services import gamification
from app.services import rate_limit
from app.services import statiques
from app.services.liens import LienInvalide, valider_liens
from app.services import liens_verification
from app.services import reformulation as reformulation_service
from app.services import nommage as nommage_service
from app.services import independance_lecture
from app.services import signalement as signalement_regles
from app.services import signalement_file
from app.services import themes as themes_service
from app.services import chiffres as chiffres_service
from app.services import video as video_service

router = APIRouter(prefix="/moderation", tags=["moderation"])
templates = Jinja2Templates(directory="app/templates")
statiques.enregistrer_globals(templates)
logger = logging.getLogger("app.moderation")

LOGIN_PATH = "/moderation/login"


def _optional_int(value: str, *, low: int, high: int) -> int | None:
    """Champ numérique facultatif : vide, illisible ou hors bornes -> « automatique ».

    Les deux réglages d'analyse sont facultatifs et bornés. Un POST forgé à la main
    (`force_group_count=abc`) faisait tomber `int()` en erreur 500 ; retomber sur le
    comportement automatique est le seul repli qui ne casse rien, et le formulaire
    réaffiche aussitôt la valeur réellement retenue.
    """
    value = (value or "").strip()
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if low <= number <= high else None


async def require_moderator(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    """Réserve la page aux comptes `is_superuser`.

    Redirige vers la connexion plutôt que de renvoyer 401 : ce sont des pages, pas
    des appels d'API.
    """
    user = await user_from_cookie(session, request.cookies)
    if user is None or not user.is_superuser:
        raise HTTPException(status_code=303, headers={"Location": LOGIN_PATH})
    return user


async def _page(
    request: Request,
    session: AsyncSession,
    moderator: User,
    template: str,
    **context,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {
            "moderator": moderator,
            "pending_total": await service.pending_count(session),
            # Le compteur de liens morts est calculé par la MÊME fonction que l'écran
            # qu'il annonce, et non par un `count(*)` plus économe : un chiffre dans la
            # barre qui ne correspondrait pas à la liste ouverte ferait chercher pour
            # rien. Le coût est celui du catalogue de liens publiés — celui-là même que
            # le worker parcourt à chaque passage.
            "liens_total": await liens_verification.combien_a_corriger(session),
            # Même règle que le compteur de liens morts : le chiffre de la barre est
            # calculé par la MÊME fonction que le repère de la page qu'il annonce, et
            # non par un `count(*)` plus économe. Un chiffre qui ne correspondrait pas
            # à ce que la page montre ferait chercher pour rien.
            "signalements_total": await signalement_file.non_traites(session),
            # MOD-16. Même règle que les trois autres compteurs : calculé par la MÊME
            # fonction que la section de l'écran qu'il annonce.
            "reformulations_total": await reformulation_service.combien_en_attente(
                session
            ),
            **context,
        },
    )


# --- connexion ------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"moderator": None})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    # Plafond avant vérification : sinon chaque essai coûte un hachage argon2id, et
    # c'est précisément ce qu'on refuse de dépenser pour un attaquant.
    if not await rate_limit.login_allowed(session, client_ip(request)):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "moderator": None,
                "error": (
                    "Trop de tentatives depuis cette adresse. "
                    "Réessayez dans une minute."
                ),
            },
            status_code=429,
            headers={"Retry-After": "60"},
        )

    user = await authenticate(session, username, password)
    if user is None or not user.is_superuser:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"moderator": None, "error": "Identifiants invalides ou compte non modérateur."},
            status_code=401,
        )
    response = RedirectResponse("/moderation/", status_code=303)
    await establish_session(session, user, request, response)
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(LOGIN_PATH, status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# --- conversations --------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def conversations_index(
    request: Request,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> HTMLResponse:
    rows = []
    for conversation in await service.list_conversations(session):
        counts = dict(
            (
                await session.execute(
                    select(Statement.moderation_status, func.count(Statement.id))
                    .where(Statement.conversation_id == conversation.id)
                    .group_by(Statement.moderation_status)
                )
            ).all()
        )
        rows.append(
            (
                conversation,
                counts.get(ModerationStatus.approved, 0),
                counts.get(ModerationStatus.pending, 0),
            )
        )
    return await _page(
        request, session, moderator, "conversations.html", conversations=rows
    )


@router.post("/conversations")
async def create_conversation(
    title: str = Form(...),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    conversation = await service.create_conversation(
        session,
        owner=moderator,
        title=title.strip(),
        description=description.strip() or None,
    )
    await gamification.on_conversation_approved(session, conversation)
    return RedirectResponse(
        f"/moderation/conversations/{conversation.id}", status_code=303
    )


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
async def edit_conversation(
    conversation_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> HTMLResponse:
    conversation = await service.by_id(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    approved = await approved_statement_count(session, conversation)
    return await _page(
        request,
        session,
        moderator,
        "conversation.html",
        conversation=conversation,
        statements=await service.all_statements(session, conversation),
        states=[s.value for s in ConversationState],
        approved_statements=approved,
        # Ce que vaudra le seuil si le champ reste vide : le modérateur doit voir la
        # règle appliquée, pas la deviner.
        auto_min_user_votes=bound_for(approved),
        themes=themes_service.THEMES,
        themes_max=themes_service.THEMES_MAX,
        themes_choisis=conversation.codes_themes,
        chiffres=await chiffres_service.chiffres_de(session, conversation),
        video_max_octets=settings.video_max_octets,
        video_miniature_positions=range(len(video_service.MINIATURE_POSITIONS_CANDIDATES)),
    )


@router.post("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: int,
    title: str = Form(...),
    description: str = Form(""),
    state: str = Form(...),
    allow_participant_statements: str | None = Form(None),
    force_group_count: str = Form(""),
    min_user_votes: str = Form(""),
    themes: list[str] = Form([]),
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    conversation = await service.by_id(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation inconnue")

    # « Au moins un thème » est exigé À LA PUBLICATION, et non sur un brouillon : un
    # débat en préparation n'est visible de personne, et rien ne le filtre encore.
    #
    # La règle porte sur ce qui est SOUMIS, pas sur ce qui est stocké — c'est ce qui
    # permet d'étiqueter un débat déjà en ligne sans se heurter à sa propre règle. La
    # contrepartie est réelle et voulue : tant qu'un vieux débat n'est pas étiqueté,
    # toute modification de son titre exige d'en choisir les thèmes au passage.
    publie = ConversationState(state) is not ConversationState.draft
    if publie:
        try:
            themes_service.valider_choix(themes)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Un débat publié porte de 1 à {themes_service.THEMES_MAX} "
                    f"thèmes — {exc}."
                ),
            ) from None
    service.poser_themes(conversation, themes)

    conversation.title = title.strip()
    conversation.description = description.strip() or None
    conversation.state = ConversationState(state)
    conversation.allow_participant_statements = allow_participant_statements is not None
    # Chaîne vide = « automatique » pour les deux réglages d'analyse. Ils sont pris
    # en compte au prochain passage du worker, que `conversations_needing_analysis`
    # déclenche dès que l'un d'eux diffère de celui du dernier calcul.
    conversation.force_group_count = _optional_int(force_group_count, low=2, high=8)
    # Borne haute large : le seuil ne sert qu'à écarter les passages éclair, et un
    # modérateur qui vise 30 votes sur une conversation qui en compte 40 sait ce
    # qu'il fait. En dessous de 1, le réglage n'aurait plus de sens.
    conversation.min_user_votes = _optional_int(min_user_votes, low=1, high=50)
    await session.commit()
    return RedirectResponse(
        f"/moderation/conversations/{conversation_id}", status_code=303
    )


@router.post("/conversations/{conversation_id}/statements")
async def add_statement(
    conversation_id: int,
    request: Request,
    text: str = Form(...),
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    conversation = await service.by_id(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    # Les couples sont relevés dans l'ordre du formulaire, comme sur `/proposer` : une
    # seule proposition ici, donc aucun appariement à tenir.
    form = await request.form()
    urls = [str(v) for k, v in form.multi_items() if k == "source_url"]
    labels = [str(v) for k, v in form.multi_items() if k == "source_label"]
    try:
        labels += [""] * (len(urls) - len(labels))
        liens = valider_liens(list(zip(urls, labels)))
    except LienInvalide as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        await service.add_seed_statement(session, conversation, text, liens=liens)
    except (ValueError, service.DuplicateStatement) as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc) or "Cette proposition existe déjà dans la conversation.",
        ) from None
    return RedirectResponse(
        f"/moderation/conversations/{conversation_id}", status_code=303
    )


# --- chiffres publics d'un débat (chantier J4) ------------------------------------
#
# Écran volontairement SEC : un tableau et un formulaire, pas d'éditeur. C'est l'outil
# d'une personne, pas un produit — et chaque commodité ajoutée ici est une commodité à
# tester et à maintenir pour un usage qui se compte en lignes par débat.


async def _chiffre_de(
    session: AsyncSession, conversation_id: int, source_id: int
) -> ConversationSource:
    """Le chiffre demandé, **et la vérification qu'il appartient bien à ce débat**.

    Sans ce second contrôle, `/moderation/conversations/1/chiffres/999` modifierait le
    chiffre 999 quelle que soit la conversation à laquelle il appartient. La modération
    est déjà réservée aux modérateurs, mais une route qui accepte deux identifiants doit
    vérifier qu'ils vont ensemble — sinon un lien recopié de travers écrit ailleurs.
    """
    chiffre = await session.get(ConversationSource, source_id)
    if chiffre is None or chiffre.conversation_id != conversation_id:
        raise HTTPException(status_code=404, detail="Chiffre inconnu")
    return chiffre


@router.post("/conversations/{conversation_id}/chiffres")
async def add_chiffre(
    conversation_id: int,
    titre: str = Form(""),
    valeur: str = Form(""),
    url: str = Form(""),
    date_donnees: str = Form(""),
    date_verification: str = Form(""),
    position: str = Form(""),
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    conversation = await service.by_id(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    try:
        titre, valeur, url = chiffres_service.valider_chiffre(titre, valeur, url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    session.add(
        ConversationSource(
            conversation_id=conversation_id,
            titre=titre,
            valeur=valeur,
            url=url,
            date_donnees=chiffres_service.date_saisie(date_donnees),
            date_verification=chiffres_service.date_saisie(date_verification),
            position=_optional_int(position, low=0, high=999) or 0,
        )
    )
    await session.commit()
    return RedirectResponse(
        f"/moderation/conversations/{conversation_id}", status_code=303
    )


@router.post("/conversations/{conversation_id}/chiffres/{source_id}")
async def update_chiffre(
    conversation_id: int,
    source_id: int,
    titre: str = Form(""),
    valeur: str = Form(""),
    url: str = Form(""),
    date_donnees: str = Form(""),
    date_verification: str = Form(""),
    position: str = Form(""),
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    chiffre = await _chiffre_de(session, conversation_id, source_id)
    try:
        chiffre.titre, chiffre.valeur, chiffre.url = chiffres_service.valider_chiffre(
            titre, valeur, url
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    chiffre.date_donnees = chiffres_service.date_saisie(date_donnees)
    chiffre.date_verification = chiffres_service.date_saisie(date_verification)
    chiffre.position = _optional_int(position, low=0, high=999) or 0
    await session.commit()
    return RedirectResponse(
        f"/moderation/conversations/{conversation_id}", status_code=303
    )


@router.post("/conversations/{conversation_id}/chiffres/{source_id}/supprimer")
async def delete_chiffre(
    conversation_id: int,
    source_id: int,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    """Sous `/supprimer` et en POST.

    En GET, un aspirateur de liens ou un préchargement de navigateur viderait la page
    des chiffres sans que personne ait cliqué.
    """
    await session.delete(await _chiffre_de(session, conversation_id, source_id))
    await session.commit()
    return RedirectResponse(
        f"/moderation/conversations/{conversation_id}", status_code=303
    )


# --- vidéo de présentation (chantier Vidéo, VIDEO-2) -----------------------------
#
# Seul endroit d'où « viennent les envois », faute de laquelle VIDEO-1 avait laissé la
# question ouverte : c'est l'écran de modération, pas un formulaire participant — la
# création d'un débat est déjà un geste de modérateur (voir `create_conversation`
# ci-dessus), attacher sa vidéo l'est tout autant. Appelé EN LIGNE, dans la requête :
# l'encodage mesuré au VIDEO-1 tient en quelques secondes (0,29x le temps réel), et ce
# n'est pas un volume qui justifie une file d'attente — la même raison qui vaut pour
# `app/worker.py`.


def _refuser_si_en_attente(conversation: Conversation) -> None:
    """Un débat encore `pending` (proposé par un participant, pas encore relu) garde
    la vidéo que son auteur a jointe À LA PROPOSITION — le modérateur ne peut ni la
    remplacer ni la retirer avant d'avoir statué (décision du 13/09/2026, VIDEO-3).
    Une fois approuvé ou rejeté, ces routes redeviennent pleinement actives : c'est
    une fenêtre de lecture seule, pas une perte de capacité durable."""
    if conversation.moderation_status == ModerationStatus.pending:
        raise HTTPException(
            status_code=409,
            detail=(
                "Ce débat est en attente de relecture : sa vidéo ne peut être "
                "modifiée qu'après votre décision (approbation ou rejet)."
            ),
        )


def _effacer_video_existante(conversation: Conversation) -> None:
    """Efface fichier(s) et candidates d'une vidéo déjà attachée — remplacement ou
    retrait, même geste. Décision du 13/09/2026 : une vidéo remplacée n'est PAS
    conservée pour la modération « ligne rouge »."""
    if conversation.video_miniature_chemin:
        video_service.supprimer(conversation.video_miniature_chemin)
    elif conversation.video_chemin:
        video_service.supprimer_candidates_miniatures(conversation.video_chemin)
    video_service.supprimer(conversation.video_chemin)


@router.post("/conversations/{conversation_id}/video")
async def add_video(
    conversation_id: int,
    fichier: UploadFile,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    conversation = await service.by_id(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    _refuser_si_en_attente(conversation)

    try:
        chemin_brut = await video_service.recevoir(
            fichier.read,
            settings.video_max_octets,
            suffixe=Path(fichier.filename or "").suffix,
        )
    except video_service.FichierTropGros as erreur:
        raise HTTPException(status_code=413, detail=f"{erreur} — envoi refusé.") from None
    finally:
        await fichier.close()

    try:
        resultat = await video_service.preparer_avec_choix_miniature(
            chemin_brut, sous_dossier=f"conversation-{conversation_id}"
        )
    except video_service.OutilAbsent as erreur:
        # Ne concerne pas qui a envoyé le fichier : c'est une panne de serveur, et
        # c'est précisément pour CETTE erreur-là que le journal ne suffit pas (question
        # posée en fin de journal VIDEO-1, tranchée le 13/09/2026).
        logger.error(
            "ffmpeg/ffprobe absent — conversation %s : %s", conversation_id, erreur
        )
        if settings.alert_email:
            await email.send_email(
                settings.alert_email,
                "Chantier C — ffmpeg absent sur le serveur",
                (
                    f"L'envoi d'une vidéo pour la conversation {conversation_id} a "
                    f"échoué : {erreur}\n\n"
                    "ffmpeg/ffprobe n'est pas installé sur ce serveur — c'est une "
                    "dépendance SYSTÈME, pas une bibliothèque Python."
                ),
            )
        raise HTTPException(
            status_code=503,
            detail="Le serveur ne peut pas traiter les vidéos actuellement.",
        ) from None
    except video_service.ErreurVideo as erreur:
        raise HTTPException(status_code=422, detail=str(erreur)) from None
    finally:
        chemin_brut.unlink(missing_ok=True)

    _effacer_video_existante(conversation)
    conversation.video_chemin = resultat.chemin_video
    conversation.video_miniature_chemin = None  # en attente du choix
    conversation.video_duree_secondes = resultat.duree_s
    conversation.video_profil = resultat.profil
    await session.commit()
    return RedirectResponse(
        f"/moderation/conversations/{conversation_id}", status_code=303
    )


@router.post("/conversations/{conversation_id}/video/miniature")
async def choose_video_thumbnail(
    conversation_id: int,
    indice: int = Form(...),
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    conversation = await service.by_id(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    _refuser_si_en_attente(conversation)
    if not conversation.video_chemin:
        raise HTTPException(status_code=409, detail="Aucune vidéo à illustrer.")
    try:
        conversation.video_miniature_chemin = video_service.choisir_miniature(
            conversation.video_chemin, indice
        )
    except video_service.ErreurVideo as erreur:
        raise HTTPException(status_code=422, detail=str(erreur)) from None
    await session.commit()
    return RedirectResponse(
        f"/moderation/conversations/{conversation_id}", status_code=303
    )


@router.post("/conversations/{conversation_id}/video/supprimer")
async def delete_video(
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    """Sous `/supprimer` et en POST — même raison que `delete_chiffre` : un GET
    qu'un aspirateur de liens suivrait ne doit rien pouvoir effacer."""
    conversation = await service.by_id(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    _refuser_si_en_attente(conversation)
    _effacer_video_existante(conversation)
    conversation.video_chemin = None
    conversation.video_miniature_chemin = None
    conversation.video_duree_secondes = None
    conversation.video_profil = None
    await session.commit()
    return RedirectResponse(
        f"/moderation/conversations/{conversation_id}", status_code=303
    )


async def _conversation_avec_video(
    session: AsyncSession, conversation_id: int
) -> Conversation:
    conversation = await service.by_id(session, conversation_id)
    if conversation is None or not conversation.video_chemin:
        raise HTTPException(status_code=404, detail="Aucune vidéo pour ce débat")
    return conversation


@router.get("/conversations/{conversation_id}/video/fichier")
async def video_file(
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> FileResponse:
    """Prévisualisation RÉSERVÉE À LA MODÉRATION — pas la route publique du VIDEO-3,
    qui demandera la configuration nginx que VIDEO-1 a explicitement laissée de côté."""
    conversation = await _conversation_avec_video(session, conversation_id)
    return FileResponse(video_service.racine_media() / conversation.video_chemin)


@router.get("/conversations/{conversation_id}/video/couverture")
async def video_cover(
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> FileResponse:
    conversation = await _conversation_avec_video(session, conversation_id)
    if not conversation.video_miniature_chemin:
        raise HTTPException(status_code=404, detail="Miniature pas encore choisie")
    return FileResponse(video_service.racine_media() / conversation.video_miniature_chemin)


@router.get("/conversations/{conversation_id}/video/miniature/{indice}")
async def video_thumbnail_candidate(
    conversation_id: int,
    indice: int,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> FileResponse:
    conversation = await _conversation_avec_video(session, conversation_id)
    video = video_service.racine_media() / conversation.video_chemin
    candidate = video.parent / f"{video.stem}-choix-{indice}.jpg"
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Miniature candidate introuvable")
    return FileResponse(candidate)


# --- file de modération ---------------------------------------------------------


@router.get("/liens", response_class=HTMLResponse)
async def liens(
    request: Request,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> HTMLResponse:
    """Les liens signalés injoignables, et où ils sont publiés (chantier K3).

    Le seul écran où un signalement appelle une action : ailleurs, il informe. C'est
    aussi le seul endroit d'où l'on peut **vérifier soi-même** — le lien y est
    cliquable, et un humain qui ouvre la page tranche ce qu'aucun `HEAD` ne tranchera
    jamais, comme un site qui répond 404 à un robot et 200 à un navigateur.

    Aucune requête sortante n'est déclenchée d'ici, et il n'y a pas de bouton pour en
    déclencher une : c'est la question 6 posée au client, et sa réponse est non.
    """
    return await _page(
        request,
        session,
        moderator,
        "liens.html",
        signalements=await liens_verification.liens_a_corriger(session),
        echecs_pour_signaler=liens_verification.ECHECS_POUR_SIGNALER,
        jours_pour_signaler=liens_verification.JOURS_POUR_SIGNALER,
    )


#: Les routes, telles qu'elles s'écrivent sur l'écran. Le code brut
#: (`RETRAIT_CONSERVATOIRE`) est ce qui va en base ; il n'a pas à se lire dans une page.
ROUTES_LIBELLES = {
    "RETRAIT_CONSERVATOIRE": "Ligne rouge — retrait conservatoire",
    "A_QUALIFIER": "À qualifier",
    "A_REFORMULER": "À reformuler",
    "A_FUSIONNER": "À fusionner",
    "FILE_RESPONSABLE": "File du responsable",
}


@router.get("/independance", response_class=HTMLResponse)
async def independance(
    request: Request,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> HTMLResponse:
    """L'écran de surveillance des afflux coordonnés (MOD-5).

    **Réservé à la file de modération, et rien de ce qu'il montre ne s'affiche
    publiquement** — ni les indicateurs, ni les seuils, ni l'alerte, ni le fait même
    qu'un débat soit surveillé. La règle est publique, les seuils de détection sont
    privés : les publier reviendrait à publier la notice pour les contourner.

    Lecture seule de bout en bout : cet écran n'a aucun bouton, et c'est voulu. Ce lot
    regarde, il n'agit sur rien.
    """
    return await _page(
        request,
        session,
        moderator,
        "independance.html",
        debats=await independance_lecture.debats_surveilles(session),
        jours_contexte=signalement_file.JOURS_CONTEXTE,
    )


@router.get("/signalements", response_class=HTMLResponse)
async def signalements(
    request: Request,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> HTMLResponse:
    """Les propositions signalées, groupées, triées par gravité puis par ancienneté.

    Séparée de `/moderation/queue` et non ajoutée comme troisième section : la file
    existante traite ce qui n'a **jamais** été publié, celle-ci ce qui l'est **déjà**.
    Ce ne sont ni les mêmes gestes, ni le même degré d'urgence, ni les mêmes
    conséquences — et les mélanger enterrerait la ligne rouge sous les propositions en
    attente, qui sont nombreuses et sans danger.
    """
    lignes = await signalement_file.file(session)
    combien, age = await signalement_file.reperes(session)
    return await _page(
        request,
        session,
        moderator,
        "signalements.html",
        lignes=lignes,
        non_traites=combien,
        age_lisible=signalement_file.age_lisible(age),
        # Calculés ici plutôt que dans le gabarit : une propriété qui interroge
        # l'horloge à chaque accès donnerait deux valeurs différentes dans la même page.
        ages={ligne.statement.id: signalement_file.age_lisible(ligne.age) for ligne in lignes},
        routes_libelles=ROUTES_LIBELLES,
        libelle=signalement_regles.libelle,
        jours_contexte=signalement_file.JOURS_CONTEXTE,
        note_confidentialite=signalement_regles.NOTE_CONFIDENTIALITE,
        # Les contestations EN TÊTE de page (MOD-3b), avant les signalements : une
        # personne qui conteste attend une réponse, un signalement n'attend personne.
        # Groupées par proposition depuis le MOD-13, et réduites à ce qui reste À LIRE :
        # une contestation lue n'est plus une tâche, elle reste sur la carte du dossier.
        contestations=await signalement_file.contestations_a_lire(session),
        # Les MÊMES contestations, portées sur la carte de la proposition — là où
        # confirmer et annuler se cliquent. C'est tout l'objet du MOD-13 : la défense de
        # l'auteur sous les yeux de qui tranche, et non en haut d'une page qu'il faut
        # avoir pensé à lire d'abord.
        contestations_par_proposition=await signalement_file.contestations_des_propositions(
            session, [ligne.statement.id for ligne in lignes]
        ),
        lien_de_contestation=signalement_regles.lien_de_contestation,
        # MOD-10. Les navettes en cours, par proposition — pour que la carte montre soit
        # le formulaire, soit l'échange qui attend déjà une réponse, jamais les deux.
        navettes={
            ligne.statement.id: await reformulation_service.en_attente(
                session, ligne.statement.id
            )
            for ligne in lignes
        },
        # Quelles cartes portent le formulaire : celles dont une plainte encore reçue
        # demande une reformulation. On lit les signalements de la ligne plutôt que sa
        # route la plus grave — une proposition signalée pour ligne rouge ET pour mauvaise
        # formulation se retire d'abord, mais la demande de reformulation existe.
        reformulables={
            ligne.statement.id
            for ligne in lignes
            if any(
                signalement.route == signalement_regles.Route.A_REFORMULER.value
                for signalement in ligne.signalements
            )
        },
        reformulation_max=service.MAX_STATEMENT_LENGTH,
    )


@router.get("/reformulations", response_class=HTMLResponse)
async def reformulations(
    request: Request,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> HTMLResponse:
    """Le registre des reformulations (MOD-16).

    **Un écran de lecture, sans aucun geste.** L'écran des signalements montre ce qu'il y
    a à faire, et une reformulation acceptée en sort avec la plainte qu'elle a close : il
    ne reste alors rien de visible de ce qui a été proposé ni de ce que l'auteur a
    répondu. Ce registre est cette mémoire-là.

    Il ne porte aucun bouton, et c'est délibéré : les deux seuls gestes possibles sur une
    reformulation appartiennent à l'auteur, et les proposer ici reviendrait à permettre
    au responsable de répondre à sa place.
    """
    lignes = await reformulation_service.registre(session)
    return await _page(
        request,
        session,
        moderator,
        "reformulations.html",
        lignes=lignes,
        # Calculés ici et non dans le gabarit : une propriété qui interroge l'horloge à
        # chaque accès donnerait deux valeurs différentes dans la même page.
        restants={
            ligne.reformulation.id: signalement_file.age_lisible(ligne.restant)
            for ligne in lignes
            if ligne.restant is not None and ligne.restant.total_seconds() > 0
        },
        delai_jours=reformulation_service.DELAI_VALIDATION.days,
    )


@router.post("/signalements/{statement_id}/reformuler")
async def proposer_une_reformulation(
    statement_id: int,
    texte: str = Form(""),
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    """Le responsable propose une version plus claire à l'auteur (MOD-10).

    **Déclarée AVANT `/{statement_id}/{decision}`**, qui capturerait « reformuler » comme
    une décision inconnue et rendrait 404. L'ordre de déclaration est ce qui fait la
    différence, et c'est la raison pour laquelle cette route n'est pas plus bas.

    **Rien n'est remplacé ici** : la proposition part vers l'auteur, qui accepte ou
    refuse. C'est la différence entre une navette et une retouche éditoriale, et elle
    tient à ce que le responsable ne peut pas écrire dans le texte de quelqu'un d'autre.

    En attendant des arbitres tirés au sort (MOD-7, MOD-12), le responsable est le seul
    reformulateur — arbitrage de dom du 15 septembre 2026. Il n'y a donc jamais qu'un
    candidat, et aucune élection à trancher.
    """
    statement = await session.get(Statement, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="Proposition inconnue")
    try:
        await reformulation_service.proposer(
            session, statement, texte, auteur=moderator.email, user_id=moderator.id
        )
    except reformulation_service.ReformulationImpossible as erreur:
        raise HTTPException(status_code=409, detail=str(erreur)) from None
    return RedirectResponse("/moderation/signalements", status_code=303)


@router.post("/signalements/{statement_id}/{decision}")
async def moderate_signalement(
    statement_id: int,
    decision: str,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    """Les trois gestes du responsable, chacun journalisé.

    `confirmer` et `annuler` exigent que la proposition soit effectivement retirée, et
    renvoient 409 sinon : rejouer un lien d'annulation sur une proposition déjà remise
    en circulation la « remettrait » une seconde fois, en écrivant un acte de journal
    qui décrirait quelque chose qui n'a pas eu lieu. C'est la même garde qu'au chantier C
    sur les conversations.
    """
    gestes = {
        "confirmer": signalement_file.confirmer_retrait,
        "annuler": signalement_file.annuler_retrait,
        "classer": signalement_file.classer_sans_suite,
    }
    if decision not in gestes:
        raise HTTPException(status_code=404, detail="Décision inconnue")
    statement = await session.get(Statement, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="Proposition inconnue")

    try:
        await gestes[decision](
            session, statement, auteur=moderator.email, user_id=moderator.id
        )
    except signalement_file.PropositionNonSignalable as erreur:
        raise HTTPException(status_code=409, detail=str(erreur)) from None
    return RedirectResponse("/moderation/signalements", status_code=303)


@router.post("/contestations/{contestation_id}/lue")
async def marquer_contestation_lue(
    contestation_id: int,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    """« J'ai lu cette demande » — et rien de plus (MOD-13).

    **Ce n'est pas une décision, et le bouton le dit.** Marquer lu ne répond pas à
    l'auteur, n'accepte ni ne refuse son recours, et ne touche pas à sa proposition : les
    trois gestes qui font cela sont ailleurs sur la même page, et ce lot n'y touche pas.
    Ce geste sert à une seule chose — que la liste des demandes à lire diminue quand on
    l'a lue, faute de quoi le responsable relit tout à chaque passage et finit par ne
    plus rien lire.

    Aucun 409 sur une contestation déjà lue, contrairement aux trois gestes de retrait :
    relire n'est pas rejouer une décision, et le service ne ré-horodate pas.
    """
    if not await signalement_file.marquer_contestation_lue(session, contestation_id):
        raise HTTPException(status_code=404, detail="Contestation inconnue")
    return RedirectResponse("/moderation/signalements", status_code=303)


@router.get("/queue", response_class=HTMLResponse)
async def queue(
    request: Request,
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> HTMLResponse:
    """Ce qui attend encore une décision avant d'exister publiquement.

    **Il ne reste que deux sections depuis le MOD-14** : les conversations proposées et
    les noms de groupe. La troisième — les propositions en attente — a disparu avec la
    pré-modération : une proposition déposée est publiée d'emblée, et ce qui la concerne
    se décide après coup sur `/moderation/signalements`.

    Une conversation, elle, continue de s'approuver AVANT : l'approuver ouvre un espace
    entier et ses amorces, ce qui n'est pas du même ordre qu'une ligne de plus dans un
    débat existant. Un nom de groupe aussi : il est engendré par un modèle, et il porte
    sur des personnes.
    """
    pending_conversations = [
        (conversation, await service.all_statements(session, conversation))
        for conversation in await service.pending_conversations(session)
    ]
    # Les noms de groupe : chacun avec le débat et les déclarations sur lesquelles le
    # modèle a travaillé. Sans elles, un nom ne se juge pas — voir la note du gabarit.
    noms = []
    for ligne in await nommage_service.a_moderer(session):
        conversation = await session.get(Conversation, ligne.conversation_id)
        if conversation is None:
            continue
        ligne.lettre = group_name(ligne.stable_group_id)
        noms.append(
            (ligne, conversation, await nommage_service.declarations_lisibles(session, ligne))
        )

    return await _page(
        request,
        session,
        moderator,
        "queue.html",
        conversations=pending_conversations,
        noms=noms,
        themes=themes_service.THEMES,
        themes_max=themes_service.THEMES_MAX,
    )


@router.post("/noms/{naming_id}/{decision}")
async def moderate_group_name(
    naming_id: int,
    decision: str,
    nom: str = Form(""),
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    """Valide (éventuellement en corrigeant) ou refuse un nom de groupe généré.

    La correction est permise ici alors qu'elle ne l'est nulle part ailleurs dans la
    modération, et la différence n'est pas un relâchement : ailleurs, le texte est la
    parole d'un participant, et la retoucher serait lui mettre des mots dans la bouche.
    Ici, le texte est la sortie d'une machine — il n'y a personne à trahir. Ce que le
    modèle avait écrit reste enregistré à côté, pour que la mention publique puisse
    distinguer « validé » de « corrigé et validé ».
    """
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=404, detail="Décision inconnue")
    ligne = await session.get(GroupNaming, naming_id)
    if ligne is None or ligne.nom is None:
        raise HTTPException(status_code=404, detail="Nom inconnu")
    if ligne.statut is not ModerationStatus.pending:
        # Même garde qu'au chantier C sur les conversations : rejouer un lien de
        # validation sur un nom déjà tranché le rouvrirait en silence.
        raise HTTPException(status_code=409, detail="Ce nom a déjà été modéré.")

    await nommage_service.modere(
        session,
        ligne,
        moderator,
        approuve=decision == "approve",
        nom_corrige=nom,
    )
    return RedirectResponse("/moderation/queue", status_code=303)


# Sous /queue/ volontairement : « /conversations/{id}/{decision} » capterait aussi
# « /conversations/{id}/statements » selon l'ordre d'enregistrement des routes.
@router.post("/queue/conversations/{conversation_id}/{decision}")
async def moderate_conversation(
    conversation_id: int,
    decision: str,
    themes: list[str] = Form([]),
    session: AsyncSession = Depends(get_session),
    moderator: User = Depends(require_moderator),
) -> RedirectResponse:
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=404, detail="Décision inconnue")
    conversation = await service.by_id(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    if conversation.moderation_status is not ModerationStatus.pending:
        # L'URL reste accessible hors de la file : rejouer un lien d'approbation sur
        # une conversation close la rouvrait silencieusement.
        raise HTTPException(
            status_code=409,
            detail="Cette conversation a déjà été modérée.",
        )

    approve = decision == "approve"
    if approve:
        # Le proposeur a suggéré, le modérateur tranche — au moment où il approuve,
        # pas dans un écran de plus. Un rejet ne touche pas à l'étiquetage : il n'y a
        # rien à ranger dans une conversation qui ne sera pas publiée.
        try:
            themes_service.valider_choix(themes)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Approuver un débat demande de 1 à {themes_service.THEMES_MAX} "
                    f"thèmes — {exc}."
                ),
            ) from None
        service.poser_themes(conversation, themes)

    await service.moderate_conversation(
        session, conversation, moderator, approve=approve
    )
    if approve:
        await gamification.on_conversation_approved(session, conversation)
    return RedirectResponse("/moderation/queue", status_code=303)



