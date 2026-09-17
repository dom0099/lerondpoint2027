"""La liste fermée des thèmes, et ce qu'on peut lui demander.

Vingt-et-une entrées, arrêtées par le client le 5 septembre 2026 (chantier J). Le
**code court** est ce qui va en base et dans l'URL ; le libellé n'existe qu'à
l'affichage et peut être réécrit sans migration.

**Pourquoi une constante et non une table.** Une taxonomie ouverte dérive :
« transports », « transport » et « mobilité » cohabitent au bout de trois mois, et le
filtre ne trie plus rien. Une liste administrable serait un écran de plus à écrire, à
protéger et à tester, pour une donnée qui bougera deux fois par an. C'est le même
choix que `TRIS` dans `app/services/accueil.py`.

**Retirer un code d'ici ne casse rien** : les débats qui le portent gardent leur ligne,
le code cesse simplement d'être proposé et d'être affiché. C'est ce qui rend la liste
modifiable sans migration — mais aussi ce qui fait qu'un retrait est silencieux, d'où
`themes_connus()`, qui filtre plutôt que de lever.
"""

from dataclasses import dataclass

#: Nombre de thèmes qu'un débat peut porter. Zéro le rendrait invisible au filtre —
#: c'est ce qui fonde l'obligation ; au-delà de trois, l'étiquetage ne trie plus rien.
THEMES_MIN = 1
THEMES_MAX = 3


@dataclass(frozen=True)
class Theme:
    code: str
    libelle: str
    #: Ce qui tombe dans ce thème, et la frontière à connaître. Sert au modérateur,
    #: affiché en aide sur les écrans d'étiquetage — pas de la documentation morte.
    aide: str


#: L'ordre est celui de la liste arrêtée, et il est celui de l'affichage : ce n'est
#: ni l'alphabet ni une fréquence, c'est un regroupement par voisinage de sujet, pour
#: qu'un modérateur balaye la grille au lieu de la lire.
THEMES: tuple[Theme, ...] = (
    Theme("agriculture", "Agriculture et alimentation",
          "Exploitations, revenu agricole, pesticides, qualité alimentaire, pêche. "
          "L'eau et les sols vont en Environnement."),
    Theme("economie", "Économie et fiscalité",
          "Croissance, entreprises, industrie, commerce, chômage, impôts, dette, "
          "budget, niches. Les salaires et les cotisations vont en Travail."),
    Theme("travail", "Travail et retraites",
          "Salaires, temps de travail, conditions, syndicats, âge de départ, emploi "
          "public. Le chômage va en Économie."),
    Theme("sante", "Santé",
          "Hôpital, déserts médicaux, prévention, médicaments. Grand âge et handicap "
          "vont en Solidarités."),
    Theme("solidarites", "Solidarités, handicap et grand âge",
          "Minima sociaux, pauvreté, dépendance, accessibilité. L'assurance vieillesse "
          "va en Travail."),
    Theme("education", "Éducation et recherche",
          "École, collège, lycée, université, laboratoires. L'apprentissage va en "
          "Travail."),
    Theme("famille", "Lien social, famille et enfance",
          "Petite enfance, protection de l'enfance, jeunesse, isolement, liens "
          "intergénérationnels, vie associative et bénévolat. L'école va en Éducation, "
          "la dépendance en Solidarités."),
    Theme("logement", "Logement et urbanisme",
          "Loyers, construction, foncier, sans-abrisme. L'artificialisation va en "
          "Environnement."),
    Theme("transports", "Transports et mobilités",
          "Train, route, vélo, aérien, tarifs. Le prix du carburant va en Énergie."),
    Theme("environnement", "Environnement et climat",
          "Biodiversité, eau, air, déchets, adaptation. Le nucléaire va en Énergie."),
    Theme("energie", "Énergie",
          "Production, réseaux, prix, nucléaire, renouvelables. Les objectifs "
          "d'émissions vont en Environnement."),
    Theme("justice", "Justice et sécurité",
          "Police, prisons, tribunaux, délinquance, droit d'asile, rétention, recours. "
          "Les frontières vont en International."),
    Theme("institutions", "Institutions et démocratie",
          "Constitution, élections, référendum, décentralisation, partis, nationalité. "
          "L'exécution et l'accès aux services vont en Administration."),
    Theme("international", "Europe, international et défense",
          "UE, traités, diplomatie, armées, aide au développement, contrôle des "
          "frontières. Le droit d'asile va en Justice."),
    Theme("numerique", "Numérique et libertés",
          "Données personnelles, réseaux sociaux, IA, surveillance, illectronisme. "
          "La fibre en zone rurale va en Territoire."),
    Theme("culture", "Culture et médias",
          "Patrimoine, spectacle vivant, audiovisuel, presse, langue française. "
          "La publicité en ligne va en Numérique."),
    Theme("sport", "Sport",
          "Pratique sportive, équipements, grands événements. La vie associative va "
          "en Lien social."),
    Theme("egalite", "Égalité et discriminations",
          "Égalité femmes-hommes, racisme, LGBT+, laïcité. L'accessibilité va en "
          "Solidarités."),
    Theme("territoire", "Territoire, ruralité et outre-mer",
          "Fracture territoriale, aménagement, outre-mer, périurbain. C'est "
          "l'inégalité entre lieux : un désert médical va en Santé, l'urbanisme en "
          "Logement, une ligne fermée en Transports — sauf quand le sujet est "
          "l'abandon d'un territoire."),
    Theme("administration", "Administration et service public",
          "Simplification, démarches, guichets, accès aux droits, non-recours, "
          "organisation de l'État. Institutions décide, Administration exécute ; le "
          "statut et les salaires des agents vont en Travail."),
    Theme("demographie", "Démographie",
          "Natalité, vieillissement, solde migratoire, volume de l'immigration. Le "
          "financement des retraites va en Travail, le grand âge en Solidarités, la "
          "politique familiale en Lien social."),
)

#: Index par code, construit une fois. `THEMES` reste la source de l'ORDRE.
PAR_CODE: dict[str, Theme] = {theme.code: theme for theme in THEMES}


def libelle(code: str) -> str:
    """Le libellé d'un code, ou le code lui-même s'il a quitté la liste.

    Ne lève pas : un code retiré de la constante survit en base, et une page qui
    l'affiche ne doit pas rendre une erreur 500 pour un débat étiqueté l'an dernier.
    """
    theme = PAR_CODE.get(code)
    return theme.libelle if theme else code


def themes_connus(codes: list[str] | tuple[str, ...] | None) -> list[str]:
    """Filtre une liste de codes sur ceux que la constante connaît, sans doublon.

    L'ordre rendu est celui de `THEMES` et non celui de l'appelant : deux débats
    portant les mêmes thèmes doivent afficher leurs étiquettes dans le même ordre,
    sans quoi la comparaison à l'œil sur une liste devient impossible.
    """
    if not codes:
        return []
    demandes = set(codes)
    return [theme.code for theme in THEMES if theme.code in demandes]


def valider_suggestion(codes: list[str] | tuple[str, ...] | None) -> list[str]:
    """Le contrôle d'un étiquetage **suggéré** par un proposeur.

    Plafonne et refuse l'inconnu, mais **n'exige rien** : la règle du client dit « au
    moins un thème exigé à la publication », et `/proposer` ne publie pas — il dépose
    une conversation dans la file. Le proposeur suggère, le modérateur tranche
    (question 5), et une suggestion obligatoire n'est plus une suggestion.

    C'est aussi la seule lecture compatible avec la question 8 : une étape obligatoire
    de plus entre « je veux participer » et « je participe » se paie en abandons.
    """
    retenus = themes_connus(codes)
    inconnus = sorted(set(codes or ()) - set(PAR_CODE))
    if inconnus:
        raise ValueError(f"thème inconnu : {', '.join(inconnus)}")
    if len(retenus) > THEMES_MAX:
        raise ValueError(f"au plus {THEMES_MAX} thèmes")
    return retenus


def valider_interets(codes: list[str] | tuple[str, ...] | None) -> list[str]:
    """Le contrôle des centres d'intérêt **personnels** d'une personne (`/mes-themes`).

    Volontairement sans plafond, à la différence de `valider_suggestion` et
    `valider_choix` : `THEMES_MAX` protège l'ÉTIQUETAGE d'un débat, où trop de
    thèmes ne trie plus rien (décision du client, voir plus haut). Une préférence
    personnelle ne pose pas ce problème — rien n'empêche quelqu'un de s'intéresser à
    dix sujets, et le filtre qu'elle alimente se contente de faire l'union. Refuser
    au-delà de trois serait une contrainte importée d'un autre écran, pas une
    règle qui a sa propre raison d'être ici.

    Garde le même refus de l'inconnu que les deux autres fonctions : une case
    trafiquée n'a pas plus sa place ici que là.
    """
    retenus = themes_connus(codes)
    inconnus = sorted(set(codes or ()) - set(PAR_CODE))
    if inconnus:
        raise ValueError(f"thème inconnu : {', '.join(inconnus)}")
    return retenus


def valider_choix(codes: list[str] | tuple[str, ...] | None) -> list[str]:
    """Le contrôle d'un étiquetage soumis par un formulaire.

    Lève `ValueError` : c'est une saisie à refuser, pas une valeur à corriger en
    silence. Le laxisme de `themes_connus` sert à AFFICHER l'existant ; celui-ci sert
    à ÉCRIRE, et les deux n'ont pas le même devoir.
    """
    retenus = themes_connus(codes)
    inconnus = sorted(set(codes or ()) - set(PAR_CODE))
    if inconnus:
        raise ValueError(f"thème inconnu : {', '.join(inconnus)}")
    if len(retenus) < THEMES_MIN:
        raise ValueError("au moins un thème est exigé")
    if len(retenus) > THEMES_MAX:
        raise ValueError(f"au plus {THEMES_MAX} thèmes")
    return retenus
