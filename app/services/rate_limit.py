"""Plafonnement des actions coûteuses ou abusables."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rate_limit import RateLimitHit

#: Réinitialisation de mot de passe. Chaque appel remet un message au relais Brevo :
#: sans plafond, l'endpoint permet d'épuiser le quota d'envoi et d'abîmer la
#: réputation du domaine expéditeur.
RESET_PER_EMAIL = 3
RESET_PER_IP = 10
RESET_WINDOW = timedelta(hours=1)

#: Propositions (conversations et propositions). Rien ne bornait le nombre de
#: propositions : une seule personne pouvait remplir la file de modération et, en
#: post-modération, la conversation elle-même. Les chiffres ci-dessous sont des
#: réglages, pas des invariants : ils sont volontairement larges pour un usage
#: honnête — trois conversations par jour, c'est déjà beaucoup à relire — et seront
#: réajustés une fois l'usage réel observé.
PROPOSAL_WINDOW = timedelta(hours=24)
#: Par participant.
CONVERSATIONS_PER_PARTICIPANT = 3
#: Par participant ET par conversation : dix propositions dans une même conversation
#: dans la journée, c'est le maximum d'un contributeur très actif.
STATEMENTS_PER_PARTICIPANT = 10
#: Filet par IP : un anonyme retrouve un participant neuf en vidant ses cookies, le
#: plafond par participant ne tient donc pas seul. Assez haut pour ne pas gêner un
#: foyer, un bureau ou un réseau mobile partagé.
CONVERSATIONS_PER_IP = 10
STATEMENTS_PER_IP = 40

#: Signalements (chantier Modération, MOD-3a). Dix par heure et par identité.
#:
#: Le plafond est là pour empêcher qu'une seule personne balaye un débat entier en
#: signalant tout ce qu'elle y lit — pas pour la décourager de signaler. Dix par heure
#: laisse largement passer quelqu'un qui parcourt un débat choquant de bout en bout ;
#: au-delà, ce n'est plus de la lecture.
#:
#: Par IDENTITÉ et non par IP, contrairement aux propositions : le signalement est déjà
#: dédoublonné par proposition (une identité ne peut signaler qu'une fois), donc atteindre
#: le plafond demande de viser dix propositions distinctes. Un second plafond par IP
#: frapperait d'abord un foyer ou un bureau où deux personnes lisent le même débat.
SIGNALEMENTS_PER_PARTICIPANT = 10
SIGNALEMENT_WINDOW = timedelta(hours=1)

#: Second plafond, sur l'ADRESSE — ajouté au MOD-3b, et le raisonnement du MOD-3a qui
#: s'en passait était faux. Il tenait que le plafond par identité suffisait puisque
#: signaler dix fois demande dix propositions distinctes. Il oubliait ce que les chiffres
#: du §0 bis disaient : **164 participants sur 167 n'ont pas de compte**. L'identité qui
#: porte l'unicité d'un signalement est alors un jeton de session, qui se jette et se
#: reprend en une seconde — le plafond par identité ne coûte donc qu'un vidage de
#: cookies.
#:
#: 30 par heure, soit trois identités pleines : assez pour un foyer, un bureau ou un
#: réseau mobile partagé où plusieurs personnes lisent le même débat, trop peu pour
#: balayer un site en renouvelant son jeton.
#:
#: **Le seau porte le CONDENSÉ de l'adresse**, jamais l'adresse — celui-là même que
#: `signalement.condenser` écrit déjà en base. Les autres seaux du fichier (`reset-ip:`,
#: `conv-ip:`) portent l'adresse en clair ; c'est une dette de leur époque, pas un
#: modèle à suivre, et elle est bornée par `purge_expired`.
SIGNALEMENTS_PER_ADRESSE = 30

#: Tentatives de connexion. `/admin` et `/moderation` restent joignables publiquement,
#: derrière le seul mot de passe : sans plafond, rien n'empêche de l'essayer en boucle.
#:
#: Deux fenêtres, **par adresse IP uniquement**. Le plafond porte sur l'IP et non sur le
#: compte visé, délibérément : un plafond par compte permettrait à n'importe qui
#: d'enfermer dehors le seul modérateur du site en brûlant son quota avec de mauvais
#: mots de passe. Contre le remplissage d'identifiants volés, c'est l'IP qui compte.
#:
#: 5 par minute laisse passer une personne qui se trompe et recommence ; 30 par heure
#: ferme la porte à une tentative soutenue. Rappel : chaque essai coûte déjà un hachage
#: argon2id (m=65536, t=3, p=4), donc ~0,1 s de CPU à l'attaquant.
LOGIN_PER_IP_BURST = 5
LOGIN_BURST_WINDOW = timedelta(minutes=1)
LOGIN_PER_IP_HOUR = 30
LOGIN_HOUR_WINDOW = timedelta(hours=1)

#: Au-delà, une trace de plafond ne sert plus à rien : c'est la plus longue fenêtre.
#: Sert au balayage périodique (voir `purge_expired`).
LONGEST_WINDOW = PROPOSAL_WINDOW


async def record(session: AsyncSession, bucket: str) -> None:
    """Enregistre une tentative dans ce seau."""
    session.add(RateLimitHit(bucket=bucket))
    await session.commit()


async def used_since(session: AsyncSession, bucket: str, window: timedelta) -> int:
    """Nombre de tentatives de ce seau dans la fenêtre glissante."""
    since = datetime.now(timezone.utc) - window
    return await session.scalar(
        select(func.count(RateLimitHit.id)).where(
            RateLimitHit.bucket == bucket, RateLimitHit.created_at >= since
        )
    )


async def purge_expired(
    session: AsyncSession, older_than: timedelta | None = None
) -> int:
    """Supprime TOUTES les traces sorties de la plus longue fenêtre.

    Le nettoyage paresseux de `record_and_exceeds` ne balayait que le seau qu'il venait
    de toucher : une ligne `reset:<adresse e-mail>` d'un seau jamais re-sollicité
    restait en base indéfiniment, alors que la fenêtre annoncée est d'une heure. Or ces
    lignes contiennent des données personnelles — adresse e-mail en clair pour la
    réinitialisation, adresse IP pour les propositions et les connexions. Une durée de
    conservation qui n'est bornée que par le hasard d'un nouvel accès n'est pas une
    durée de conservation.

    Appelé à chaque passage du worker (toutes les heures), donc indépendamment de tout
    trafic. Renvoie le nombre de lignes supprimées.
    """
    window = LONGEST_WINDOW if older_than is None else older_than
    cutoff = datetime.now(timezone.utc) - window
    result = await session.execute(
        delete(RateLimitHit).where(RateLimitHit.created_at < cutoff)
    )
    await session.commit()
    return result.rowcount or 0


async def record_and_exceeds(
    session: AsyncSession, bucket: str, limit: int, window: timedelta
) -> bool:
    """Enregistre la tentative et dit si le plafond est franchi.

    L'enregistrement a lieu **avant** le test : une rafale compte donc entièrement,
    y compris la requête qui déclenche le refus.
    """
    await record(session, bucket)
    used = await used_since(session, bucket, window)

    # Purge opportuniste de ce seau. Elle ne suffit pas — `purge_expired` passe
    # derrière, sur toute la table — mais elle garde les seaux actifs compacts.
    since = datetime.now(timezone.utc) - window
    await session.execute(
        delete(RateLimitHit).where(
            RateLimitHit.bucket == bucket, RateLimitHit.created_at < since
        )
    )
    await session.commit()
    return used > limit


async def password_reset_allowed(
    session: AsyncSession, email: str, client_ip: str | None
) -> bool:
    """Deux plafonds : par adresse visée, et par origine de la demande.

    Le plafond par adresse protège la personne visée du harcèlement ; celui par IP
    protège le quota d'envoi contre un émetteur qui balaierait des adresses.
    """
    blocked = await record_and_exceeds(
        session, f"reset:{email.lower()}", RESET_PER_EMAIL, RESET_WINDOW
    )
    if client_ip:
        blocked = (
            await record_and_exceeds(
                session, f"reset-ip:{client_ip}", RESET_PER_IP, RESET_WINDOW
            )
            or blocked
        )
    return not blocked


async def conversation_proposal_allowed(
    session: AsyncSession, participant_id: int, client_ip: str | None
) -> bool:
    """Plafond des conversations proposées : par participant, et par origine."""
    blocked = await record_and_exceeds(
        session,
        f"conv:{participant_id}",
        CONVERSATIONS_PER_PARTICIPANT,
        PROPOSAL_WINDOW,
    )
    if client_ip:
        blocked = (
            await record_and_exceeds(
                session, f"conv-ip:{client_ip}", CONVERSATIONS_PER_IP, PROPOSAL_WINDOW
            )
            or blocked
        )
    return not blocked


async def statement_proposal_allowed(
    session: AsyncSession,
    participant_id: int,
    conversation_id: int,
    client_ip: str | None,
) -> bool:
    """Plafond des propositions déposées, compté PAR conversation.

    Le seau inclut la conversation : quelqu'un qui participe activement à trois
    consultations n'est pas quelqu'un qui inonde la file de l'une d'elles.
    """
    blocked = await record_and_exceeds(
        session,
        f"stmt:{participant_id}:{conversation_id}",
        STATEMENTS_PER_PARTICIPANT,
        PROPOSAL_WINDOW,
    )
    if client_ip:
        blocked = (
            await record_and_exceeds(
                session, f"stmt-ip:{client_ip}", STATEMENTS_PER_IP, PROPOSAL_WINDOW
            )
            or blocked
        )
    return not blocked


async def signalement_allowed(
    session: AsyncSession, participant_id: int, adresse_condensee: str | None = None
) -> bool:
    """Deux plafonds horaires distincts : par identité, et par adresse condensée.

    Les deux sont enregistrés **avant** d'être évalués, et les deux le sont toujours,
    même si le premier a déjà tranché : un seau qu'on cesserait d'alimenter dès qu'un
    autre a dit non laisserait passer la rafale suivante, la fenêtre glissante ayant
    oublié ce qu'on ne lui a pas donné.
    """
    bloque = await record_and_exceeds(
        session,
        f"signalement:{participant_id}",
        SIGNALEMENTS_PER_PARTICIPANT,
        SIGNALEMENT_WINDOW,
    )
    if adresse_condensee:
        bloque = (
            await record_and_exceeds(
                session,
                f"signalement-adresse:{adresse_condensee}",
                SIGNALEMENTS_PER_ADRESSE,
                SIGNALEMENT_WINDOW,
            )
            or bloque
        )
    return not bloque


async def login_allowed(session: AsyncSession, client_ip: str | None) -> bool:
    """Plafond des tentatives de connexion, par adresse IP.

    Une seule trace est écrite par tentative, évaluée sur DEUX fenêtres : la rafale
    (une personne qui se trompe) et l'heure (une tentative soutenue). Toutes les
    tentatives comptent, réussies comprises : la dépendance qui protège
    `/auth/login` s'exécute avant la route, donc avant de connaître l'issue — et
    5 connexions réussies par minute depuis une même adresse resteraient de toute
    façon inhabituelles.
    """
    if not client_ip:
        # Sans IP identifiable (appel interne, test), on ne plafonne pas : le refus
        # porterait sur tout le monde à la fois.
        return True

    bucket = f"login-ip:{client_ip}"
    await record(session, bucket)
    if await used_since(session, bucket, LOGIN_BURST_WINDOW) > LOGIN_PER_IP_BURST:
        return False
    return await used_since(session, bucket, LOGIN_HOUR_WINDOW) <= LOGIN_PER_IP_HOUR
