"""La voie de recours : `/contester/<jeton>` (chantier Modération, MOD-3b).

Elle existe parce que dès qu'un retrait conservatoire devient visible par son auteur,
l'exposé des motifs et le recours sont dus — et le MOD-3a, fusionné, rend ce retrait réel
au prochain déploiement.

**Le jeton est le seul droit d'accès**, et ce n'est pas un raccourci : 164 des 167
participants du site n'ont pas de compte, il n'existe donc aucune identité sur laquelle
adosser une autorisation. Ce fichier éprouve surtout cela — qu'un jeton vaille exactement
ce qu'il doit valoir, ni plus ni moins.
"""

from sqlalchemy import func, select

from app.models import (
    ActeModeration,
    Contestation,
    JournalModeration,
    ModerationStatus,
    Statement,
)
from app.services import signalement as regles
from app.services import signalement_file
from tests.conftest import open_conversation


async def _proposition_retiree(client, moderator_client, session_factory):
    """Un débat, une proposition retirée par un signalement de ligne rouge, son jeton."""
    conversation_id, slug, ids = await open_conversation(
        moderator_client, statements=4, title="Débat à contester"
    )
    reponse = await client.post(
        "/api/signalements",
        json={"statement_id": ids[0], "motifs": ["incitation_violence"]},
    )
    assert reponse.status_code == 201
    async with session_factory() as session:
        statement = await session.get(Statement, ids[0])
        assert statement.moderation_status is ModerationStatus.retire
        return conversation_id, slug, ids[0], statement.jeton_contestation


# --- le jeton -----------------------------------------------------------------------


async def test_le_retrait_pose_un_jeton(client, moderator_client, session_factory):
    """Posé au moment du retrait, pas à la première demande : c'est le retrait qui ouvre
    le droit, et un jeton créé plus tard serait un droit qui dépend de ce que quelqu'un a
    pensé à faire."""
    _, _, _, jeton = await _proposition_retiree(client, moderator_client, session_factory)

    assert jeton is not None
    # 32 octets en base64 URL : 43 signes. Un identifiant court se devine, pas celui-ci.
    assert len(jeton) >= 40


async def test_deux_jetons_ne_se_ressemblent_pas(
    client_factory, moderator_client, session_factory
):
    """Imprévisible : deux retraits ne doivent pas donner deux jetons voisins, sans quoi
    en connaître un donnerait des idées sur l'autre."""
    jetons = set()
    for rang in range(2):
        conversation_id, _, ids = await open_conversation(
            moderator_client, statements=3, title=f"Débat imprévisible {rang}"
        )
        async with client_factory() as visiteur:
            await visiteur.post(
                "/api/signalements",
                json={"statement_id": ids[0], "motifs": ["propos_haineux"]},
            )
        async with session_factory() as session:
            jetons.add((await session.get(Statement, ids[0])).jeton_contestation)

    assert len(jetons) == 2
    premier, second = (j for j in jetons)
    # Aucun préfixe commun : un compteur ou un horodatage en produirait un.
    assert premier[:8] != second[:8]


async def test_une_proposition_non_retiree_n_a_pas_de_jeton(
    client, moderator_client, session_factory
):
    """Il n'y a rien à contester : fabriquer un jeton en créerait un que personne n'a le
    droit d'utiliser."""
    _, _, ids = await open_conversation(
        moderator_client, statements=3, title="Débat sans retrait"
    )
    await client.post(
        "/api/signalements", json={"statement_id": ids[0], "motifs": ["doublon"]}
    )

    async with session_factory() as session:
        statement = await session.get(Statement, ids[0])
    assert statement.jeton_contestation is None


# --- l'accès à la page ---------------------------------------------------------------


async def test_un_jeton_inconnu_donne_404(client):
    """404 et jamais 403 : un 403 dirait « ce jeton existe mais pas pour vous »,
    c'est-à-dire confirmerait à qui essaie des jetons qu'il a touché juste."""
    for essai in ("", "inconnu", "a" * 43, "../../etc/passwd"):
        reponse = await client.get(f"/contester/{essai}")
        assert reponse.status_code == 404, essai


async def test_le_jeton_ouvre_l_expose_des_motifs(
    client, moderator_client, session_factory
):
    """Le motif retenu, la date, et le texte concerné. En toutes lettres : « propos
    haineux » et non « propos_haineux » — un code n'est pas un exposé des motifs."""
    from markupsafe import escape

    _, _, statement_id, jeton = await _proposition_retiree(
        client, moderator_client, session_factory
    )
    async with session_factory() as session:
        texte = (await session.get(Statement, statement_id)).text

    page = await client.get(f"/contester/{jeton}")

    assert page.status_code == 200
    # Échappé par Jinja : comparer au texte brut testerait l'ABSENCE d'échappement.
    assert str(escape(texte)) in page.text
    assert regles.libelle("incitation_violence") in page.text
    assert "incitation_violence" not in page.text
    assert "Retirée le" in page.text


async def test_la_page_ne_montre_aucun_signaleur_ni_aucun_decompte(
    client_factory, moderator_client, session_factory
):
    """Ce sont des tiers, et le §4 du cadrage interdit de faire du signalement un jeu où
    l'on compte les points."""
    async with client_factory() as premier:
        _, _, statement_id, jeton = await _proposition_retiree(
            premier, moderator_client, session_factory
        )

    async with client_factory() as auteur:
        page = await auteur.get(f"/contester/{jeton}")

    assert "signaleur" not in page.text.lower()
    assert "signalement" not in page.text.lower()


async def test_la_page_ne_promet_aucun_delai(client, moderator_client, session_factory):
    """On n'écrit pas « sous 48 heures » tant que personne ne garantit 48 heures."""
    _, _, _, jeton = await _proposition_retiree(client, moderator_client, session_factory)

    import re

    page = await client.get(f"/contester/{jeton}")

    for promesse in ("48 h", "48 heures", "sous 24", "délai de"):
        assert promesse not in page.text
    # Espaces normalisés : le gabarit replie ses phrases sur plusieurs lignes, et une
    # assertion qui dépendrait de l'endroit du repli casserait à la première retouche.
    aplati = re.sub(r"\s+", " ", page.text)
    assert "Aucun délai n'est garanti" in aplati


async def test_la_page_ne_demande_aucune_identification(
    client, moderator_client, session_factory
):
    """Aucun compte à créer, aucune connexion à faire : l'auteur est anonyme dans 98 %
    des cas, et lui demander de s'identifier fermerait le recours à presque tout le monde.

    Les liens de connexion de la barre de navigation du site sont là, évidemment — c'est
    le gabarit public. Ce qui est vérifié est que la PAGE, elle, n'en demande rien : son
    formulaire va sur `/contester`, il n'y a pas de champ de mot de passe, et elle
    s'ouvre sans la moindre session.
    """
    _, _, _, jeton = await _proposition_retiree(client, moderator_client, session_factory)

    page = await client.get(f"/contester/{jeton}")

    assert page.status_code == 200
    assert f'action="/contester/{jeton}"' in page.text
    assert 'type="password"' not in page.text
    assert "Connectez-vous" not in page.text


# --- le dépôt -------------------------------------------------------------------------


async def test_une_contestation_est_enregistree_et_journalisee(
    client, moderator_client, session_factory
):
    _, _, statement_id, jeton = await _proposition_retiree(
        client, moderator_client, session_factory
    )

    reponse = await client.post(
        f"/contester/{jeton}", data={"texte": "Ce n'était pas un appel à la violence."}
    )

    assert reponse.status_code == 200
    assert "Votre demande est enregistrée" in reponse.text
    async with session_factory() as session:
        ligne = await session.scalar(select(Contestation))
        assert ligne is not None
        assert ligne.statement_id == statement_id
        assert ligne.texte == "Ce n'était pas un appel à la violence."
        actes = [j.acte for j in await session.scalars(select(JournalModeration))]
    assert ActeModeration.contestation_deposee in actes


async def test_le_journal_ne_porte_ni_le_jeton_ni_l_identite(
    client, moderator_client, session_factory
):
    """Le journal est en ajout seul : ce qu'on y écrit ne se reprend jamais. On n'y met
    donc ni la clé du recours, ni un identifiant de navigateur."""
    _, _, _, jeton = await _proposition_retiree(client, moderator_client, session_factory)
    await client.post(f"/contester/{jeton}", data={"texte": "Je conteste."})

    async with session_factory() as session:
        lignes = list(await session.scalars(select(JournalModeration)))

    for ligne in lignes:
        assert jeton not in (ligne.auteur or "")
        assert jeton not in (ligne.motif or "")
    contestation = [
        l for l in lignes if l.acte is ActeModeration.contestation_deposee
    ][0]
    assert contestation.auteur == "auteur de la proposition"


async def test_un_texte_vide_est_refuse(client, moderator_client, session_factory):
    _, _, _, jeton = await _proposition_retiree(client, moderator_client, session_factory)

    reponse = await client.post(f"/contester/{jeton}", data={"texte": "   "})

    assert reponse.status_code == 200
    assert "Votre demande est enregistrée" not in reponse.text
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Contestation.id))) == 0


async def test_un_texte_trop_long_est_refuse(client, moderator_client, session_factory):
    """Refus, pas de troncature : couper le texte de quelqu'un qui se défend serait le
    faire parler à moitié."""
    _, _, _, jeton = await _proposition_retiree(client, moderator_client, session_factory)
    maximum = signalement_file.CONTESTATION_MAX

    trop = await client.post(f"/contester/{jeton}", data={"texte": "x" * (maximum + 1)})
    juste = await client.post(f"/contester/{jeton}", data={"texte": "y" * maximum})

    assert "Votre demande est enregistrée" not in trop.text
    assert "Votre demande est enregistrée" in juste.text
    async with session_factory() as session:
        lignes = list(await session.scalars(select(Contestation)))
    assert len(lignes) == 1
    assert len(lignes[0].texte) == maximum


async def test_contester_sur_un_jeton_inconnu_n_ecrit_rien(client, session_factory):
    reponse = await client.post("/contester/jeton-invente", data={"texte": "Bonjour."})

    assert reponse.status_code == 404
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Contestation.id))) == 0


# --- le lien entre le recours et la file du responsable --------------------------------


async def test_la_contestation_remonte_en_tete_de_la_file(
    client, moderator_client, session_factory
):
    """Avant les signalements : une personne qui conteste attend une réponse, un
    signalement n'attend personne."""
    _, _, _, jeton = await _proposition_retiree(client, moderator_client, session_factory)
    await client.post(
        f"/contester/{jeton}", data={"texte": "Je demande un réexamen de ce retrait."}
    )

    page = await moderator_client.get("/moderation/signalements")

    assert page.status_code == 200
    # Le titre porte « à lire » depuis le MOD-13 : la liste de tête est devenue une file
    # de lecture, que le bouton « J'ai lu » fait diminuer.
    assert "Demandes de réexamen à lire (1)" in page.text
    assert "Je demande un réexamen de ce retrait." in page.text
    # En tête : avant le titre de la section des signalements.
    assert page.text.index("Demandes de réexamen") < page.text.index(
        "pas un verdict"
    )


async def test_l_ecran_du_responsable_donne_l_adresse_a_transmettre(
    client, moderator_client, session_factory
):
    """Aucun canal ne porte encore le jeton jusqu'à l'auteur — l'envoi de courriel est
    hors périmètre. C'est donc le responsable qui le transmet, et il faut qu'il l'ait
    sous les yeux."""
    _, _, _, jeton = await _proposition_retiree(client, moderator_client, session_factory)

    page = await moderator_client.get("/moderation/signalements")

    assert jeton in page.text
    assert "Adresse de recours à transmettre" in page.text


async def test_annuler_le_retrait_ferme_la_voie_de_recours(
    client, moderator_client, session_factory
):
    """La proposition est revenue en circulation : il n'y a plus rien à contester, et une
    adresse encore ouvrable inviterait l'auteur à défendre ce qu'on vient de lui rendre.

    Les contestations déjà déposées, elles, restent — ce sont elles qui ont pu motiver
    l'annulation.
    """
    _, _, statement_id, jeton = await _proposition_retiree(
        client, moderator_client, session_factory
    )
    await client.post(f"/contester/{jeton}", data={"texte": "Je conteste ce retrait."})

    await moderator_client.post(
        f"/moderation/signalements/{statement_id}/annuler", follow_redirects=False
    )

    assert (await client.get(f"/contester/{jeton}")).status_code == 404
    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.jeton_contestation is None
        assert statement.moderation_status is ModerationStatus.approved
        assert await session.scalar(select(func.count(Contestation.id))) == 1


async def test_le_message_a_l_auteur_porte_le_jeton_quand_il_existe(
    client, moderator_client, session_factory
):
    """C'est l'adresse exacte que l'auteur ouvrirait — pas l'adresse générique, qui ne
    mène nulle part."""
    _, _, statement_id, jeton = await _proposition_retiree(
        client, moderator_client, session_factory
    )

    async with session_factory() as session:
        lignes = await signalement_file.file(session)

    ligne = [l for l in lignes if l.statement.id == statement_id][0]
    assert jeton in ligne.message


# --- l'écran du responsable : lire avant de trancher (MOD-13) -------------------------


async def _contestation_deposee(client, moderator_client, session_factory, texte):
    """Une proposition retirée, une demande de réexamen déposée dessus."""
    _, _, statement_id, jeton = await _proposition_retiree(
        client, moderator_client, session_factory
    )
    reponse = await client.post(f"/contester/{jeton}", data={"texte": texte})
    assert reponse.status_code == 200
    async with session_factory() as session:
        contestation = await session.scalar(
            select(Contestation).where(Contestation.statement_id == statement_id)
        )
        return statement_id, jeton, contestation.id


async def test_la_demande_s_affiche_sur_la_carte_ou_se_prennent_les_gestes(
    client, moderator_client, session_factory
):
    """**Le test qui porte le lot.**

    Depuis le MOD-3b la demande était en tête de page, c'est-à-dire à un endroit qu'il
    fallait avoir pensé à lire avant de descendre cliquer. Un recours qu'on peut ne pas
    voir au moment de trancher ne remplit pas son office : le texte de l'auteur doit
    figurer sur la carte, au-dessus des boutons qui décident de son sort.
    """
    statement_id, _, _ = await _contestation_deposee(
        client, moderator_client, session_factory, "Ma proposition citait un rapport."
    )

    page = await moderator_client.get("/moderation/signalements")

    assert "L'auteur a demandé un réexamen" in page.text
    # Sur la carte : entre le titre de la section des signalements et le bouton qui
    # confirme le retrait de CETTE proposition.
    debut_cartes = page.text.index("pas un verdict")
    bouton = page.text.index(f"/moderation/signalements/{statement_id}/confirmer")
    sur_la_carte = page.text.index("L'auteur a demandé un réexamen", debut_cartes)
    assert debut_cartes < sur_la_carte < bouton


async def test_marquer_lue_sort_la_demande_de_la_liste_a_lire_sans_l_effacer(
    client, moderator_client, session_factory
):
    """La liste de tête est une file de lecture : ce qui est lu en sort, sinon le
    responsable relit tout à chaque passage. Mais la demande reste sur la carte du
    dossier — elle n'est pas une tâche cochée, c'est la défense de l'auteur."""
    _, _, contestation_id = await _contestation_deposee(
        client, moderator_client, session_factory, "Je demande un réexamen motivé."
    )

    reponse = await moderator_client.post(
        f"/moderation/contestations/{contestation_id}/lue", follow_redirects=False
    )
    assert reponse.status_code == 303

    page = await moderator_client.get("/moderation/signalements")
    assert "Demandes de réexamen à lire" not in page.text
    # Toujours lisible là où se prend la décision, avec la date de lecture.
    assert "Je demande un réexamen motivé." in page.text
    assert "L'auteur a demandé un réexamen" in page.text
    async with session_factory() as session:
        contestation = await session.get(Contestation, contestation_id)
        assert contestation.traite_le is not None


async def test_une_demande_deja_lue_n_est_pas_re_horodatee(
    client, moderator_client, session_factory
):
    """La date qui vaut quelque chose est celle du PREMIER regard — c'est elle qui dira
    un jour combien de temps un auteur a attendu d'être lu. La réécrire à chaque passage
    en ferait la date du dernier clic, qui ne renseigne sur rien."""
    _, _, contestation_id = await _contestation_deposee(
        client, moderator_client, session_factory, "Relire, s'il vous plaît."
    )

    await moderator_client.post(f"/moderation/contestations/{contestation_id}/lue")
    async with session_factory() as session:
        premiere = (await session.get(Contestation, contestation_id)).traite_le

    await moderator_client.post(f"/moderation/contestations/{contestation_id}/lue")
    async with session_factory() as session:
        seconde = (await session.get(Contestation, contestation_id)).traite_le

    assert premiere == seconde


async def test_marquer_lue_n_ecrit_rien_au_journal_d_audit(
    client, moderator_client, session_factory
):
    """Le journal enregistre des ACTES de modération, qui changent le sort d'une
    proposition. Lire n'en est pas un : l'y verser mêlerait une trace d'attention à des
    décisions opposables, dans une table qu'on ne peut plus jamais nettoyer."""
    _, _, contestation_id = await _contestation_deposee(
        client, moderator_client, session_factory, "Un texte à lire."
    )
    async with session_factory() as session:
        avant = await session.scalar(select(func.count(JournalModeration.id)))

    await moderator_client.post(f"/moderation/contestations/{contestation_id}/lue")

    async with session_factory() as session:
        assert await session.scalar(select(func.count(JournalModeration.id))) == avant


async def test_marquer_lue_une_demande_inconnue_donne_404(moderator_client):
    reponse = await moderator_client.post("/moderation/contestations/999999/lue")

    assert reponse.status_code == 404


async def test_marquer_lue_est_reserve_au_responsable(
    client, moderator_client, session_factory
):
    """Un visiteur ne doit pas pouvoir faire disparaître une demande de la file de
    lecture — ce serait la faire taire sans que personne l'ait lue."""
    _, _, contestation_id = await _contestation_deposee(
        client, moderator_client, session_factory, "À ne pas escamoter."
    )

    reponse = await client.post(
        f"/moderation/contestations/{contestation_id}/lue", follow_redirects=False
    )

    assert reponse.status_code == 303
    assert "/moderation/login" in reponse.headers["location"]
    async with session_factory() as session:
        assert (await session.get(Contestation, contestation_id)).traite_le is None


async def test_deux_demandes_sur_la_meme_proposition_font_un_seul_dossier(
    client, moderator_client, session_factory
):
    """Le schéma autorise délibérément plusieurs dépôts — un auteur qui se ravise ou
    complète ne doit pas trouver porte close. Deux cartes feraient croire à deux dossiers
    à trancher là où il n'y a qu'un retrait à réexaminer."""
    _, jeton, _ = await _contestation_deposee(
        client, moderator_client, session_factory, "Première explication."
    )
    await client.post(f"/contester/{jeton}", data={"texte": "Je complète : deuxième."})

    page = await moderator_client.get("/moderation/signalements")

    assert "Demandes de réexamen à lire (2)" in page.text
    assert "2 demandes déposées, dont 2 non lues" in page.text
    assert "Première explication." in page.text
    assert "Je complète : deuxième." in page.text
    # Un seul dossier, donc un seul rappel du texte de la proposition dans le bloc de
    # tête : deux blocs auraient dédoublé la décision à prendre.
    assert page.text.count("2 demandes déposées") == 1


async def test_une_demande_dont_le_retrait_a_ete_annule_le_dit_et_ne_disparait_pas(
    client, moderator_client, session_factory
):
    """**L'angle mort que ce lot ferme.** Annuler un retrait remet la proposition en
    ligne et ferme la voie de recours, mais les contestations restent en base. Sans
    mention d'état, elles s'affichaient comme des demandes en souffrance : le responsable
    rouvrait un dossier qu'il avait lui-même tranché en faveur de l'auteur.
    """
    statement_id, _, _ = await _contestation_deposee(
        client, moderator_client, session_factory, "Je conteste ce retrait, en détail."
    )

    await moderator_client.post(
        f"/moderation/signalements/{statement_id}/annuler", follow_redirects=False
    )

    page = await moderator_client.get("/moderation/signalements")
    assert "retrait annulé depuis" in page.text
    # Elle ne disparaît pas : c'est elle qui a pu motiver l'annulation, et elle reste à
    # lire tant que personne ne l'a lue.
    assert "Je conteste ce retrait, en détail." in page.text


async def test_une_demande_sur_un_retrait_confirme_porte_le_geste_d_annulation(
    client, moderator_client, session_factory
):
    """**Option A du MOD-13, tranchée par dom.**

    La file ne montre que les signalements NON TRAITÉS : un retrait confirmé n'a plus de
    carte, donc plus de bouton. Une demande déposée après la confirmation — le cas le plus
    probable, puisque le jeton est transmis à la main — restait alors lisible sans aucun
    geste atteignable. Le geste est porté sur le dossier lui-même.
    """
    statement_id, _, _ = await _contestation_deposee(
        client, moderator_client, session_factory, "Réexaminez, même après coup."
    )

    await moderator_client.post(
        f"/moderation/signalements/{statement_id}/confirmer", follow_redirects=False
    )

    page = await moderator_client.get("/moderation/signalements")
    assert "Réexaminez, même après coup." in page.text
    assert "signalement déjà traité" in page.text
    # La carte a bien disparu — et pourtant l'annulation est atteignable.
    assert f"/moderation/signalements/{statement_id}/confirmer" not in page.text
    assert f"/moderation/signalements/{statement_id}/annuler" in page.text


async def test_le_geste_porte_par_le_dossier_remet_bien_la_proposition_en_circulation(
    client, moderator_client, session_factory
):
    """Le bouton n'est pas un décor : il poste sur la route existante du MOD-3a, et celle-ci
    remet la proposition en ligne. Aucune route de décision neuve n'a été écrite."""
    statement_id, _, _ = await _contestation_deposee(
        client, moderator_client, session_factory, "Ma proposition citait une source."
    )
    await moderator_client.post(
        f"/moderation/signalements/{statement_id}/confirmer", follow_redirects=False
    )

    reponse = await moderator_client.post(
        f"/moderation/signalements/{statement_id}/annuler", follow_redirects=False
    )

    assert reponse.status_code == 303
    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.moderation_status is ModerationStatus.approved
    page = await moderator_client.get("/moderation/signalements")
    assert "retrait annulé depuis" in page.text


async def test_le_dossier_ne_double_pas_le_bouton_quand_la_carte_est_la(
    client, moderator_client, session_factory
):
    """Deux endroits pour une même décision sont deux occasions de se contredire : tant
    que la carte est dans la liste, c'est elle qui porte les gestes."""
    statement_id, _, _ = await _contestation_deposee(
        client, moderator_client, session_factory, "Je conteste tout de suite."
    )

    page = await moderator_client.get("/moderation/signalements")

    # Le signalement est encore en attente : la carte est là, et le bouton n'existe qu'une
    # fois dans la page.
    assert page.text.count(f"/moderation/signalements/{statement_id}/annuler") == 1
    assert "sa carte est plus bas" in page.text


async def test_le_dossier_ne_propose_ni_confirmer_ni_classer(
    client, moderator_client, session_factory
):
    """Un seul bouton, et non les trois de la carte.

    « Confirmer » écrirait au journal d'audit un second `retrait_confirme` décrivant un
    retrait déjà confirmé — un acte qui n'a pas eu lieu ce jour-là, dans une table qu'on ne
    peut plus jamais corriger. « Classer sans suite » ne clôt que des signalements en
    attente, et il n'en reste aucun : le bouton serait sans effet.
    """
    statement_id, _, _ = await _contestation_deposee(
        client, moderator_client, session_factory, "Un réexamen, s'il vous plaît."
    )
    await moderator_client.post(
        f"/moderation/signalements/{statement_id}/confirmer", follow_redirects=False
    )

    page = await moderator_client.get("/moderation/signalements")

    assert f"/moderation/signalements/{statement_id}/annuler" in page.text
    assert f"/moderation/signalements/{statement_id}/confirmer" not in page.text
    assert f"/moderation/signalements/{statement_id}/classer" not in page.text
    # Et l'écran dit pourquoi maintenir ne demande rien, plutôt que de laisser conclure
    # qu'un bouton manque.
    assert "Maintenir le retrait ne demande aucun geste" in page.text
