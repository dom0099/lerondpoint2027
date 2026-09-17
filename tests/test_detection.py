"""Le détecteur de signaux de forme (chantier Modération, MOD-1).

Un fichier par sujet, comme le veut la convention du projet. Ce qui est éprouvé ici :

1. le **contrat d'API** — des signaux, jamais un verdict ; des codes descriptifs ;
   un résultat déterministe et idempotent ;
2. pour chacun des cinq signaux, des cas positifs **et des cas négatifs proches**,
   parce qu'un détecteur qui se déclenche partout ne mesure rien ;
3. la **symétrie de la liste de désignations**, couple par couple, nommément. C'est
   le seul test de ce fichier qui protège autre chose que du code ;
4. le **budget de temps** : ce module tournera un jour dans un formulaire.

Aucun test n'a besoin de base : `analyser` est une fonction pure.
"""

import time

import pytest

from app.services import detection
from app.services.conversations import MAX_STATEMENT_LENGTH
from app.services.designations import DESIGNATIONS, FAMILLES, SANS_CONTRAIRE
from app.services.detection import (
    AFFIRMATION_DE_FAIT,
    CIBLE_PERSONNES,
    CODES,
    DEUX_IDEES,
    INTERROGATION,
    LONGUEUR,
    Signal,
    analyser,
    normaliser,
)


def codes(texte: str) -> set[str]:
    """Les codes déclenchés par un texte, sans leurs positions."""
    return {signal.code for signal in analyser(texte)}


# --------------------------------------------------------------------------------
# Le contrat d'API
# --------------------------------------------------------------------------------


def test_analyser_rend_des_signaux_et_jamais_un_verdict():
    """Le détecteur ne décide de rien, et l'API doit l'en empêcher.

    Ce test n'est pas une formalité : tout le chantier repose sur le fait qu'un lot
    ultérieur ne puisse pas raccourcir en demandant à ce module si une proposition
    est acceptable. S'il échoue un jour, c'est que quelqu'un a ajouté le raccourci.
    """
    signaux = analyser("Les riches profitent du système")
    assert all(isinstance(signal, Signal) for signal in signaux)

    interdits = {
        "est_problematique", "problematique", "score", "note", "gravite",
        "rejeter", "refuser", "bloquer", "verdict", "acceptable", "valide",
    }
    assert interdits.isdisjoint(dir(detection))
    assert interdits.isdisjoint(Signal.__dataclass_fields__)


def test_un_signal_porte_son_code_sa_portion_et_sa_position():
    (signal,) = analyser("Les retraités partent en vacances")
    assert signal.code == CIBLE_PERSONNES
    assert signal.extrait == "Les retraités partent"
    assert (signal.debut, signal.fin) == (0, 21)


def test_les_positions_indexent_le_texte_donne_meme_accentue():
    """L'invariant qui fait tout marcher : normaliser ne change pas les longueurs."""
    texte = "Où ? Les élèves prennent le métro — c'est déjà ça."
    assert len(normaliser(texte)) == len(texte)
    for signal in analyser(texte):
        assert texte[signal.debut : signal.fin] == signal.extrait


def test_les_codes_sont_descriptifs_et_jamais_moraux():
    """« cible_personnes », pas « haineux ». Un code moral est un verdict déguisé."""
    moraux = {
        "haineux", "haine", "insulte", "injure", "douteux", "faux", "mensonge",
        "toxique", "mauvais", "grave", "suspect", "abusif",
    }
    for code in CODES:
        assert moraux.isdisjoint(code.split("_")), code


def test_le_resultat_est_deterministe_et_idempotent():
    texte = "Les fonctionnaires travaillent moins ; selon 3 études, les patrons le disent."
    premier = analyser(texte)
    assert premier == analyser(texte)
    assert [s.code for s in premier] == [s.code for s in analyser(texte)]


def test_les_signaux_sortent_dans_l_ordre_de_lecture():
    signaux = analyser("Selon 3 études, les jeunes votent moins et les vieux votent plus ?")
    positions = [(s.debut, s.fin, s.code) for s in signaux]
    assert positions == sorted(positions)


def test_un_texte_sans_rien_a_signaler_ne_rend_rien():
    assert analyser("Il faut rénover les logements anciens avant d'en construire") == []


# --------------------------------------------------------------------------------
# cible_personnes — le signal principal, et le seul qui demande du soin
# --------------------------------------------------------------------------------


CIBLE_POSITIFS = [
    # Le cas discriminant de la consigne : il DOIT se déclencher. Ce n'est pas un
    # défaut — c'est bien la forme « Les X + verbe », quoi qu'elle veuille dire.
    #
    # La consigne du MOD-1 l'écrivait avec « les agriculteurs ». Ce mot est sorti de la
    # liste à la réduction du 15 septembre — un métier ordinaire n'est plus une
    # désignation — et l'exemple est repris avec « les retraités », qui y est resté.
    # C'est le mot qui change, pas la règle illustrée.
    "Les retraités souffrent de la concurrence",
    "Les riches profitent du système",
    "Ces gens-là veulent imposer leur mode de vie",
    "Les fonctionnaires travaillent moins que les autres",
    "Certains élus mentent à leurs électeurs",
    "Beaucoup de retraités votent contre les jeunes",
    "La plupart des chômeurs touchent trop peu",
    # La désignation n'est pas en tête du texte, mais bien en tête de proposition.
    "Non car les jeunes sont irresponsables",
    "Je pense que les patrons abusent de la situation",
    "Le problème est simple. Les banquiers gagnent trop.",
    # Un complément court entre la désignation et le verbe : la fenêtre de trois.
    "Les jeunes de banlieue partent tous à l'étranger",
]

CIBLE_NEGATIFS = [
    # Le contre-exemple de la consigne : la désignation y est complément, pas sujet.
    "Il faut aider les retraités",
    "Une aide d'urgence pour les précaires et les mal-logés",
    "Il faut mieux payer les fonctionnaires",
    "Créer un impôt sur les riches",
    # Une désignation sujet, mais aucun verbe conjugué derrière.
    "Les retraités, premiers touchés par l'inflation",
    "Les jeunes du continent africain",
    # Un verbe, mais trop loin : au-delà de la fenêtre de trois mots.
    "Les élus de la petite commune voisine décident",
    # Aucune désignation de personnes : une politique, pas des gens.
    "Les subventions agricoles augmentent chaque année",
]


@pytest.mark.parametrize("texte", CIBLE_POSITIFS)
def test_cible_personnes_se_declenche(texte):
    assert CIBLE_PERSONNES in codes(texte), texte


@pytest.mark.parametrize("texte", CIBLE_NEGATIFS)
def test_cible_personnes_ne_se_declenche_pas(texte):
    assert CIBLE_PERSONNES not in codes(texte), texte


def test_le_cas_discriminant_de_la_consigne():
    """Les deux phrases de la consigne, côte à côte, parce que c'est tout l'enjeu.

    La première se déclenche **exprès** : le détecteur ne juge pas ce qui est dit, il
    constate une forme. La seconde ne se déclenche pas, parce que « les agriculteurs »
    y est complément — c'est la seule chose que l'approximation « position de sujet »
    doit vraiment savoir faire.
    """
    assert CIBLE_PERSONNES in codes("Les retraités souffrent de la concurrence")
    assert CIBLE_PERSONNES not in codes("Il faut aider les retraités")


def test_la_portion_rendue_va_du_determinant_au_verbe():
    """Souligner le seul nom de groupe accuserait le mot, qui n'a rien de fautif."""
    (signal,) = [
        s
        for s in analyser("Mais les patrons refusent les hausses")
        if s.code == CIBLE_PERSONNES
    ]
    assert signal.extrait == "les patrons refusent"


# --- Le piège des adverbes ------------------------------------------------------


def test_un_adverbe_en_ment_n_est_pas_un_verbe():
    """« rapidement » finit en -ent sans être un verbe : le piège classique."""
    assert detection._est_verbe("rapidement") is False
    assert detection._est_verbe("augmentent") is True
    assert CIBLE_PERSONNES not in codes("Les prix augmentent rapidement")
    assert CIBLE_PERSONNES not in codes("Les agriculteurs rapidement oubliés")


@pytest.mark.parametrize(
    "mot", ["rapidement", "vraiment", "seulement", "souvent", "argent", "dont", "parents"]
)
def test_des_mots_qui_finissent_comme_un_verbe_sans_en_etre_un(mot):
    assert detection._est_verbe(mot) is False


@pytest.mark.parametrize(
    "mot", ["sont", "ont", "font", "vont", "veulent", "peuvent", "doivent", "croient"]
)
def test_les_irreguliers_nommes_par_la_consigne_sont_reconnus(mot):
    assert detection._est_verbe(mot) is True


# --------------------------------------------------------------------------------
# deux_idees
# --------------------------------------------------------------------------------


def test_deux_verbes_conjugues_relies_par_et():
    assert DEUX_IDEES in codes("Les loyers augmentent et les salaires stagnent")


def test_un_point_virgule_suffit():
    assert DEUX_IDEES in codes("Il faut baisser les impôts ; l'État dépense trop")


def test_un_et_entre_deux_noms_ne_declenche_pas():
    assert DEUX_IDEES not in codes("Il faut soutenir l'école et l'hôpital")


def test_deux_infinitifs_relies_par_et_ne_declenchent_pas():
    """« légaliser et encadrer » est une seule idée en deux verbes non conjugués."""
    assert DEUX_IDEES not in codes("Il faut légaliser et encadrer la vente")


# --------------------------------------------------------------------------------
# affirmation_de_fait
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texte",
    [
        # Un nombre PORTEUR d'une unité ou d'un ordre de grandeur : une mesure.
        "Le tabac tue 75 000 personnes par an",
        "Près de 30 % des jeunes ne votent pas",
        "La répression coûte 398 millions d'euros",
        "Il manque 200 000 logements en Île-de-France",
        # Un millésime.
        "En 2014, la consommation avait déjà doublé",
        "La loi date de janvier dernier",
        # Ce qui annonce une source, ou un classement.
        "Selon une étude, la mesure est inefficace",
        "C'est le plus grand chantier du siècle",
        "La France est le premier pays concerné",
    ],
)
def test_affirmation_de_fait_se_declenche(texte):
    assert AFFIRMATION_DE_FAIT in codes(texte), texte


@pytest.mark.parametrize(
    "texte",
    [
        "Il faut interdire la publicité pour le tabac",
        "Le logement devrait être une priorité",
        "On doit mieux répartir l'effort",
    ],
)
def test_affirmation_de_fait_ne_se_declenche_pas(texte):
    assert AFFIRMATION_DE_FAIT not in codes(texte), texte


@pytest.mark.parametrize(
    "texte",
    [
        # Le cas qui a motivé la correction : un âge n'est pas une statistique.
        "Oui car 16 ou 18 ans c'est pareil",
        "Le permis à 18 ans, c'est trop tard",
        "Il faut 3 médecins par village",
        "Ramener la semaine à 32 heures",
        "Deux idées valent mieux qu'une seule proposition",
    ],
)
def test_un_nombre_nu_ne_suffit_plus_a_faire_une_affirmation_de_fait(texte):
    """Décision du client, 13 septembre 2026 — et la correction la plus lourde du lot.

    La première version se déclenchait sur n'importe quel chiffre. Le signal pesait
    alors 17,2 % de la base à lui seul, presque entièrement pour cette raison, et la
    ligne affichée disait « Ceci semble être une affirmation de fait » à quelqu'un qui
    venait d'écrire « 16 ou 18 ans c'est pareil ».

    Un nombre ne compte désormais que s'il porte une unité, un ordre de grandeur, ou
    la forme d'un millésime.
    """
    assert AFFIRMATION_DE_FAIT not in codes(texte), texte


def test_ans_n_est_pas_une_unite_de_mesure():
    """« ans » est volontairement absent d'`UNITES` : c'est tout l'objet de la correction."""
    assert "ans" not in detection.UNITES
    assert AFFIRMATION_DE_FAIT not in codes("Le permis dès 16 ans")
    assert AFFIRMATION_DE_FAIT in codes("16 millions de personnes concernées")


# --------------------------------------------------------------------------------
# interrogation
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texte",
    [
        "Faut-il légaliser le cannabis ?",
        "Faut-il vraiment continuer comme ça",
        "Pourquoi personne ne réagit",
        "Est-ce que la mesure est utile",
        "Comment financer la transition",
        "Que faut-il faire ? Rien.",
    ],
)
def test_interrogation_se_declenche(texte):
    assert INTERROGATION in codes(texte), texte


@pytest.mark.parametrize(
    "texte",
    [
        "Il faut légaliser le cannabis",
        # « qui » et « quel » ailleurs qu'en tête sont des relatifs ordinaires.
        "La personne qui décide doit rendre des comptes",
        "On ignore quel budget sera voté",
    ],
)
def test_interrogation_ne_se_declenche_pas(texte):
    assert INTERROGATION not in codes(texte), texte


# --------------------------------------------------------------------------------
# longueur
# --------------------------------------------------------------------------------


def test_trop_court():
    assert LONGUEUR in codes("Oui")
    assert LONGUEUR in codes("   Non    ")
    # Le cas réel de la base, qui avait motivé le signal.
    assert LONGUEUR in codes("Sur les fesses")


def test_le_plancher_est_celui_qu_a_arrete_le_client():
    """20 caractères, décidé le 12 septembre 2026. Une valeur de jugement, pas un calcul.

    Écrit ici parce que rien dans le code ne la fonde : c'est le seul nombre de ce
    module qu'une relecture ne peut pas vérifier par elle-même, et il doit donc être
    difficile à changer par distraction.
    """
    assert detection.LONGUEUR_MIN == 20
    assert LONGUEUR in codes("a" * 19)
    assert LONGUEUR not in codes("a" * 20)


def test_proche_du_plafond():
    assert LONGUEUR in codes("a" * MAX_STATEMENT_LENGTH)


def test_le_plafond_est_lu_dans_le_code_et_non_recopie():
    """Si `MAX_STATEMENT_LENGTH` bouge, le seuil doit bouger avec lui."""
    assert detection.LONGUEUR_PROCHE_PLAFOND == MAX_STATEMENT_LENGTH * 9 // 10
    assert detection.LONGUEUR_PROCHE_PLAFOND < MAX_STATEMENT_LENGTH


def test_une_longueur_ordinaire_ne_declenche_rien():
    assert LONGUEUR not in codes("Il faut rénover les logements anciens")


# --------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texte",
    [
        "LES RICHES PROFITENT DU SYSTÈME",
        "Les riches profitent du système",
        "Les  riches   profitent  du  système",
        "Les riches profitent du systeme",
    ],
)
def test_casse_accents_et_espaces_multiples_ne_changent_rien(texte):
    assert CIBLE_PERSONNES in codes(texte), texte


def test_les_deux_apostrophes_typographiques_se_valent():
    droite = "Je crois qu'ils mentent. Beaucoup d'élus profitent du système"
    courbe = droite.replace("'", "’")
    assert codes(droite) == codes(courbe)
    assert CIBLE_PERSONNES in codes(courbe)


def test_normaliser_conserve_la_longueur():
    for texte in CIBLE_POSITIFS + CIBLE_NEGATIFS:
        assert len(normaliser(texte)) == len(texte), texte


# --------------------------------------------------------------------------------
# La liste des désignations
# --------------------------------------------------------------------------------


#: Les couples que la liste doit porter **des deux côtés**. Ils sont écrits ici, en
#: dur, et non lus depuis le module : ce test doit être une affirmation indépendante
#: sur la liste, pas un miroir de ce qu'elle contient déjà.
#:
#: C'est la règle la plus importante de tout le chantier. Une liste qui n'attraperait
#: la forme « Les X + verbe » que pour certains groupes serait attaquable le premier
#: jour, et à juste titre.
#: Les couples sur lesquels la règle de symétrie est vérifiée.
#:
#: La réduction du 15 septembre en avait laissé cinq boiteux — un bord resté, l'autre
#: parti. **La décision D10 les a réparés le même jour** : `locataires`, `conservateurs`,
#: `européistes`, `indépendants` et `syndicats` sont revenus, et les cinq couples sont
#: ici avec les autres. La liste passe de 122 à 127 entrées.
#:
#: Quatre couples de l'ancienne liste ont en revanche disparu ENTIÈREMENT —
#: fumeurs/non-fumeurs, grévistes/non-grévistes, contribuables/allocataires,
#: électeurs/abstentionnistes. Ils ne sont plus ici parce qu'il n'y a plus rien à
#: comparer : une symétrie entre deux absences est vraie et ne dit rien.
COUPLES = [
    ("riches", "pauvres"),
    ("patrons", "syndicats"),
    ("fonctionnaires", "indépendants"),
    ("propriétaires", "locataires"),
    ("souverainistes", "européistes"),
    ("progressistes", "conservateurs"),
    ("jeunes", "vieux"),
    ("Parisiens", "ruraux"),
    ("musulmans", "chrétiens"),
    ("croyants", "athées"),
    ("femmes", "hommes"),
    ("Français", "étrangers"),
    ("valides", "handicapés"),
    ("aidants", "aidés"),
    ("gens de gauche", "gens de droite"),
    ("féministes", "masculinistes"),
]

@pytest.mark.parametrize("un,autre", COUPLES)
def test_la_liste_est_symetrique(un, autre):
    assert un in DESIGNATIONS, f"« {un} » manque, alors que « {autre} » est là"
    assert autre in DESIGNATIONS, f"« {autre} » manque, alors que « {un} » est là"


@pytest.mark.parametrize("un,autre", COUPLES)
def test_les_deux_bords_d_un_couple_se_declenchent_pareil(un, autre):
    """La symétrie de la liste doit se voir dans le comportement, pas seulement dedans."""
    assert CIBLE_PERSONNES in codes(f"Les {un} profitent du système")
    assert CIBLE_PERSONNES in codes(f"Les {autre} profitent du système")


def test_la_liste_tient_dans_les_bornes_annoncees():
    """100 à 150 depuis la réduction du 15 septembre 2026.

    La liste comptait 253 entrées et mélangeait deux choses : des catégories
    d'IDENTITÉ — origine, religion, genre, santé — qui servent vraiment à repérer une
    généralisation visant des personnes, et des catégories qui sont le sujet ORDINAIRE
    d'une proposition de politique publique ici : métiers, statuts civils, mots
    génériques. Le signal se déclenchait pareil sur « les fonctionnaires travaillent
    moins que les autres » et sur « les agriculteurs ont besoin de plus d'aides » — la
    seconde n'avait rien à faire déclencher quoi que ce soit.

    Décision de dom, sur ses faux positifs observés : garder l'identitaire, couper le
    reste. 122 entrées.

    La borne reste une borne, dans les deux sens : la liste ne doit ni se remettre à
    enfler, ni fondre au point de ne plus rien attraper.
    """
    assert 100 <= len(DESIGNATIONS) <= 150


def test_aucune_designation_n_est_en_double():
    assert len(set(DESIGNATIONS)) == len(DESIGNATIONS)


def test_les_familles_annoncees_sont_toutes_peuplees():
    attendues = {
        "origine", "religion", "age", "metier", "economie",
        "territoire", "genre", "politique", "sante", "generique",
    }
    assert {famille.code for famille in FAMILLES} == attendues
    for famille in FAMILLES:
        assert famille.designations, famille.code


def test_les_designations_sont_ecrites_sans_determinant():
    """« agriculteurs », pas « les agriculteurs » : le déterminant est de la grammaire.

    S'il se glissait dans la liste, le motif chercherait « les les agriculteurs » et
    l'entrée serait morte sans que rien ne rougisse.
    """
    for designation in DESIGNATIONS:
        premier = normaliser(designation).split(" ")[0]
        assert premier not in {"les", "des", "ces", "la", "le", "un", "une"}, designation


# --------------------------------------------------------------------------------
# Budget de temps
# --------------------------------------------------------------------------------


def test_analyser_tient_sous_cinq_millisecondes():
    """Ce module tournera dans un formulaire : la lenteur y serait un défaut visible.

    Mesuré sur une proposition de longueur MAXIMALE et **dense en déclencheurs** —
    le pire cas pour l'alternation des désignations, pas un texte quelconque.
    """
    pire_cas = ("Les agriculteurs profitent ; les retraités votent et les riches ont tout. " * 10)[
        :MAX_STATEMENT_LENGTH
    ]
    assert len(pire_cas) == MAX_STATEMENT_LENGTH

    analyser(pire_cas)  # hors mesure : premier appel, caches de `re` tièdes
    debut = time.perf_counter()
    for _ in range(20):
        analyser(pire_cas)
    moyenne_ms = (time.perf_counter() - debut) / 20 * 1000

    assert moyenne_ms < 5, f"{moyenne_ms:.2f} ms par appel"


def test_les_motifs_sont_compiles_une_seule_fois():
    """Compilés au chargement, en constantes : `analyser` n'appelle jamais `re.compile`."""
    assert isinstance(detection.MOTIF_SUJET, type(detection.MOTIF_MOT))
    for nom in ("MOTIF_SUJET", "MOTIF_MOT", "MOTIF_FAIT", "MOTIF_INTERROGATION"):
        motif = getattr(detection, nom)
        assert motif is getattr(detection, nom), nom
        assert hasattr(motif, "finditer"), nom


# --------------------------------------------------------------------------------
# La commande rapport-detection
# --------------------------------------------------------------------------------


def test_souligner_entoure_la_portion_declenchante():
    from app.cli import _souligner

    signal = Signal(CIBLE_PERSONNES, "Les riches profitent", 0, 20)
    assert _souligner("Les riches profitent du système", [signal]) == (
        "[[Les riches profitent]] du système"
    )


def test_souligner_fusionne_les_portions_qui_se_recouvrent():
    """Deux signaux au même endroit ne doivent pas imbriquer deux paires de crochets."""
    from app.cli import _souligner

    texte = "Les riches ont 3 maisons"
    chevauchants = [
        Signal(CIBLE_PERSONNES, "Les riches ont", 0, 14),
        Signal(AFFIRMATION_DE_FAIT, "ont 3", 11, 16),
    ]
    souligne = _souligner(texte, chevauchants)
    assert souligne.count("[[") == 1 == souligne.count("]]")
    assert souligne == "[[Les riches ont 3]] maisons"


def test_souligner_remet_une_proposition_multiligne_sur_une_ligne():
    """Le rapport se lit en colonne ; un saut de ligne y casserait l'alignement."""
    from app.cli import _souligner

    assert _souligner("Légaliser,\nmais encadrer", []) == "Légaliser, ⏎ mais encadrer"


def test_un_corpus_vide_ne_divise_pas_par_zero():
    from app.cli import _pourcent

    assert _pourcent(0, 0) == "—"
    assert _pourcent(8, 29) == "27,6 %"


def test_le_fichier_de_corpus_ignore_les_vides_et_les_commentaires(tmp_path):
    from app.cli import _corpus_du_fichier

    fichier = tmp_path / "corpus.txt"
    fichier.write_text(
        "# un commentaire\n\nLes riches profitent\n   \n  # indenté\nIl faut aider\n",
        encoding="utf-8",
    )
    assert [t for t, _ in _corpus_du_fichier(str(fichier))] == [
        "Les riches profitent",
        "Il faut aider",
    ]


@pytest.mark.anyio
async def test_le_rapport_lit_la_base_en_lecture_seule(session_factory):
    """La promesse « aucune écriture » doit être tenue par PostgreSQL, pas par la relecture.

    `_corpus_de_la_base` ouvre sa transaction en `postgresql_readonly`. Ce test vérifie
    que l'option fait bien ce qu'elle dit : une écriture y est refusée par le serveur.
    Sans lui, la garantie du MOD-1 ne tiendrait qu'à la discipline du prochain lot.
    """
    from sqlalchemy import text as sql

    async with session_factory() as session:
        await session.connection(execution_options={"postgresql_readonly": True})
        lecture = await session.execute(sql("SHOW transaction_read_only"))
        assert lecture.scalar() == "on"

        with pytest.raises(Exception) as refus:
            await session.execute(sql("UPDATE statement SET text = text WHERE id = -1"))
        assert "ReadOnly" in str(refus.value)


# --------------------------------------------------------------------------------
# La dérogation à la symétrie
# --------------------------------------------------------------------------------


def test_le_registre_des_entrees_sans_contraire_est_exactement_celui_qui_a_ete_decide():
    """Trois entrées, décidées nommément le 12 septembre 2026. Pas une quatrième.

    La règle de symétrie est ce qui rend cette liste défendable publiquement. Elle a
    été levée une fois, sur trois tournures précises, par une décision explicite du
    client. Ce test est là pour qu'une quatrième exige la même décision au lieu de
    s'ajouter par habitude — c'est la seule chose qui empêche une dérogation ponctuelle
    de devenir une pente.
    """
    assert SANS_CONTRAIRE == {"Français de souche", "islamistes", "complotistes"}


@pytest.mark.parametrize("designation", sorted(SANS_CONTRAIRE))
def test_les_entrees_sans_contraire_sont_bien_dans_la_liste(designation):
    assert designation in DESIGNATIONS
    assert CIBLE_PERSONNES in codes(f"Les {designation} veulent imposer leur loi")


def test_les_entrees_sans_contraire_restent_des_designations_et_non_des_insultes():
    """La dérogation porte sur la SYMÉTRIE, pas sur l'interdiction des injures.

    Cette seconde règle-là n'a pas bougé et ne bougera pas : les trois entrées sont
    des désignations — contestables, politiquement marquées, mais prononçables devant
    la personne visée. Le jour où une injure entre ici, le signal cesse d'être
    grammatical et devient moral, et tout le module change de nature.
    """
    for designation in SANS_CONTRAIRE:
        assert designation.lower() == designation or designation[0].isupper()
        assert len(normaliser(designation).strip()) > 3
