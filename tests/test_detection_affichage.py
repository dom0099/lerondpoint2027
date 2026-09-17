"""La question posée pendant la frappe (chantier Modération, MOD-2).

Fichier distinct de `tests/test_detection.py`, sur le modèle de la séparation
`test_nommage.py` / `test_nommage_affichage.py` du chantier E : l'un éprouve le
calcul, l'autre ce que le site en fait.

Ce qui est éprouvé ici :

1. l'endpoint `/api/detection` — son contrat, et surtout le fait qu'il **ne touche
   pas la base** et **ne rend aucun verdict** ;
2. le branchement des gabarits : les champs marqués, le script chargé ;
3. et la propriété qui compte le plus pour ce lot : **rien n'est bloqué**. Une
   proposition qui déclenche un signal part exactement comme les autres.
"""

import pytest
from httpx import AsyncClient

from app.services.conversations import MAX_STATEMENT_LENGTH
from app.services.detection import CIBLE_PERSONNES
from tests.conftest import open_conversation

#: Une proposition de la forme « Les X + verbe ». Sert de fil rouge à tout le fichier.
CIBLANTE = "Les fonctionnaires travaillent moins que les autres"
#: La même intention, écrite sans cibler personne. Ne doit rien déclencher.
NEUTRE = "Il faut revoir l'organisation du service public"


# --------------------------------------------------------------------------------
# L'endpoint
# --------------------------------------------------------------------------------


async def test_l_endpoint_rend_les_signaux_d_un_texte(client: AsyncClient) -> None:
    reponse = await client.post("/api/detection", json={"text": CIBLANTE})
    assert reponse.status_code == 200
    signaux = reponse.json()["signaux"]
    assert [s["code"] for s in signaux] == [CIBLE_PERSONNES]
    (signal,) = signaux
    assert signal["extrait"] == "Les fonctionnaires travaillent"
    assert CIBLANTE[signal["debut"] : signal["fin"]] == signal["extrait"]


async def test_l_endpoint_ne_rend_rien_sur_un_texte_neutre(client: AsyncClient) -> None:
    reponse = await client.post("/api/detection", json={"text": NEUTRE})
    assert reponse.status_code == 200
    assert reponse.json()["signaux"] == []


async def test_l_endpoint_ne_rend_aucun_verdict(client: AsyncClient) -> None:
    """Le contrat du module tient jusque dans la réponse HTTP.

    Si un champ de jugement apparaissait ici, le navigateur pourrait s'en servir pour
    bloquer un envoi — et le dispositif cesserait d'être une question pour devenir un
    refus, sans qu'une seule ligne de `detection.py` ait changé.
    """
    corps = (await client.post("/api/detection", json={"text": CIBLANTE})).json()
    assert set(corps) == {"signaux"}
    assert set(corps["signaux"][0]) == {"code", "extrait", "debut", "fin"}


async def test_l_endpoint_refuse_au_dela_du_plafond_de_saisie(client: AsyncClient) -> None:
    """Un texte plus long que ce que le formulaire accepte ne vient pas du formulaire."""
    reponse = await client.post("/api/detection", json={"text": "a" * (MAX_STATEMENT_LENGTH + 1)})
    assert reponse.status_code == 422
    assert (await client.post("/api/detection", json={"text": "a" * MAX_STATEMENT_LENGTH})).status_code == 200


async def test_l_endpoint_accepte_un_texte_vide_sans_broncher(client: AsyncClient) -> None:
    """Le navigateur peut l'appeler sur un champ qu'on vient de vider."""
    reponse = await client.post("/api/detection", json={"text": ""})
    assert reponse.status_code == 200


async def test_l_endpoint_ne_touche_pas_la_base(client: AsyncClient, monkeypatch) -> None:
    """Appelé pendant la frappe, il doit être gratuit en base — sinon il EST la charge.

    Le brancher sur le compteur de débit lui ferait écrire une ligne à chaque frappe.
    Ce test rend la propriété structurelle : si quelqu'un ajoute un jour une dépendance
    `get_session` à cette route, il rougit.
    """
    import app.db as db

    appels = []

    vraie_fabrique = db.get_sessionmaker
    monkeypatch.setattr(db, "get_sessionmaker", lambda: appels.append("base") or vraie_fabrique())

    assert (await client.post("/api/detection", json={"text": CIBLANTE})).status_code == 200
    assert appels == []


async def test_l_endpoint_est_deterministe(client: AsyncClient) -> None:
    premier = (await client.post("/api/detection", json={"text": CIBLANTE})).json()
    second = (await client.post("/api/detection", json={"text": CIBLANTE})).json()
    assert premier == second


# --------------------------------------------------------------------------------
# Le branchement des gabarits
# --------------------------------------------------------------------------------


async def test_la_page_d_un_debat_branche_son_champ_de_proposition(
    moderator_client, client: AsyncClient
) -> None:
    _, slug, _ = await open_conversation(moderator_client)
    page = (await client.get(f"/c/{slug}")).text
    assert "data-detection" in page
    assert "/static/detection.js" in page


async def test_la_page_proposer_branche_ses_amorces(client: AsyncClient) -> None:
    page = (await client.get("/proposer")).text
    assert "data-detection" in page
    assert "/static/detection.js" in page


#: Les phrases écrites par le client, et le signal que chacune commente. Recopiées ici
#: pour que le fichier de test soit une affirmation indépendante sur ce qui s'affiche :
#: une phrase réécrite par mégarde dans le script doit faire rougir quelque chose.
#:
#: Les cinq signaux ont chacun la leur depuis le 13 septembre 2026.
PHRASES = {
    CIBLE_PERSONNES: "Quelle est votre proposition à soumettre au vote, exactement ?",
    "deux_idees": "Cette proposition contient deux idées.",
    "affirmation_de_fait": "Ceci semble être une affirmation de fait, plus qu'une proposition.",
    "interrogation": "Ceci semble être une question, plus qu'une proposition.",
    "longueur": "Cette proposition est trop courte pour qu'on puisse voter dessus.",
}


async def test_le_script_est_servi_avec_les_phrases_du_client(
    client: AsyncClient,
) -> None:
    """Les phrases vivent dans le script, à un seul endroit.

    Le serveur continue de rendre les cinq signaux ; c'est le navigateur qui choisit
    quoi en dire. Réécrire une phrase ne demande donc de toucher qu'à ce fichier.

    Ce que ce test NE vérifie pas : que la ligne s'affiche vraiment. Ça se joue dans
    `bancs/detection.banc.mjs`, qui exécute le script dans un DOM simulé — pytest ne
    fait que servir du HTML.
    """
    reponse = await client.get("/static/detection.js")
    assert reponse.status_code == 200
    script = reponse.text
    for code, phrase in PHRASES.items():
        assert f"'{code}'" in script, code
        # L'apostrophe est échappée dans la chaîne JavaScript.
        assert phrase.replace("'", "\\'") in script, phrase


async def test_les_cinq_signaux_ont_tous_leur_phrase(client: AsyncClient) -> None:
    """Plus aucun signal muet : les cinq codes de `detection.CODES` sont couverts.

    Le mécanisme qui ignore un signal sans phrase reste en place — c'est lui qui
    permettra d'ajouter un sixième signal sans rien afficher tant que son texte n'est
    pas écrit — mais plus aucun signal ne s'en sert aujourd'hui.
    """
    from app.services.detection import CODES

    script = (await client.get("/static/detection.js")).text
    debut = script.index("const PHRASES = [")
    fin = script.index("];", debut)
    bloc = script[debut:fin]
    assert set(PHRASES) == set(CODES)
    for code in CODES:
        assert f"'{code}'" in bloc, code


async def test_l_ordre_de_priorite_est_celui_du_serveur(client: AsyncClient) -> None:
    """Une seule ligne s'affiche : celle du premier signal présent, dans l'ordre de `CODES`.

    L'ordre des phrases dans le script doit donc suivre celui du module, sans quoi une
    proposition qui cible des personnes ET pose une question montrerait la mauvaise.
    """
    from app.services.detection import CODES

    script = (await client.get("/static/detection.js")).text
    debut = script.index("const PHRASES = [")
    positions = [script.index(f"'{code}'", debut) for code in CODES]
    assert positions == sorted(positions), "l'ordre des phrases ne suit plus celui de CODES"


async def test_le_style_de_la_question_existe(client: AsyncClient) -> None:
    """Sans cette règle, la question se confondrait avec l'aide statique du dessous."""
    feuille = (await client.get("/static/styles.css")).text
    assert ".question-detection" in feuille


# --------------------------------------------------------------------------------
# Rien n'est bloqué — la propriété la plus importante de ce lot
# --------------------------------------------------------------------------------


async def test_une_proposition_qui_declenche_un_signal_part_comme_les_autres(
    moderator_client, client: AsyncClient
) -> None:
    """Le dispositif pose une question ; il ne refuse rien, et ne doit jamais le faire.

    Si ce test rougit un jour, c'est que le détecteur a cessé d'être une aide pour
    devenir une modération automatique — exactement ce que tout le chantier s'interdit.
    """
    _, slug, _ = await open_conversation(moderator_client)

    envoi = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": CIBLANTE, "sources": []}
    )
    assert envoi.status_code == 201, envoi.text

    # Et la proposition est bien enregistrée, avec son texte intact.
    signaux = (await client.post("/api/detection", json={"text": CIBLANTE})).json()["signaux"]
    assert signaux, "le fil rouge de ce fichier doit bien déclencher"


async def test_la_proposition_enregistree_ne_porte_aucune_trace_du_detecteur(
    moderator_client, client: AsyncClient
) -> None:
    """Le MOD-2 ne marque rien, ne stocke rien, n'ajoute aucun champ.

    Le détecteur tourne dans le navigateur de la personne qui écrit, et s'arrête là.
    """
    _, slug, _ = await open_conversation(moderator_client)
    envoi = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": CIBLANTE, "sources": []}
    )
    assert envoi.status_code == 201
    corps = envoi.json()
    interdits = {"signaux", "signals", "detection", "cible_personnes", "signale"}
    assert interdits.isdisjoint(corps)
