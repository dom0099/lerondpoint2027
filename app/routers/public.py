"""Interface participant.

Pages servies en HTML (Jinja) ; le parcours de vote lui-même appelle l'API en
JavaScript, pour enchaîner les propositions sans recharger la page.

**Aucun groupe d'opinion n'est affiché** : les étiquettes de k-means ne sont pas
stables d'un recalcul à l'autre, donc « groupe 1 » d'une heure n'est pas celui de la
suivante. L'appariement des clusters entre exécutions est prévu à C6 ; d'ici là,
montrer des groupes induirait en erreur.
"""

import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.datastructures import UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi_users.exceptions import (
    InvalidPasswordException,
    UserAlreadyExists,
    UserAlreadyVerified,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app import email
from app.auth.cookies import clear_anon_cookie, read_anon_cookie
from app.auth.deps import client_ip, get_current_participant
from app.auth.service import authenticate, establish_session, user_manager_for
from app.auth.users import SESSION_COOKIE_NAME, current_user_optional
from app.config import settings
from app.db import get_session
from app.models import ConversationState, Participant, User
from app.schemas import UserCreate
from app.services import accounts as accounts_service
from app.services import statiques
from app.services import accueil as accueil_service
from app.services import nommage as nommage_service
from app.services import avatar as avatar_service
from app.services import carte as carte_service
from app.services import chiffres as chiffres_service
from app.services import liens_verification
from app.services import carte_rendu
from app.services import conversations as conversations_service
from app.services import gamification
from app.services import participants as participants_service
from app.services import groups as groups_service
from app.services import rappels as rappels_service
from app.services import reformulation as reformulation_service
from app.services import rate_limit
from app.services import signalement as signalement_regles
from app.services import signalement_file
from app.services.liens import LIENS_MAX, LienInvalide, valider_liens
from app.services import themes as themes_service
from app.services import resultats as resultats_service
from app.services import video as video_service
from app.services import votes as votes_service

logger = logging.getLogger("app.public")

async def _charger_progression(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> None:
    """Charge la progression du visiteur connecté, pour l'avatar de l'en-tête.

    Posée en dépendance de ROUTEUR et non ajoutée à trente signatures : l'en-tête
    est rendu par toutes les pages, donc la donnée qui l'alimente doit arriver par
    le même chemin partout. Le résultat voyage par `request.state` parce qu'une
    dépendance de routeur ne peut pas s'injecter dans la vue — c'est le seul point
    de couture de ce montage.

    Deux garde-fous sur le coût, qui est de trois petites requêtes :
      - **rien pour un visiteur anonyme.** Le parcours de vote, qui est le chemin de
        masse, se fait sans compte : il ne paie rien du tout.
      - **rien hors des GET.** Un POST qui redirige ne rend aucun en-tête ; calculer
        sa progression serait du travail jeté.

    Pourquoi le faire quand même, alors que `_page` s'interdisait d'ajouter une
    requête : un avatar qui n'affiche le niveau que sur la page du compte n'est pas
    un avatar, c'est une décoration de cette page-là. La forme ne vaut que si elle
    dit la même chose partout. Si le coût devient visible, la réponse est un cache
    par session — pas un affichage à moitié.
    """
    request.state.progression = None
    if user is not None and request.method == "GET":
        request.state.progression = await gamification.progress_for(session, user)


async def _charger_rappels(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> None:
    """Charge ce qui attend une réponse de ce visiteur (MOD-6, lot de repli).

    Posée en dépendance de ROUTEUR, comme `_charger_progression`, et pour la même
    raison : le rappel doit pouvoir paraître sur plusieurs pages, et la donnée qui
    l'alimente doit arriver par le même chemin partout.

    **L'identité est cherchée, jamais créée.** `get_current_participant` fabriquerait un
    participant à chaque visiteur de passage — donc une ligne en base par robot
    d'indexation. On lit le jeton du cookie et on s'arrête là s'il ne désigne personne :
    quelqu'un qui n'a jamais rien déposé n'a rien à lire, et ne coûte aucune requête.

    Rien hors des GET : un POST qui redirige ne rend aucune page.
    """
    request.state.rappels = []
    if request.method != "GET":
        return

    participant = None
    if user is not None:
        participant = await participants_service.by_user(session, user)
    else:
        jeton = read_anon_cookie(request)
        if jeton:
            participant = await participants_service.by_anon_token(session, jeton)
    if participant is None:
        return

    request.state.rappels = await rappels_service.en_attente(session, participant)


router = APIRouter(
    tags=["public"],
    dependencies=[Depends(_charger_progression), Depends(_charger_rappels)],
)
templates = Jinja2Templates(directory="app/templates")
statiques.enregistrer_globals(templates)
# Fonction Python et non macro Jinja : le dessin dépend d'un hachage, que Jinja ne
# calcule pas — voir app/services/avatar.py. Accessible depuis n'importe quel
# gabarit public, sur le même principe que les filtres déjà enregistrés ailleurs.
templates.env.globals["identicon_svg"] = avatar_service.identicon_svg
# Décide si un chiffre public se prête au petit graphique en gaufre du chantier I
# (voir chiffres.html) — pas une donnée de page, une fonction pure, au même titre.
templates.env.globals["pourcentage"] = chiffres_service.pourcentage

#: Date affichée en tête des pages légales. Tenue à la main : elle doit changer
#: quand le TEXTE change, pas à chaque déploiement.
LEGAL_UPDATED = "2 septembre 2026"
#: La politique de confidentialité a sa PROPRE date depuis le MOD-13, et ce n'est pas un
#: détail : elle a changé le 15 septembre (le traitement lié aux signalements y a été
#: décrit), les mentions légales non. Une date partagée aurait daté d'aujourd'hui un
#: document qui n'a pas bougé — c'est-à-dire menti sur la seule chose qu'un lecteur
#: regarde pour savoir ce qui est à jour.
CONFIDENTIALITE_UPDATED = "16 septembre 2026"


def _page(request: Request, template: str, user: User | None, **context) -> HTMLResponse:
    # `champ` (nom du champ fautif) accompagne `error` quand la faute est
    # localisable : le gabarit borde alors CE champ en rouge et pose le message
    # dessous, au lieu d'un bandeau en haut de page qui laisse chercher. Quand la
    # faute ne se rattache à aucun champ — plafond de tentatives, lien expiré — il
    # reste absent et le message garde sa place en tête.
    # La progression vient de `_charger_progression` (dépendance de routeur) ; celle
    # que la page a déjà chargée pour son propre compte la remplace, pour ne pas
    # calculer deux fois la même chose sur /compte.
    progress = context.get("progress") or getattr(request.state, "progression", None)
    return templates.TemplateResponse(
        request,
        f"public/{template}",
        {
            "account": user,
            "level": progress.level if progress else None,
            "progression": progress,
            # Les rappels dus à ce visiteur (MOD-6). Ils voyagent jusqu'à TOUTES les
            # pages publiques, mais seuls l'accueil et la liste des débats incluent le
            # gabarit qui les affiche : la page d'un débat ne le fait pas, et c'est ce
            # qui garde le flux de vote intouchable.
            "rappels": getattr(request.state, "rappels", []),
            **context,
        },
    )


def chemin_local(valeur: str | None, defaut: str = "/") -> str:
    """Ramène une adresse de retour venue de l'URL à un chemin de CE site.

    Sans cela, `?suivant=https://ailleurs.example/` fait du site un tremplin : la
    personne clique un lien du Rond-Point, s'inscrit, et se retrouve sur une page qui
    n'est pas la nôtre — c'est la redirection ouverte, et elle sert surtout à rendre
    crédible une fausse page de connexion.

    Deux refus, et le second est celui qu'on oublie : `//ailleurs.example` commence
    bien par une barre oblique, et le navigateur y lit une adresse absolue dont le
    protocole est celui de la page courante.
    """
    valeur = (valeur or "").strip()
    if not valeur.startswith("/") or valeur.startswith("//"):
        return defaut
    return valeur


def _bloc_themes(participant: Participant | None) -> dict:
    """Ce que `/compte` affiche des centres d'intérêt.

    `None` (jamais réglé) et `[]` (a répondu « aucun ») ne se disent pas de la même
    façon : le premier invite à choisir, le second constate un choix. Les confondre
    reprocherait à quelqu'un de ne pas avoir répondu alors qu'il a répondu.
    """
    codes = participant.themes if participant else None
    return {
        "mes_themes": [themes_service.libelle(code) for code in codes or []],
        "themes_reglees": codes is not None,
    }


async def _themes_du_visiteur(request: Request, session: AsyncSession) -> list[str] | None:
    """Les thèmes du participant porté par le cookie, sans jamais en créer un.

    `get_current_participant` créerait une ligne à chaque visite de l'accueil, pour un
    visiteur qui n'a peut-être jamais voté. La page de liste se contente de lire.

    `None` = jamais réglé, ce qui n'est pas « aucun thème » : c'est cette distinction
    qui décide de montrer l'onglet « Mes centres d'intérêt » ou l'invitation à le régler.
    """
    participant = await _participant_du_cookie(request, session)
    return participant.themes if participant else None


# --- conversations ---------------------------------------------------------------


# HEAD accepté explicitement : voir la note de app/routers/health.py. Le corps
# n'est pas transmis (uvicorn le supprime), mais la page est bien rendue —
# préférer /health pour une sonde périodique, qui ne touche pas la base.
async def _liste_de_l_onglet(
    session: AsyncSession,
    tri: str,
    filtre: accueil_service.Filtre,
    limite: int,
    decalage: int = 0,
) -> tuple[list, list, bool]:
    """Ce que l'onglet courant fait afficher : des débats, ou des propositions (L6).

    Écrit une fois pour les trois routes de liste. Chacune servait la même paire
    `(tranche, il en reste)` ; laisser chacune choisir elle-même entre les deux objets
    aurait fini par en faire diverger une — et le défilement de `/debats/suite` aurait
    rendu des cartes de débats sous un onglet qui montre des propositions.

    La tranche des accords se découpe en Python, et non en base : la liste entière est
    déjà bornée à `ACCORDS_PAR_DEBAT` par débat ouvert, et un `OFFSET` en base aurait
    coupé le classement AVANT la règle (b), qui écarte des propositions en le
    parcourant. C'est le même piège que le L3 a rencontré avec son `LIMIT 3`.
    """
    if accueil_service.liste_de_propositions(tri):
        tous = await accueil_service.accords_en_cours(session, filtre)
        tranche = tous[decalage : decalage + limite]
        return [], tranche, len(tous) > decalage + limite
    conversations, encore = await accueil_service.page_de_debats(
        session, tri, limite, decalage, filtre=filtre
    )
    return conversations, [], encore


@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def home(
    request: Request,
    tri: str = "recent",
    theme: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    # Un `tri` inconnu retombe sur le défaut au lieu de lever : une URL partagée avec
    # une faute de frappe doit afficher la page, pas un 422. Même règle pour le filtre.
    # Les onglets proposés dépendent de la base depuis le L4 : les deux tris de mesure
    # n'apparaissent que si un débat est mesuré, et `?tri=clivant` retombe sur le défaut
    # tant que ce n'est pas le cas.
    tris = await accueil_service.tris_proposes(session)
    tri = accueil_service.tri_valide(tri, tris)
    mes_themes = await _themes_du_visiteur(request, session)
    filtre = accueil_service.filtre_valide(
        theme, request.query_params.get("mes-themes") == "1", mes_themes
    )
    # Cinq débats, et la mention qu'il en reste (chantier G10) : l'accueil est une
    # vitrine. La liste entière vit sur /debats, et c'est elle qui se déroule.
    #
    # Le quatrième onglet ne montre pas des débats mais des propositions (L6) : les
    # deux listes ne sont donc jamais construites toutes les deux, et l'écran n'en
    # rend qu'une.
    conversations, accords, encore = await _liste_de_l_onglet(
        session, tri, filtre, accueil_service.LIMITE_ACCUEIL
    )
    return _page(
        request,
        "home.html",
        user,
        conversations=conversations,
        accords=accords,
        liste_de_propositions=accueil_service.liste_de_propositions(tri),
        encore=encore,
        repartitions=await accueil_service.repartitions(session, conversations),
        depuis_ouverture=accueil_service.depuis_ouverture,
        tri=tri,
        tris=tris,
        tri_mesure=accueil_service.tri_sur_mesure(tri),
        base="/",
        filtre=filtre,
        adresse=accueil_service.adresse,
        libelle_theme=themes_service.libelle,
        mes_themes_reglees=bool(mes_themes),
        propositions_ouvertes=await accueil_service.propositions_ouvertes(
            session, filtre
        ),
        participants_semaine=await accueil_service.participants_de_la_semaine(session),
        periode_recalcul=accueil_service.periode_de_recalcul(),
        # « Le site en chiffres » (chantier I) reprend deux valeurs déjà calculées
        # ci-dessus et en ajoute deux.
        #
        # `total_propositions_ouvertes` est le MÊME compte que `propositions_ouvertes`,
        # SANS le filtre de thème — et c'est tout l'objet de cette variable en plus.
        # Le compteur de la barre de tri suit le filtre, et il le doit : il surmonte la
        # liste filtrée. Ce bloc-ci annonce « le site » ; lui donner un compte filtré le
        # ferait mentir dès qu'un thème est actif (`/?theme=sante` aurait affiché les
        # propositions de la santé sous un titre qui parle du site entier).
        total_propositions_ouvertes=await accueil_service.propositions_ouvertes(session),
        total_votes=await accueil_service.total_votes(session),
    )


@router.get("/debats", response_class=HTMLResponse)
async def debats(
    request: Request,
    tri: str = "recent",
    theme: str | None = None,
    nombre: int = accueil_service.LIMITE_DEBATS,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """La liste complète, par fournées de dix (chantier G10).

    `nombre` dit combien de débats afficher depuis le début, et non quelle page servir.
    C'est ce que fait le défilement, donc les deux chemins — avec et sans JavaScript —
    donnent le même écran. Il est **plafonné** : sans cela, `?nombre=100000` ferait
    rendre le catalogue entier sur une requête anonyme.
    """
    tris = await accueil_service.tris_proposes(session)
    tri = accueil_service.tri_valide(tri, tris)
    mes_themes = await _themes_du_visiteur(request, session)
    filtre = accueil_service.filtre_valide(
        theme, request.query_params.get("mes-themes") == "1", mes_themes
    )
    conversations, accords, encore = await _liste_de_l_onglet(
        session, tri, filtre, accueil_service.nombre_valide(nombre)
    )
    return _page(
        request,
        "debats.html",
        user,
        conversations=conversations,
        accords=accords,
        liste_de_propositions=accueil_service.liste_de_propositions(tri),
        encore=encore,
        par_fournee=accueil_service.LIMITE_DEBATS,
        repartitions=await accueil_service.repartitions(session, conversations),
        depuis_ouverture=accueil_service.depuis_ouverture,
        tri=tri,
        tris=tris,
        tri_mesure=accueil_service.tri_sur_mesure(tri),
        base="/debats",
        filtre=filtre,
        adresse=accueil_service.adresse,
        libelle_theme=themes_service.libelle,
        mes_themes_reglees=bool(mes_themes),
        propositions_ouvertes=await accueil_service.propositions_ouvertes(
            session, filtre
        ),
    )


@router.get("/debats/suite")
async def debats_suite(
    request: Request,
    tri: str = "recent",
    theme: str | None = None,
    decalage: int = 0,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """La fournée suivante, déjà mise en forme.

    Du HTML et non des données : les cartes sont rendues par la **même macro** que la
    page (`_carte_debat.html`). Renvoyer du JSON aurait demandé une seconde écriture de
    cette carte en JavaScript, qui aurait divergé de celle du serveur à la première
    retouche — et le même débat n'aurait pas eu la même allure selon qu'on l'a vu au
    chargement ou en descendant.

    Le décalage est plafonné comme `nombre` : c'est la même surface d'attaque.
    """
    # La fournée est ordonnée comme la page : le même tri, validé de la même façon.
    # Sans les onglets réellement proposés, `?tri=clivant` classerait ici une liste que
    # la page a rendue par date, et la onzième ligne romprait l'ordre des dix premières.
    tri = accueil_service.tri_valide(
        tri, await accueil_service.tris_proposes(session)
    )
    # La fournée porte le MÊME filtre que la page, sans quoi descendre dans une liste
    # filtrée la ferait déborder de débats hors filtre à partir de la onzième ligne.
    filtre = accueil_service.filtre_valide(
        theme,
        request.query_params.get("mes-themes") == "1",
        await _themes_du_visiteur(request, session),
    )
    decalage = accueil_service.nombre_valide(decalage, mini=0)
    conversations, accords, encore = await _liste_de_l_onglet(
        session, tri, filtre, accueil_service.LIMITE_DEBATS, decalage
    )
    # La fournée est rendue par la MÊME macro que la page, quelle que soit sa nature :
    # le navigateur colle du HTML, il n'assemble rien. Une seconde écriture de la carte
    # — ou de la ligne d'accord — en JavaScript aurait divergé à la première retouche.
    if accueil_service.liste_de_propositions(tri):
        macro = templates.get_template("public/_liste_accords.html").module
        html = "".join(str(macro.accord(a)) for a in accords)
        nombre = len(accords)
    else:
        repartitions = await accueil_service.repartitions(session, conversations)
        macro = templates.get_template("public/_carte_debat.html").module
        html = "".join(
            str(
                macro.carte(
                    c,
                    repartitions[c.id],
                    accueil_service.depuis_ouverture,
                    "/debats",
                    # Même niveau de titre que les cartes déjà en place sur la page
                    # « Débats » : une fournée collée à la suite ne doit pas rompre le
                    # plan de titres au milieu de la liste.
                    niveau=2,
                )
            )
            for c in conversations
        )
        nombre = len(conversations)
    return JSONResponse(
        {
            "html": html,
            "nombre": nombre,
            "encore": encore,
            "par_fournee": accueil_service.LIMITE_DEBATS,
        }
    )


@router.get("/c/{slug}/video")
async def conversation_video(
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """Le fichier vidéo de présentation, en lecture PUBLIQUE (chantier Vidéo, VIDEO-4).

    Déclarée AVANT `/c/{slug}`, par le même réflexe que `chiffres_page` ci-dessous.

    Distincte de `/moderation/conversations/{id}/video/fichier` (VIDEO-2) : celle-ci
    n'exige aucune authentification, mais n'existe que pour un débat déjà PUBLIÉ
    (`is_published`) — un débat encore `pending` ou `rejected` reste invisible ici
    même si son auteur en connaît le slug, exactement comme le reste de la page.
    """
    conversation = await conversations_service.by_slug(session, slug)
    if (
        conversation is None
        or not conversations_service.is_published(conversation)
        or not conversation.video_chemin
    ):
        raise HTTPException(status_code=404, detail="Vidéo introuvable")
    return FileResponse(video_service.racine_media() / conversation.video_chemin)


@router.get("/c/{slug}/video/couverture")
async def conversation_video_cover(
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """La miniature de la vidéo, publique — sert à la fois de bouton « lecture » sur
    la page du débat, de vignette dans les listes, et d'`og:image` au partage.
    """
    conversation = await conversations_service.by_slug(session, slug)
    if (
        conversation is None
        or not conversations_service.is_published(conversation)
        or not conversation.video_miniature_chemin
    ):
        raise HTTPException(status_code=404, detail="Miniature introuvable")
    return FileResponse(
        video_service.racine_media() / conversation.video_miniature_chemin
    )


@router.get("/c/{slug}/chiffres", response_class=HTMLResponse)
async def chiffres_page(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """Les chiffres publics d'un débat.

    **Déclarée AVANT `/c/{slug}`** : sans cela, `/c/quelque-chose/chiffres` ne
    correspondrait à rien, `{slug}` ne capturant pas la barre oblique. L'ordre des
    routes est ici une dépendance réelle, pas un rangement.

    Une seule liste, et une seule requête : les liens des participants n'y figurent pas
    (décision du client). Aucun `get_current_participant` — la page ne dépend de
    personne et ne doit pas créer de ligne à la lecture.
    """
    conversation = await conversations_service.by_slug(session, slug)
    if conversation is None or not conversations_service.is_published(conversation):
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    chiffres = await chiffres_service.chiffres_de(session, conversation)
    # Les liens morts (K3), déjà mis en français ici plutôt que dans le gabarit : une
    # date se formate dans le code, pas dans une expression Jinja imbriquée. Une seule
    # requête pour la page, et un dictionnaire vide quand rien n'est signalé — ce qui
    # est le cas ordinaire.
    signales = await liens_verification.signalements(session, [c.url for c in chiffres])
    return _page(
        request,
        "chiffres.html",
        user,
        conversation=conversation,
        chiffres=chiffres,
        date_lisible=chiffres_service.en_toutes_lettres,
        signale_le={
            url: chiffres_service.en_toutes_lettres(quand.date())
            for url, quand in signales.items()
        },
    )


def _parcours(comptes: tuple[int, int, int]) -> dict:
    """Le triplet du service, plus son total — la forme que le gabarit attend.

    Le total n'est pas recompté côté base : c'est la somme des trois, par construction.
    Il sert aussi de `votes_emis` à la barre de progression, pour que les deux blocs de
    la page ne comptent jamais séparément le même ensemble de votes.
    """
    accord, desaccord, passe = comptes
    return {
        "accord": accord,
        "desaccord": desaccord,
        "passe": passe,
        "total": accord + desaccord + passe,
    }


def _groupes_de_la_carte(carte) -> list[dict]:
    """Un groupe par entrée, avec sa vraie couleur et sa part — pour les cartes de
    groupe du panneau « La carte des avis ».

    Rien n'est décidé ici : les groupes sont ceux que `carte_service` a retenus (donc
    ceux que le dessin montre), les couleurs viennent de `carte_rendu`, et le nom est
    celui de l'enveloppe. La seule opération est la règle de trois.

    Les parts sont arrondies à l'entier et peuvent donc sommer à 99 ou 101 ; elles ne
    sont pas rectifiées, parce que rien ne les additionne à l'écran — chaque carte
    porte la sienne, et forcer la somme fausserait l'une d'elles pour un total que
    personne ne calcule.
    """
    if carte is None or not carte.groupes:
        return []
    couleurs = carte_rendu.couleurs_par_groupe(carte.groupes)
    total = sum(enveloppe.size for enveloppe in carte.groupes)
    if total <= 0:
        return []
    return [
        {
            "nom": enveloppe.name,
            "couleur": couleurs.get(enveloppe.name),
            "effectif": enveloppe.size,
            "part": round(enveloppe.size / total * 100),
        }
        for enveloppe in carte.groupes
    ]


@router.get("/c/{slug}", response_class=HTMLResponse)
async def conversation_page(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
    participant: Participant = Depends(get_current_participant),
) -> HTMLResponse:
    conversation = await conversations_service.by_slug(session, slug)
    if conversation is None or not conversations_service.is_published(conversation):
        raise HTTPException(status_code=404, detail="Conversation inconnue")

    # La carte est calculée UNE fois et servie deux fois : au tracé, et au tableau des
    # résultats, qui doit donner à chaque groupe la couleur qu'il porte sur la carte
    # juste au-dessus. Deux appels donneraient deux lectures du même run — donc le
    # même résultat, mais deux fois le travail, et une occasion de diverger.
    carte = await carte_service.carte(session, conversation, participant)
    # Le lien vers les chiffres n'apparaît QUE s'il y en a : un lien vers une page vide
    # promet ce qu'elle ne montre pas.
    n_chiffres = await chiffres_service.combien(session, conversation)

    return _page(
        request,
        "conversation.html",
        user,
        conversation=conversation,
        n_chiffres=n_chiffres,
        # Les deux chiffres de cadrage du bandeau de titre (chantier I2, instruction 9),
        # et les un ou deux chiffres sourcés qu'il montre en dessous.
        #
        # `participants_debat` n'est PAS `group.total_participants` : celui-ci compte les
        # participants du dernier calcul de groupes, n'existe pas tant qu'aucun calcul
        # n'a abouti, et exclut ceux qui n'ont pas assez voté pour être situés. Le
        # bandeau annonce « participants » tout court.
        #
        # `total_statements`, plus bas, donne déjà le nombre de propositions : c'est le
        # « sur N » de la carte de vote, et le bandeau le reprend plutôt que de le
        # recompter.
        participants_debat=await votes_service.participants_de(session, conversation),
        # La barre de progression de la carte de vote (chantier I2, instruction 10).
        # Le seuil est celui de CE débat — `min_user_votes_for` le borne par le nombre
        # de propositions —, jamais le 7 fixe de la maquette : annoncer 7 votes sur un
        # débat qui n'en compte que 3 serait une consigne intenable. Les deux valeurs
        # sont ensuite tenues à jour par la réponse de chaque vote, sans rechargement.
        seuil_situe=await groups_service.min_user_votes_for(session, conversation),
        # Le parcours de la personne sur ce débat (chantier I2, instruction 14) : il
        # sert au panneau « Votre parcours », et son total est aussi le `votes_emis` de
        # la barre de progression — une seule requête pour les deux, plutôt que deux
        # comptages du même ensemble qui finiraient par diverger.
        parcours=_parcours(
            await votes_service.parcours_de(session, conversation, participant)
        ),
        chiffres_en_tete=(
            await chiffres_service.chiffres_de(session, conversation)
        )[:2]
        if n_chiffres
        else [],
        is_open=conversation.state is ConversationState.open,
        accepts_statements=conversations_service.accepts_statements(conversation),
        remaining=await votes_service.remaining_count(
            session, conversation, participant
        ),
        group=await groups_service.for_participant(session, conversation, participant),
        total_statements=len(
            await conversations_service.visible_statements(session, conversation)
        ),
        # Carte des groupes (chantier D6). Le bloc « votre groupe » qui la précède
        # porte déjà le message d'un visiteur non situé : `carte_message=False` évite
        # de l'écrire deux fois dans le même encadré.
        carte_message=False,
        # Même source que l'accueil pour la fréquence de recalcul : deux gabarits
        # l'annoncent, une seule valeur la décide.
        periode_recalcul=accueil_service.periode_de_recalcul(),
        # Résultats par proposition, montrés en fin de parcours. Rendus par le serveur
        # dès l'arrivée sur la page plutôt que demandés en JavaScript au moment où le
        # bloc s'affiche : les données sont déjà en base, et un appel de plus à la fin
        # du parcours ferait attendre l'écran le plus attendu de la visite.
        resultats=await resultats_service.par_proposition(session, conversation),
        # Les cartes de groupe du panneau « La carte des avis » (chantier I2,
        # instruction 11).
        #
        # Elles sont construites depuis `carte.groupes`, la MÊME source que le dessin :
        # un second calcul de « quels groupes comptent » aurait fini par en lister un
        # que la carte ne montre pas, ou l'inverse. Leur nombre suit donc le débat — il
        # n'y en a pas forcément deux, contrairement à la maquette qui code « Groupe A »
        # et « Groupe B » en dur.
        #
        # La part est calculée sur le total des groupes DESSINÉS, pas sur tous les
        # participants : c'est ce qui fait que les parts somment à 100 et se lisent
        # comme « la moitié des gens situés », ce que la carte montre effectivement.
        groupes_carte=_groupes_de_la_carte(carte),
        # La liste fermée des motifs de signalement (MOD-3b), rendue par le SERVEUR dans
        # la fenêtre de signalement. Pas de `fetch` sur `/api/signalements/motifs` au
        # moment du clic : la fenêtre doit s'ouvrir instantanément sur un réseau lent,
        # et surtout la liste ne doit exister qu'à un seul endroit — ici elle vient de
        # la constante Python, comme partout ailleurs.
        #
        # Dans l'ordre du module, sans regroupement visible par famille : un
        # regroupement donnerait au participant une échelle de gravité, donc lui
        # dirait quelle case « pèse » le plus, et orienterait son choix.
        motifs_signalement=signalement_regles.MOTIFS,
        motif_texte_libre=signalement_regles.MOTIF_TEXTE_LIBRE,
        texte_libre_max=signalement_regles.TEXTE_LIBRE_MAX,
        motifs_max=signalement_regles.MOTIFS_MAX,
        # Les couleurs de groupe viennent de la MÊME source que la carte, sans quoi le
        # groupe A serait bleu sur le dessin et orange trois centimètres plus bas.
        couleurs_groupes=(
            carte_rendu.couleurs_par_groupe(carte.groupes)
            if carte is not None
            else {}
        ),
        # Les noms de groupe posés sur la carte viennent de la MÊME source que ceux
        # du bloc « Ce qui caractérise chaque groupe » : seuls les noms validés par un
        # modérateur y figurent (E5), et deux lectures séparées finiraient par montrer
        # deux noms différents pour le même groupe sur la même page.
        **carte_rendu.contexte(
            carte,
            {
                stable: donnees["nom"]
                for stable, donnees in (
                    await nommage_service.noms_affichables(session, conversation.id)
                ).items()
            },
        ),
    )


# --- compte ----------------------------------------------------------------------


@router.get("/compte/connexion", response_class=HTMLResponse)
async def login_form(request: Request, suivant: str = "/") -> HTMLResponse:
    return _page(request, "login.html", None, suivant=suivant)


@router.post("/compte/connexion")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    suivant: str = Form("/"),
    session: AsyncSession = Depends(get_session),
):
    # Même plafond que /moderation/login : cette page ouvre le même cookie de session,
    # donc /admin par ricochet. Ne protéger que l'écran de modération se contournerait
    # en changeant d'URL.
    if not await rate_limit.login_allowed(session, client_ip(request)):
        return _page(
            request,
            "login.html",
            None,
            suivant=suivant,
            error=(
                "Trop de tentatives de connexion depuis cette adresse. "
                "Réessayez dans une minute."
            ),
        )

    user = await authenticate(session, username, password)
    if user is None:
        return _page(
            request,
            "login.html",
            None,
            suivant=suivant,
            error="Adresse ou mot de passe incorrect.",
        )
    response = RedirectResponse(chemin_local(suivant), status_code=303)
    await establish_session(session, user, request, response)
    return response


@router.get("/compte/inscription", response_class=HTMLResponse)
async def register_form(request: Request, suivant: str = "/") -> HTMLResponse:
    return _page(request, "register.html", None, suivant=suivant)


@router.post("/compte/inscription")
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    suivant: str = Form("/"),
    session: AsyncSession = Depends(get_session),
):
    manager = user_manager_for(session)
    try:
        creation = UserCreate(email=username, password=password)
    except ValidationError:
        # `type="email"` du navigateur est plus permissif que le validateur du
        # serveur : il laisse passer des domaines réservés (`.invalid`, `.local`),
        # des points doublés, des libellés trop longs. Sans ce filet, une adresse
        # que le navigateur a acceptée fait remonter une ValidationError brute —
        # c'est-à-dire une page 500 sur le formulaire d'inscription, à qui a
        # simplement fait une faute de frappe. Découvert le 4 septembre 2026 en
        # vérifiant la mise en production de F2 ; le défaut est antérieur au
        # chantier F.
        #
        # Le message ne reprend pas celui de pydantic : il est en anglais, et il
        # explique la règle violée plutôt que le geste à faire.
        return _page(
            request,
            "register.html",
            None,
            suivant=suivant,
            error="Cette adresse e-mail n'est pas valide. Vérifiez qu'elle est de "
            "la forme nom@domaine.fr.",
            champ="username",
            username=username,
        )

    try:
        user = await manager.create(creation, request=request)
    except UserAlreadyExists:
        return _page(
            request,
            "register.html",
            None,
            suivant=suivant,
            error="Un compte existe déjà avec cette adresse. Connectez-vous, ou "
            "utilisez une autre adresse.",
            champ="username",
            # L'adresse est réaffichée : la faire retaper après un refus ajoute une
            # corvée à une contrariété. Le mot de passe, lui, ne revient jamais —
            # il n'a rien à faire dans le HTML d'une page renvoyée.
            username=username,
        )
    except InvalidPasswordException as exc:
        return _page(
            request,
            "register.html",
            None,
            suivant=suivant,
            error=str(exc.reason),
            champ="password",
            username=username,
        )

    # L'inscription connecte directement : demander de se reconnecter juste après
    # avoir choisi un mot de passe est une friction sans contrepartie.
    #
    # Puis vient l'étape des thèmes (question 8), et trois choses la rendent acceptable :
    # elle est APRÈS l'inscription — le compte existe déjà, rien n'est perdu si on
    # s'arrête là ; elle est **passable d'un lien** ; et elle ne s'affiche que pour qui
    # n'a jamais répondu. Quelqu'un qui a réglé ses thèmes en anonyme puis s'inscrit ne
    # se voit pas reposer la question : c'est la même ligne `participant`, elle porte
    # déjà la réponse.
    destination = chemin_local(suivant)
    if await _themes_du_visiteur(request, session) is None:
        destination = f"/mes-themes?retour={quote(destination, safe='')}"
    response = RedirectResponse(destination, status_code=303)
    await establish_session(session, user, request, response)
    return response


@router.post("/compte/deconnexion")
async def logout(suivant: str = Form("/")) -> RedirectResponse:
    response = RedirectResponse(chemin_local(suivant), status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# --- mot de passe oublié ---------------------------------------------------------


@router.get("/compte/mot-de-passe-oublie", response_class=HTMLResponse)
async def forgot_form(request: Request, envoye: int = 0) -> HTMLResponse:
    return _page(request, "forgot.html", None, sent=bool(envoye))


@router.post("/compte/mot-de-passe-oublie")
async def forgot_submit(
    request: Request,
    username: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Demande de réinitialisation, plafonnée.

    Chaque envoi consomme le quota du relais : sans plafond, l'endpoint permet de
    l'épuiser et d'abîmer la réputation du domaine expéditeur. Le refus est
    **silencieux** — même page qu'un envoi réussi — car distinguer les deux
    révélerait à la fois quelles adresses sont inscrites et lesquelles sont visées.
    """
    client_ip = request.client.host if request.client else None
    if await rate_limit.password_reset_allowed(session, username, client_ip):
        manager = user_manager_for(session)
        user = await manager.user_db.get_by_email(username)
        if user is not None:
            await manager.forgot_password(user, request)

    # POST/Redirect/GET : sans redirection, un simple F5 renverrait un second e-mail.
    return RedirectResponse("/compte/mot-de-passe-oublie?envoye=1", status_code=303)


@router.get("/compte/reinitialisation", response_class=HTMLResponse)
async def reset_form(request: Request, token: str = "") -> HTMLResponse:
    """Page ouverte depuis le lien reçu par e-mail.

    Elle ne consomme pas le jeton : elle l'affiche dans un champ caché et attend un
    POST. Un lien simplement pré-chargé par un antispam ne peut donc pas réinitialiser
    le mot de passe à l'insu du destinataire.
    """
    if not token:
        return _page(request, "reset.html", None, error="Lien incomplet ou expiré.")
    return _page(request, "reset.html", None, token=token)


@router.post("/compte/reinitialisation", response_class=HTMLResponse)
async def reset_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirmation: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    if password != confirmation:
        return _page(
            request,
            "reset.html",
            None,
            token=token,
            error="Les deux mots de passe ne correspondent pas. Ressaisissez la "
            "confirmation.",
            champ="confirmation",
        )
    manager = user_manager_for(session)
    try:
        await manager.reset_password(token, password, request)
    except InvalidPasswordException as exc:
        return _page(
            request,
            "reset.html",
            None,
            token=token,
            error=str(exc.reason),
            champ="password",
        )
    except Exception:  # noqa: BLE001 — jeton invalide, expiré, ou compte inactif
        return _page(
            request,
            "reset.html",
            None,
            error="Ce lien n'est plus valable. Demandez-en un nouveau.",
        )
    return _page(request, "reset.html", None, done=True)


@router.get("/compte/verification", response_class=HTMLResponse)
async def verify(
    request: Request,
    token: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    manager = user_manager_for(session)
    if not token:
        return _page(request, "verify.html", None, error="Lien incomplet.")
    try:
        await manager.verify(token, request)
    except UserAlreadyVerified:
        return _page(request, "verify.html", None, already=True)
    except Exception:  # noqa: BLE001 — jeton invalide ou expiré
        return _page(
            request,
            "verify.html",
            None,
            error="Ce lien n'est plus valable.",
        )
    return _page(request, "verify.html", None, done=True)


# --- profil ----------------------------------------------------------------------

#: Bornes du formulaire « Modifier mon profil ». `display_name` n'a pas besoin des
#: 120 caractères que la colonne autorise (héritage de l'e-mail le plus long
#: possible) : ce n'est pas un identifiant, juste un nom lu par d'autres personnes,
#: et une valeur démesurée casserait l'en-tête sur téléphone. `bio` reprend la borne
#: de la colonne (0014_bio_du_profil.py) — la même limite aux deux endroits évite
#: qu'une saisie acceptée par l'écran soit ensuite refusée par la base.
DISPLAY_NAME_MAX = 40
BIO_MAX = 280


async def _contexte_profil(session: AsyncSession, user: User) -> dict:
    """Ce que `/compte` affiche, hors erreur de formulaire — factorisé parce que
    `profile` (GET) et `profile_update` (POST, en cas de refus) rendent le même
    gabarit avec le même contenu autour du formulaire."""
    return {
        "progress": await gamification.progress_for(session, user),
        "levels": gamification.LEVELS,
        # `/compte` n'avait aucun réglage des thèmes jusqu'ici. Celui-ci n'y est pas
        # RECOPIÉ : la page montre l'état et mène à l'écran de réglage, qui est le
        # même pour tout le monde. Deux formulaires pour une préférence auraient
        # divergé.
        #
        # Le participant est lu par le COMPTE et non par le cookie anonyme : sur
        # cette page la personne est authentifiée, et son cookie de participant a
        # pu être réémis depuis. C'est le compte qui fait autorité ici.
        **_bloc_themes(await participants_service.by_user(session, user)),
    }


@router.get("/compte", response_class=HTMLResponse)
async def profile(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
):
    if user is None:
        return RedirectResponse("/compte/connexion?suivant=/compte", status_code=303)
    return _page(request, "profile.html", user, **await _contexte_profil(session, user))


@router.post("/compte/profil")
async def profile_update(
    request: Request,
    display_name: str = Form(""),
    bio: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
):
    """Nom affiché et bio dans un seul formulaire — pas deux, pour la même raison
    que les thèmes plus bas : une préférence de profil qui se réglerait à deux
    endroits finirait par s'y contredire.

    Les deux champs sont facultatifs : le repli reste l'e-mail (gabarits), et une
    bio vide efface la précédente plutôt que de la laisser en place sans moyen de
    revenir en arrière.
    """
    if user is None:
        return RedirectResponse("/compte/connexion?suivant=/compte", status_code=303)

    display_name = display_name.strip()
    bio = bio.strip()

    erreur: str | None = None
    champ: str | None = None
    if len(display_name) > DISPLAY_NAME_MAX:
        erreur = f"Le nom affiché ne peut pas dépasser {DISPLAY_NAME_MAX} caractères."
        champ = "display_name"
    elif len(bio) > BIO_MAX:
        erreur = f"La description ne peut pas dépasser {BIO_MAX} caractères."
        champ = "bio"

    if erreur:
        return _page(
            request,
            "profile.html",
            user,
            error=erreur,
            champ=champ,
            display_name_saisi=display_name,
            bio_saisie=bio,
            **await _contexte_profil(session, user),
        )

    user.display_name = display_name or None
    user.bio = bio or None
    await session.commit()
    # POST/Redirect/GET : sans redirection, un F5 réenverrait le formulaire.
    return RedirectResponse("/compte", status_code=303)


# --- pages légales ----------------------------------------------------------------


# --- pages d'explication, à rédiger ------------------------------------------------
#
# Deux pages dont le TEXTE reste à écrire par le client (« À DÉFINIR » dans le
# gabarit). La route et le gabarit existent dès maintenant pour deux raisons : que le
# lien de la navigation mène quelque part plutôt que sur une 404, et que l'ajout du
# texte ne demande plus qu'une modification de gabarit — pas une route, pas un test.
#
# « Règles de modération » répond en outre à un point ouvert du bilan du chantier C :
# « rendre les règles de modération publiques ».


@router.get("/manifeste", response_class=HTMLResponse)
async def manifesto(
    request: Request, user: User | None = Depends(current_user_optional)
) -> HTMLResponse:
    """Le manifeste (chantier I) : la ROUTE et le gabarit existent, le TEXTE non.

    La rédaction est une étape séparée, plus tard (section 6 de la consigne du
    chantier I). Faire exister la page dès maintenant évite que les liens qui y
    mènent — accueil, cartes « pourquoi ce site existe », navigation — pointent sur
    une 404 en attendant.
    """
    return _page(request, "manifeste.html", user)


@router.get("/comment-ca-marche", response_class=HTMLResponse)
async def how_it_works(
    request: Request, user: User | None = Depends(current_user_optional)
) -> HTMLResponse:
    return _page(request, "comment-ca-marche.html", user)


@router.get("/regles-de-moderation", response_class=HTMLResponse)
async def moderation_rules(
    request: Request, user: User | None = Depends(current_user_optional)
) -> HTMLResponse:
    return _page(request, "regles-de-moderation.html", user)


@router.get("/mentions-legales", response_class=HTMLResponse)
async def legal_notice(
    request: Request, user: User | None = Depends(current_user_optional)
) -> HTMLResponse:
    return _page(
        request,
        "mentions-legales.html",
        user,
        contact=settings.contact_email,
        maj=LEGAL_UPDATED,
    )


@router.get("/confidentialite", response_class=HTMLResponse)
async def privacy_policy(
    request: Request, user: User | None = Depends(current_user_optional)
) -> HTMLResponse:
    return _page(
        request,
        "confidentialite.html",
        user,
        contact=settings.contact_email,
        maj=CONFIDENTIALITE_UPDATED,
        # La note du MOD-3a est PASSÉE au gabarit, jamais recopiée dedans : c'est le même
        # texte que celui affiché au responsable sur `/moderation/signalements`, et deux
        # copies d'une phrase juridique divergent toujours — celle qu'on corrige et celle
        # qu'on oublie.
        note_confidentialite=signalement_regles.NOTE_CONFIDENTIALITE,
    )


# --- suppression de son compte ou de ses données ----------------------------------


async def _participant_du_cookie(request: Request, session: AsyncSession):
    """Participant anonyme porté par le cookie, sans jamais en créer un.

    `get_current_participant` créerait une ligne à la simple visite : sur une page de
    suppression, ce serait grotesque — on fabriquerait la donnée que la personne vient
    effacer.
    """
    token = read_anon_cookie(request)
    if not token:
        return None
    return await participants_service.by_anon_token(session, token)


@router.get("/compte/suppression", response_class=HTMLResponse)
async def delete_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """Page d'effacement — pour un compte, ou pour un participant sans compte."""
    participant = None if user else await _participant_du_cookie(request, session)
    return _page(
        request,
        "suppression.html",
        user,
        anonyme=participant is not None,
    )


@router.post("/compte/suppression", response_class=HTMLResponse)
async def delete_submit(
    request: Request,
    password: str = Form(""),
    confirmation: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """Efface définitivement. Irréversible, donc confirmé deux fois.

    Avec un compte : le mot de passe est redemandé — un cookie volé ne doit pas
    suffire à détruire l'historique de quelqu'un. Sans compte : la possession du
    cookie signé fait preuve, et une case à cocher explicite fait le reste.
    """
    if user is not None:
        if await authenticate(session, user.email, password) is None:
            return _page(
                request,
                "suppression.html",
                user,
                anonyme=False,
                error="Mot de passe incorrect. Rien n'a été supprimé.",
                champ="password",
            )
        await accounts_service.delete_account(session, user)
    else:
        participant = await _participant_du_cookie(request, session)
        if participant is None:
            return _page(
                request,
                "suppression.html",
                None,
                anonyme=False,
                error="Aucune donnée à supprimer pour ce navigateur.",
            )
        if confirmation != "1":
            return _page(
                request,
                "suppression.html",
                None,
                anonyme=True,
                error="Cochez la case pour confirmer.",
                champ="confirmation",
            )
        await accounts_service.delete_anonymous_participant(session, participant)

    # Les deux cookies partent avec les données : en garder un ferait pointer la
    # session suivante vers une ligne disparue.
    response = _page(request, "suppression.html", None, done=True)
    response.delete_cookie(SESSION_COOKIE_NAME)
    clear_anon_cookie(response)
    return response


# --- centres d'intérêt ------------------------------------------------------------


@router.get("/mes-themes", response_class=HTMLResponse)
async def mes_themes_form(
    request: Request,
    retour: str = "/",
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """Le réglage des thèmes, ouvert à TOUS — anonymes compris.

    C'est la réponse à la question 9, et ce n'est pas une faveur : les visiteurs sans
    compte sont la majorité sur un site qui n'en exige pas. Une préférence réservée aux
    inscrits n'aurait servi presque personne.

    En lecture, le participant n'est pas créé : quelqu'un qui ouvre la page et repart
    sans cocher ne doit pas laisser une ligne derrière lui.
    """
    return _page(
        request,
        "mes_themes.html",
        user,
        themes=themes_service.THEMES,
        choisis=await _themes_du_visiteur(request, session) or [],
        retour=chemin_local(retour),
    )


@router.post("/mes-themes")
async def mes_themes_submit(
    request: Request,
    retour: str = Form("/"),
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
    participant: Participant = Depends(get_current_participant),
):
    """Enregistre les thèmes sur le PARTICIPANT, et non sur le compte.

    Décision structurante du chantier (J1) : la même ligne `participant` est rattachée
    au compte le jour où la personne s'inscrit, donc la préférence la suit sans transfert
    à écrire. C'est ici que le participant est créé s'il ne l'était pas — enregistrer un
    choix est un geste, pas une visite.
    """
    form = await request.form()
    demandes = [str(v) for k, v in form.multi_items() if k == "themes"]
    try:
        # `valider_interets`, et non `valider_suggestion` : aucun plafond ici — c'est
        # une préférence personnelle, pas l'étiquetage d'un débat. Zéro thème reste un
        # choix recevable, « je ne veux pas filtrer » ; seul un code inventé est refusé.
        codes = themes_service.valider_interets(demandes)
    except ValueError as exc:
        return _page(
            request,
            "mes_themes.html",
            user,
            themes=themes_service.THEMES,
            choisis=demandes,
            retour=chemin_local(retour),
            error=str(exc).capitalize(),
        )

    # Une liste vide est écrite comme telle, et non remise à NULL : quelqu'un qui
    # décoche tout a RÉPONDU, et ne doit pas se voir reposer la question à l'inscription
    # suivante. C'est toute la différence entre « aucun thème » et « jamais réglé ».
    participant.themes = codes
    await session.commit()
    # POST/Redirect/GET : sans redirection, un F5 réenverrait le formulaire.
    return RedirectResponse(chemin_local(retour), status_code=303)


# --- proposer une conversation ---------------------------------------------------


@router.get("/proposer", response_class=HTMLResponse)
async def propose_form(
    request: Request,
    envoye: int = 0,
    user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    return _page(
        request,
        "propose.html",
        user,
        minimum=conversations_service.MIN_SEED_STATEMENTS,
        maximum=conversations_service.MAX_SEED_STATEMENTS,
        liens_max=LIENS_MAX,
        themes=themes_service.THEMES,
        themes_max=themes_service.THEMES_MAX,
        sent=bool(envoye),
        video_max_octets=settings.video_max_octets,
        video_duree_max=video_service.DUREE_MAX_SECONDES,
    )


@router.post("/proposer", response_class=HTMLResponse)
async def propose_submit(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
    participant: Participant = Depends(get_current_participant),
):
    """Proposition d'une conversation, ouverte aux anonymes comme aux comptes.

    Le compte n'est pas exigé — c'est le même contrat que le vote et la proposition
    de proposition. Il change seulement ce qui est décerné : sans compte, pas de
    badge ni de niveau (ils appartiennent au compte).
    """
    form = await request.form()
    title = str(form.get("title", "")).strip()
    description = str(form.get("description", "")).strip() or None

    # Les propositions sont relevées AVANT filtrage des vides, avec leurs liens, pour
    # que chaque proposition reste appariée aux siens. Le formulaire émet exactement
    # LIENS_MAX couples (adresse, libellé) par proposition, dans l'ordre : filtrer les
    # textes vides d'abord décalerait les liens d'une proposition sur une autre.
    textes_bruts = [
        str(value) for key, value in form.multi_items() if key == "statements"
    ]
    urls = [str(value) for key, value in form.multi_items() if key == "source_url"]
    labels = [str(value) for key, value in form.multi_items() if key == "source_label"]

    amorces: list[conversations_service.Amorce] = []
    erreur_lien = None
    apparie = len(urls) == len(labels) == LIENS_MAX * len(textes_bruts)
    for rang, texte in enumerate(textes_bruts):
        if not texte.strip():
            continue
        saisies = (
            list(zip(urls[rang * LIENS_MAX:(rang + 1) * LIENS_MAX],
                     labels[rang * LIENS_MAX:(rang + 1) * LIENS_MAX]))
            if apparie
            else []
        )
        try:
            liens = tuple(valider_liens(saisies))
        except LienInvalide as exc:
            # Le rang est nommé : sur un formulaire à dix propositions, « adresse
            # invalide » sans dire laquelle oblige à toutes les relire.
            erreur_lien = f"Proposition {rang + 1} — {exc}"
            liens = ()
        amorces.append(conversations_service.Amorce(texte.strip(), liens))

    statements = [amorce.text for amorce in amorces]
    themes_choisis = [
        str(value) for key, value in form.multi_items() if key == "themes"
    ]
    # Ce qui sera réaffiché en cas d'erreur : les couples tels qu'ils ont été tapés,
    # y compris les propositions vides — sinon un envoi refusé renverrait un
    # formulaire où les lignes se sont décalées sous les yeux de la personne.
    champs = [
        (
            texte,
            list(zip(urls[rang * LIENS_MAX:(rang + 1) * LIENS_MAX],
                     labels[rang * LIENS_MAX:(rang + 1) * LIENS_MAX]))
            if apparie
            else [("", "")] * LIENS_MAX,
        )
        for rang, texte in enumerate(textes_bruts)
    ]

    minimum = conversations_service.MIN_SEED_STATEMENTS
    maximum = conversations_service.MAX_SEED_STATEMENTS
    error = None
    champ = None
    erreur_theme = None
    try:
        # `valider_suggestion` et non `valider_choix` : `/proposer` ne publie pas, il
        # dépose dans la file. Le proposeur SUGGÈRE — au plus trois thèmes, aucun code
        # inventé — et le modérateur tranche à l'approbation, où « au moins un » est
        # exigé. Rendre la case obligatoire ici ajouterait une étape entre « je veux
        # participer » et « je participe », ce que la question 8 écarte.
        themes_service.valider_suggestion(themes_choisis)
    except ValueError as exc:
        erreur_theme = str(exc)

    if erreur_theme:
        # Avant les liens : c'est une case cochée en trop, la faute la plus vite
        # corrigée. Signaler d'abord une adresse mal formée ferait traverser deux fois
        # le formulaire pour deux fautes commises en même temps.
        error = f"Pas plus de {themes_service.THEMES_MAX} thèmes — {erreur_theme}."
        champ = "themes"
    elif erreur_lien:
        error = erreur_lien
        champ = "statements"
    elif not title:
        error = "Un titre est nécessaire."
        champ = "title"
    elif len(title) > conversations_service.MAX_TITLE_LENGTH:
        # La colonne est bornée : sans ce contrôle, un titre plus long tombe en
        # erreur de base (500) au lieu d'un message.
        error = (
            f"Le titre ne peut pas dépasser "
            f"{conversations_service.MAX_TITLE_LENGTH} caractères."
        )
        champ = "title"
    elif len(statements) != len({t.casefold() for t in statements}):
        error = "Deux propositions de départ sont identiques. Reformulez-en une."
        champ = "statements"
    elif len(statements) < minimum:
        error = f"Il faut au moins {minimum} propositions de départ."
        champ = "statements"
    elif len(statements) > maximum:
        error = f"Pas plus de {maximum} propositions de départ."
        champ = "statements"
    elif any(len(t) > conversations_service.MAX_STATEMENT_LENGTH for t in statements):
        error = (
            f"Une proposition ne peut pas dépasser "
            f"{conversations_service.MAX_STATEMENT_LENGTH} caractères."
        )
        champ = "statements"
    # Facteur commun aux réaffichages du formulaire — trois maintenant que la vidéo
    # (VIDEO-3) s'ajoute au titre/aux liens/au plafond quotidien comme motif de refus.
    def redisplay(message: str, champ_en_cause: str | None = None) -> HTMLResponse:
        return _page(
            request,
            "propose.html",
            user,
            minimum=minimum,
            maximum=maximum,
            error=message,
            champ=champ_en_cause,
            title=title,
            description=description or "",
            champs=champs,
            liens_max=LIENS_MAX,
            themes=themes_service.THEMES,
            themes_max=themes_service.THEMES_MAX,
            themes_choisis=themes_choisis,
            video_max_octets=settings.video_max_octets,
            video_duree_max=video_service.DUREE_MAX_SECONDES,
        )

    if error:
        return redisplay(error, champ)

    # Plafond vérifié APRÈS la validation : une faute de frappe ne doit pas consommer
    # le quota de la journée. Contrairement au mot de passe oublié, le refus est ici
    # **explicite** — il n'y a rien à révéler sur l'existence d'un compte, et un envoi
    # silencieusement ignoré ferait croire à une proposition enregistrée.
    client_ip = request.client.host if request.client else None
    if not await rate_limit.conversation_proposal_allowed(
        session, participant.id, client_ip
    ):
        return redisplay(
            "Vous avez atteint la limite de propositions pour aujourd'hui "
            f"({rate_limit.CONVERSATIONS_PER_PARTICIPANT} débats par "
            "tranche de 24 heures). Votre texte est conservé ci-dessous : "
            "réessayez demain."
        )

    # La vidéo (facultative) est traitée APRÈS le plafond quotidien, pour la même
    # raison : un transcodage — quelques secondes de ffmpeg — ne doit pas se payer
    # pour un envoi qui allait de toute façon être refusé. Transcodée AVANT la création
    # de la conversation : un échec redonne le formulaire sans avoir rien enregistré
    # à moitié, comme toute autre erreur de ce formulaire.
    resultat_video: video_service.ResultatTranscodage | None = None
    fichier_video = form.get("video")
    if isinstance(fichier_video, UploadFile) and fichier_video.filename:
        try:
            chemin_brut = await video_service.recevoir(
                fichier_video.read,
                settings.video_max_octets,
                suffixe=Path(fichier_video.filename).suffix,
            )
        except video_service.FichierTropGros:
            return redisplay(
                f"Ce fichier pèse plus de {settings.video_max_octets // 1_000_000} "
                "Mo, la limite pour ce site. Réessayez avec un fichier plus léger, "
                "ou proposez votre débat sans vidéo.",
                "video",
            )
        finally:
            await fichier_video.close()

        try:
            resultat_video = await video_service.preparer(
                chemin_brut, sous_dossier="propositions"
            )
        except video_service.VideoTropLongue as erreur:
            return redisplay(
                f"Votre vidéo dure {erreur.duree:.0f} s, la limite est de "
                f"{video_service.DUREE_MAX_SECONDES:.0f} s. Réessayez avec un extrait "
                "plus court, ou proposez votre débat sans vidéo.",
                "video",
            )
        except video_service.OutilAbsent as erreur:
            # Ne concerne pas qui a envoyé le fichier : panne de serveur, pas faute de
            # frappe — même distinction qu'à l'écran de modération (VIDEO-2).
            logger.error("ffmpeg/ffprobe absent — proposition d'un participant : %s", erreur)
            if settings.alert_email:
                await email.send_email(
                    settings.alert_email,
                    "Chantier C — ffmpeg absent sur le serveur",
                    (
                        "L'envoi d'une vidéo depuis le formulaire de proposition a "
                        f"échoué : {erreur}\n\n"
                        "ffmpeg/ffprobe n'est pas installé sur ce serveur — c'est une "
                        "dépendance SYSTÈME, pas une bibliothèque Python."
                    ),
                )
            return redisplay(
                "Un problème technique empêche l'envoi de vidéos pour l'instant. "
                "Vous pouvez proposer votre débat sans vidéo, ou réessayer plus tard.",
                "video",
            )
        except video_service.ErreurVideo:
            return redisplay(
                "Ce fichier n'a pas pu être traité comme une vidéo. Vérifiez le "
                "format et réessayez, ou proposez votre débat sans vidéo.",
                "video",
            )
        finally:
            chemin_brut.unlink(missing_ok=True)
    elif isinstance(fichier_video, UploadFile):
        await fichier_video.close()

    await conversations_service.propose_conversation(
        session,
        participant=participant,
        title=title,
        description=description,
        statements=amorces,
        themes=themes_choisis,
        video=resultat_video,
    )
    # POST/Redirect/GET : sans redirection, un F5 après envoi créerait une seconde
    # proposition identique.
    return RedirectResponse("/proposer?envoye=1", status_code=303)


# --- la voie de recours (chantier Modération, MOD-3b) -------------------------------


async def _proposition_du_jeton(session: AsyncSession, jeton: str):
    """La proposition qu'ouvre ce jeton, ou une 404.

    **404 et jamais 403.** Un 403 dirait « ce jeton existe mais vous n'y avez pas
    droit », c'est-à-dire confirmerait à qui essaie des jetons qu'il a touché juste. Le
    même 404 couvre les trois cas : jeton inconnu, jeton d'une proposition remise en
    circulation, jeton jamais émis.
    """
    statement = await signalement_file.proposition_contestable(session, jeton)
    if statement is None:
        raise HTTPException(status_code=404, detail="Page introuvable")
    return statement


def _exposé(statement) -> dict:
    """Les trois éléments de l'exposé des motifs : le texte, le motif, la date.

    Le motif est rendu en toutes lettres par la liste fermée
    (`app/services/signalement.py`) et jamais sous son code : « propos_haineux » n'est
    pas un exposé des motifs.

    **Le détecteur du MOD-1 n'est PAS appelé ici**, alors qu'il saurait souligner la
    portion en cause. Ses signaux (`cible_personnes`, `deux_idees`…) sont une autre
    grammaire que les dix motifs de signalement, et les mêler ferait reprocher à
    quelqu'un quelque chose qui n'a pas motivé le retrait.
    """
    code = statement.retire_motif or ""
    connu = code in signalement_regles.CODES
    return {
        "motif_libelle": signalement_regles.libelle(code) if connu else "Non précisé",
        "motif_phrase": (
            signalement_regles.motif(code).phrase if connu else "elle a été signalée"
        ),
        "retire_le": (
            statement.retire_le.strftime("%d/%m/%Y") if statement.retire_le else "—"
        ),
    }


@router.get("/contester/{jeton}", response_class=HTMLResponse)
async def contester_form(
    jeton: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """L'exposé des motifs et le formulaire de demande de réexamen.

    Aucune identification n'est demandée, et c'est structurel : l'auteur d'une
    proposition est anonyme dans 98 % des cas. Le jeton remplace le compte.
    """
    statement = await _proposition_du_jeton(session, jeton)
    return _page(
        request,
        "contester.html",
        user,
        statement=statement,
        jeton=jeton,
        maximum=signalement_file.CONTESTATION_MAX,
        envoyee=False,
        **_exposé(statement),
    )


@router.post("/contester/{jeton}", response_class=HTMLResponse)
async def contester_submit(
    jeton: str,
    request: Request,
    texte: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """Enregistre la demande, la journalise, et le dit.

    Pas de POST/Redirect/GET ici, contrairement au reste du site : la confirmation
    REMPLACE le formulaire dans la même réponse, et il n'y a donc rien à rejouer au F5
    — le rechargement redemande la page en GET, qui réaffiche le formulaire vide. Une
    redirection aurait exigé de porter l'état de confirmation dans l'URL, c'est-à-dire
    d'ajouter un paramètre à une adresse qui contient déjà un secret.
    """
    statement = await _proposition_du_jeton(session, jeton)
    try:
        await signalement_file.deposer_contestation(session, statement, texte)
    except signalement_file.ContestationInvalide as erreur:
        return _page(
            request,
            "contester.html",
            user,
            statement=statement,
            jeton=jeton,
            maximum=signalement_file.CONTESTATION_MAX,
            envoyee=False,
            error=str(erreur),
            texte=texte,
            **_exposé(statement),
        )
    return _page(
        request,
        "contester.html",
        user,
        statement=statement,
        jeton=jeton,
        maximum=signalement_file.CONTESTATION_MAX,
        envoyee=True,
        **_exposé(statement),
    )


# --- le rappel à l'auteur (chantier Modération, MOD-6, lot de repli) -----------------


@router.post("/rappels/{statement_id}/fermer")
async def fermer_rappel(
    statement_id: int,
    request: Request,
    suivant: str = Form("/"),
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> RedirectResponse:
    """Ferme un rappel, pour de bon.

    Un POST et non un lien : cela écrit en base, et un GET qui écrit se déclenche tout
    seul au premier passage d'un aspirateur de pages.

    L'appartenance est vérifiée par le service : sans ce contrôle, il suffirait de
    deviner un identifiant de proposition pour que son auteur n'apprenne jamais qu'elle a
    été retirée. Un refus redirige comme un succès — dire « ce n'est pas à vous »
    confirmerait l'existence de la proposition.
    """
    participant = await _participant_du_visiteur(request, session, user)
    if participant is not None:
        await rappels_service.fermer(session, participant, statement_id)
    return RedirectResponse(chemin_local(suivant), status_code=303)


async def _participant_du_visiteur(
    request: Request, session: AsyncSession, user: User | None
) -> Participant | None:
    """Le participant derrière ce visiteur, compte ou jeton de cookie — ou rien.

    La même identification que pour voter et pour signaler : aucune n'a été inventée
    pour les rappels, et c'est ce qui permet qu'un auteur sans compte soit reconnu.
    """
    if user is not None:
        return await participants_service.by_user(session, user)
    jeton = read_anon_cookie(request)
    if jeton:
        return await participants_service.by_anon_token(session, jeton)
    return None


@router.post("/reformulations/{reformulation_id}/{reponse}")
async def repondre_a_une_reformulation(
    reformulation_id: int,
    reponse: str,
    request: Request,
    suivant: str = Form("/"),
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> RedirectResponse:
    """L'auteur accepte ou refuse la reformulation qu'on lui propose (MOD-10).

    **Accepter remplace son texte et conserve ses votes** — l'arbitrage de dom du
    15 septembre 2026. Refuser ne change rien et laisse la plainte ouverte : la main
    revient au responsable au lieu de lui être prise.

    Un POST et non un lien, pour la raison du MOD-6 en plus grave : ici un GET qui écrit
    ferait réécrire le texte de quelqu'un au premier passage d'un aspirateur de pages.

    Comme au MOD-6, un refus redirige exactement comme un succès : dire « ce n'est pas à
    vous » confirmerait l'existence de l'échange, et l'appartenance est vérifiée par le
    service, jamais ici.
    """
    gestes = {
        "accepter": reformulation_service.accepter,
        "refuser": reformulation_service.refuser,
    }
    if reponse not in gestes:
        raise HTTPException(status_code=404, detail="Réponse inconnue")

    participant = await _participant_du_visiteur(request, session, user)
    if participant is not None:
        await gestes[reponse](session, participant, reformulation_id)
    return RedirectResponse(chemin_local(suivant), status_code=303)


@router.post("/rappels/reformulation/{statement_id}/fermer")
async def fermer_rappel_reformulation(
    statement_id: int,
    request: Request,
    suivant: str = Form("/"),
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(current_user_optional),
) -> RedirectResponse:
    """Écarte le rappel né d'un signalement rattrapable (MOD-15).

    **Une adresse distincte de celle du retrait, et non un paramètre.** Les deux formes
    n'écartent pas la même chose : celle-ci écrit sur les signalements, celle-là sur la
    proposition. Une seule route aurait dû deviner laquelle des deux on ferme — et se
    tromper le jour où une proposition porte les deux à la fois, ce qui est précisément
    le cas qu'on veut éviter de mal traiter : éteindre par mégarde l'exposé des motifs
    d'un retrait, qui est dû.

    Un refus redirige comme un succès, pour la même raison qu'au MOD-6 : dire « ce n'est
    pas à vous » confirmerait l'existence de la proposition.
    """
    participant = await _participant_du_visiteur(request, session, user)
    if participant is not None:
        await rappels_service.fermer_reformulation(session, participant, statement_id)
    return RedirectResponse(chemin_local(suivant), status_code=303)
