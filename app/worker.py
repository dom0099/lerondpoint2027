"""Worker d'analyse — processus séparé de l'API.

    python -m app.worker                      boucle, un passage toutes les 10 minutes
    python -m app.worker --once               un seul passage, puis sortie
    python -m app.worker --conversation SLUG  recalcule une conversation, à la demande

Pourquoi un processus à part : red-dwarf s'appuie sur pandas et scikit-learn, qui
bloquent le CPU. Les laisser tourner dans le processus API figerait les requêtes des
participants pendant tout le calcul. Ni file d'attente ni Celery : une boucle
suffit à ce volume, et n'ajoute aucune infrastructure à opérer.
"""

import argparse
import asyncio
import logging

from app.analysis import pipeline
from app.db import get_sessionmaker
from app.services import conversations as conversations_service
from app.services import nommage_llm, recalcul
from app.services import rate_limit
from app.services import liens_verification
from app.services import reformulation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("app.worker")


async def analyse_one(slug: str) -> int:
    async with get_sessionmaker()() as session:
        conversation = await conversations_service.by_slug(session, slug)
        if conversation is None:
            logger.error("conversation inconnue : %s", slug)
            return 1
        run = await pipeline.analyse(session, conversation)
        logger.info("run %s -> %s", run.id, run.status.value)
        if run.error_text:
            logger.info("  %s", run.error_text)
    return 0


async def purge_rate_limit_traces() -> int:
    """Efface les traces de plafond sorties de la plus longue fenêtre.

    Confiée au worker parce qu'elle doit avoir lieu **même sans trafic** : ces lignes
    contiennent des données personnelles (adresse e-mail pour la réinitialisation,
    adresse IP pour les propositions et les connexions), et une durée de conservation
    qui dépendrait du passage d'un visiteur n'en serait pas une.
    """
    async with get_sessionmaker()() as session:
        supprimees = await rate_limit.purge_expired(session)
    if supprimees:
        logger.info("%d trace(s) de plafond expirée(s) supprimée(s)", supprimees)
    return supprimees


async def purge_reformulations_expirees() -> int:
    """Efface les reformulations que leur auteur n'a pas validées en quinze jours (MOD-16).

    **Confiée au worker pour la même raison que la purge des traces de plafond : elle
    doit avoir lieu même sans trafic.** Une échéance qui ne tomberait qu'au passage d'un
    visiteur n'en serait pas une — et ici l'échéance protège précisément le cas où
    personne ne revient, qui est celui de la proposition n° 142.

    Le sort de la proposition ne bouge pas : c'est la proposition de réécriture qui
    disparaît, pas le texte. Rien n'est donc écrit au journal d'audit.
    """
    async with get_sessionmaker()() as session:
        effacees = await reformulation.purger_expirees(session)
    if effacees:
        logger.info(
            "%d reformulation(s) sans réponse depuis %d jours supprimée(s)",
            effacees,
            reformulation.DELAI_VALIDATION.days,
        )
    return effacees


async def purge_calculs_perimes() -> int:
    """Rend les grosses tables des calculs dépassés (chantier G9).

    `participant_projection` gagne une ligne par personne et par calcul : six fois plus
    de lignes par heure qu'au rythme horaire depuis le passage à 10 minutes (quatre
    fois au quart d'heure). La borne est la condition de la cadence, pas un entretien
    de confort. Le détail de ce qui est gardé est dans `pipeline`.

    À noter, parce que c'est ce qui rend le changement de cadence sans danger ici : la
    rétention est une DURÉE (48 h) et non un nombre de calculs. Le régime permanent de
    la table monte donc d'un facteur 1,5, mais il reste borné — rien à corriger, un
    volume à surveiller.
    """
    async with get_sessionmaker()() as session:
        otees = await pipeline.purge_calculs_perimes(session)
    if otees:
        logger.info("%d ligne(s) de calcul dépassé supprimée(s)", otees)
    return otees


async def verifier_les_liens() -> dict[str, int]:
    """Interroge quelques adresses publiées, pour savoir si elles répondent encore.

    **La seule chose que ce projet fasse sortir vers l'extérieur.** Elle est ici, dans
    le worker, et nulle part ailleurs : jamais à la saisie, jamais sur une route
    publique. Un champ d'URL public qui déclenche une requête depuis le serveur est le
    cas d'école de la SSRF, et le chantier J l'avait refusé pour cette raison.

    Le client est construit ici, et ses réglages font partie des garde-fous :

      - `follow_redirects=False` — une redirection n'est pas suivie. Un domaine autorisé
        qui renverrait vers une adresse interne ne mènerait donc nulle part ;
      - un délai court, et `HEAD` seul : aucun corps n'est téléchargé, donc rien de ce
        que rend un tiers n'entre dans le processus ;
      - un agent qui nomme le site et dit où écrire.

    Un échec du passage n'interrompt pas le worker : la vérification des liens est un
    confort, l'analyse des groupes est le métier.
    """
    import httpx

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=liens_verification.DELAI_REQUETE,
            headers={"User-Agent": liens_verification.AGENT},
        ) as client:
            async with get_sessionmaker()() as session:
                comptes = await liens_verification.verifier_les_liens(session, client)
    except Exception:  # noqa: BLE001 — voir la note ci-dessus
        logger.exception("la vérification des liens a échoué ; le passage continue")
        return {}

    if comptes:
        logger.info(
            "liens vérifiés : %s",
            ", ".join(f"{n} {verdict}" for verdict, n in sorted(comptes.items())),
        )
    return comptes


async def produis_les_noms() -> int:
    """Vide un peu de la file de nommage (chantier E4).

    **En dehors du passage d'analyse, et volontairement.** Décider qu'un groupe doit
    être nommé est instantané et fiable ; produire le nom est lent et dépend d'un
    service extérieur. Les appeler au même endroit ferait dépendre le calcul des groupes
    de la disponibilité d'un modèle de langue — exactement ce que le E3 a écarté en
    choisissant une file.

    Rien à faire tant que `naming_enabled` est faux, ce qui est le défaut : la file
    s'empile et le site se comporte comme avant.
    """
    try:
        async with get_sessionmaker()() as session:
            return await nommage_llm.produis(session)
    except Exception:  # noqa: BLE001 — un nom manquant ne casse pas un passage
        logger.exception("le nommage a échoué ; le passage continue")
        return 0


async def run_once() -> int:
    """Un passage sur toutes les conversations dont les votes ont bougé."""
    await purge_rate_limit_traces()
    await purge_reformulations_expirees()
    await purge_calculs_perimes()
    await verifier_les_liens()
    await produis_les_noms()
    async with get_sessionmaker()() as session:
        stale = await pipeline.conversations_needing_analysis(session)
        if not stale:
            logger.info("rien à recalculer")
            return 0
        logger.info("%d conversation(s) à recalculer", len(stale))
        for conversation in stale:
            run = await pipeline.analyse(session, conversation)
            logger.info(
                "%s -> run %s (%s)", conversation.slug, run.id, run.status.value
            )
    return 0


async def loop() -> int:
    """Un passage à chaque instant de la grille, et non un passage toutes les N secondes.

    La différence tient en une phrase : dormir N secondes APRÈS un passage fait dériver
    la cadence de la durée du passage. Mesuré sur la production avant le G9, les calculs
    horaires tombaient à 20:59:23, 21:59:23, 22:59:24, 23:59:24 — un quart de seconde de
    retard par tour, sans raison de se stabiliser. Cela n'avait aucune importance tant
    qu'on annonçait « toutes les heures » ; le message de fin de parcours, lui, annonce
    le temps RESTANT, et une dérive le rendrait faux. Voir app/services/recalcul.py.

    Un passage plus long que la période ne s'accumule pas en retard : la grille repart
    du présent, un tour est simplement sauté.
    """
    logger.info(
        "worker démarré — un passage %s, calé sur l'horloge",
        recalcul.periode_de_recalcul(),
    )
    while True:
        try:
            await run_once()
        except Exception:  # noqa: BLE001 — une erreur ne doit pas tuer la boucle
            logger.exception("passage en échec, on réessaiera au prochain tour")
        attente = recalcul.delai_avant_calcul().total_seconds()
        logger.info(
            "prochain passage à %s (dans %d s)",
            recalcul.prochain_calcul().isoformat(timespec="seconds"),
            round(attente),
        )
        await asyncio.sleep(attente)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.worker")
    parser.add_argument("--once", action="store_true", help="un seul passage")
    parser.add_argument("--conversation", help="recalcule ce slug et sort")
    args = parser.parse_args(argv)

    if args.conversation:
        return asyncio.run(analyse_one(args.conversation))
    if args.once:
        return asyncio.run(run_once())
    return asyncio.run(loop())


if __name__ == "__main__":
    raise SystemExit(main())
