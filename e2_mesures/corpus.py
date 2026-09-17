"""Le jeu d'essai du E2 : cinq débats, dont un vrai.

**Pourquoi il faut des débats fabriqués alors qu'on en a un vrai.** Au 6 septembre 2026
la plateforme compte deux débats en ligne, dont un manifestement d'essai (« à cloche
pied », « sur les fesses ») dont les groupes ne veulent rien dire. Il en reste UN, à deux
groupes. Juger deux modèles là-dessus reviendrait à tirer à pile ou face.

Surtout, un débat réel ne dit pas quelle est la bonne réponse. Sans référence, on peut
constater qu'un nom est laid, jamais qu'il est FAUX — or c'est faux qui compte : un nom
qui inverse la position d'un groupe est pire qu'un nom plat, et c'est exactement ce que
le biais mesuré chez Pol.is laisse craindre. Les quatre débats fabriqués portent donc
chacun leur vérité de terrain.

**Ils sont construits à l'image du vrai.** Le relevé du E1 sur le débat du permis à
16 ans montre que deux groupes opposés partagent l'essentiel de leurs déclarations
représentatives, avec des sens inverses — c'est la forme normale, puisque la `repness`
retient ce qui SÉPARE les groupes. Un corpus où chaque groupe aurait ses propres
déclarations aurait rendu la tâche artificiellement facile et n'aurait rien prouvé.

`position` est la vérité de terrain : ce que le groupe pense VRAIMENT de la question
posée. C'est elle qu'on confronte au nom produit — un nom peut être maladroit sans être
faux, il ne peut pas inverser la position sans l'être.
"""

#: Le seul débat réel exploitable, extrait du calcul 29 (6 septembre 2026) tel quel.
#: Figé ici pour que le banc se rejoue à l'identique : les groupes d'un débat vivant
#: changent, et un banc dont l'entrée bouge ne mesure plus rien.
PERMIS = {
    "cle": "permis-16",
    "reel": True,
    "titre": "Devrait-on autoriser le passage du permis B à 16 ans ?",
    "groupes": [
        {
            "id": 0,
            "position": "contre",
            "reference": "Les opposants au permis à 16 ans",
            "declarations": [
                ("Oui à la campagne car c'est necessaire !", "contre"),
                ("Oui si leur parents sont d'accord", "contre"),
                ("Oui mais pas après 21h", "contre"),
                ("Oui mais en conduite accompagnée", "contre"),
                ("Non car les jeunes sont pas responsables", "pour"),
            ],
        },
        {
            "id": 1,
            "position": "pour",
            "reference": "Les favorables sous conditions",
            "declarations": [
                ("Non car trop d'accidents", "contre"),
                ("Non car les jeunes sont pas responsables", "contre"),
                ("Oui mais en conduite accompagnée", "pour"),
                ("Oui mais pas après 21h", "pour"),
                ("Oui à la campagne car c'est necessaire !", "pour"),
            ],
        },
    ],
}

#: Deux groupes nets, sur des déclarations entièrement partagées. Le cas d'école du
#: piège : les textes seuls sont IDENTIQUES d'un groupe à l'autre, seul le sens sépare.
#: Un modèle qui ne lit pas le sens produira ici deux noms interchangeables.
LOYERS = {
    "cle": "loyers",
    "reel": False,
    "titre": "Faut-il encadrer les loyers dans le centre-ville ?",
    "groupes": [
        {
            "id": 0,
            "position": "pour",
            "reference": "Les partisans de l'encadrement",
            "declarations": [
                ("Se loger en centre-ville est devenu impossible pour un salaire moyen", "pour"),
                ("Un plafond de loyer ferait fuir les propriétaires vers la location saisonnière", "contre"),
                ("L'encadrement a fait ses preuves dans les grandes villes qui l'ont adopté", "pour"),
                ("C'est au marché de fixer les prix, pas à la mairie", "contre"),
                ("Sans plafond, les familles continueront de partir en périphérie", "pour"),
            ],
        },
        {
            "id": 1,
            "position": "contre",
            "reference": "Les opposants à l'encadrement",
            "declarations": [
                ("Se loger en centre-ville est devenu impossible pour un salaire moyen", "contre"),
                ("Un plafond de loyer ferait fuir les propriétaires vers la location saisonnière", "pour"),
                ("L'encadrement a fait ses preuves dans les grandes villes qui l'ont adopté", "contre"),
                ("C'est au marché de fixer les prix, pas à la mairie", "pour"),
                ("Sans plafond, les familles continueront de partir en périphérie", "contre"),
            ],
        },
    ],
}

#: Trois groupes, dont un intermédiaire. Le groupe 2 est le cas difficile : il n'est ni
#: pour ni contre, il déplace la question (« pas gratuit, mais moins cher et plus
#: fréquent »). Un modèle qui ne sait produire que « les pour » et « les contre » le
#: nommera comme l'un des deux autres — défaut invisible sur un débat à deux groupes.
TRANSPORTS = {
    "cle": "transports",
    "reel": False,
    "titre": "Faut-il rendre les transports en commun gratuits dans l'agglomération ?",
    "groupes": [
        {
            "id": 0,
            "position": "pour",
            "reference": "Les partisans de la gratuité totale",
            "declarations": [
                ("La gratuité est le seul moyen de faire baisser vraiment la voiture en ville", "pour"),
                ("Un service gratuit finit toujours par être un service dégradé", "contre"),
                ("Le ticket coûte plus cher à contrôler qu'il ne rapporte", "pour"),
                ("Mieux vaut un réseau payant mais qui passe toutes les cinq minutes", "contre"),
                ("Se déplacer devrait être un droit, pas un budget", "pour"),
            ],
        },
        {
            "id": 1,
            "position": "contre",
            "reference": "Les opposants au nom du financement",
            "declarations": [
                ("La gratuité est le seul moyen de faire baisser vraiment la voiture en ville", "contre"),
                ("Un service gratuit finit toujours par être un service dégradé", "pour"),
                ("Qui paiera l'entretien des rames quand la recette aura disparu ?", "pour"),
                ("Se déplacer devrait être un droit, pas un budget", "contre"),
                ("La gratuité profitera surtout à ceux qui prennent déjà le bus", "pour"),
            ],
        },
        {
            "id": 2,
            "position": "autre",
            "reference": "Ceux qui préfèrent la fréquence au prix",
            "declarations": [
                ("Mieux vaut un réseau payant mais qui passe toutes les cinq minutes", "pour"),
                ("La gratuité est le seul moyen de faire baisser vraiment la voiture en ville", "contre"),
                ("Le problème n'est pas le prix, c'est qu'il n'y a rien après 20 h", "pour"),
                ("Un service gratuit finit toujours par être un service dégradé", "contre"),
                ("Une tarification sociale suffirait pour ceux qui en ont besoin", "pour"),
            ],
        },
    ],
}

#: Deux groupes qui invoquent tous deux l'écologie et s'opposent pourtant. Le piège est
#: lexical : un modèle qui s'accroche aux mots-clés du texte nommera les deux groupes
#: « les écologistes ». C'est le désaccord de VALEURS entre alliés apparents, le cas où
#: un nom paresseux fait le plus de dégâts.
EOLIENNES = {
    "cle": "eoliennes",
    "reel": False,
    "titre": "Faut-il installer des éoliennes sur la commune ?",
    "groupes": [
        {
            "id": 0,
            "position": "pour",
            "reference": "Les partisans de l'éolien local",
            "declarations": [
                ("Le climat n'attend pas, il faut produire propre ici et maintenant", "pour"),
                ("On ne sauve pas la nature en bétonnant les crêtes", "contre"),
                ("Les retombées fiscales financeraient l'école du village", "pour"),
                ("Ces machines massacrent le paysage et les oiseaux", "contre"),
                ("Refuser chez soi ce qu'on accepte ailleurs, c'est de l'hypocrisie", "pour"),
            ],
        },
        {
            "id": 1,
            "position": "contre",
            "reference": "Les opposants au nom du paysage et du vivant",
            "declarations": [
                ("On ne sauve pas la nature en bétonnant les crêtes", "pour"),
                ("Ces machines massacrent le paysage et les oiseaux", "pour"),
                ("Le climat n'attend pas, il faut produire propre ici et maintenant", "contre"),
                ("La sobriété d'abord : produire plus n'a jamais rien réglé", "pour"),
                ("Les retombées fiscales financeraient l'école du village", "contre"),
            ],
        },
    ],
}

#: Quatre groupes, l'échelle prise pour hypothèse dans le chiffrage du 5 septembre
#: (« 4 groupes par débat en moyenne »). Sert autant à la mesure de DÉBIT qu'à la
#: qualité : c'est le format d'appel réel, un appel couvrant tous les groupes d'un débat.
PORTABLE = {
    "cle": "portable",
    "reel": False,
    "titre": "Faut-il interdire le téléphone portable au collège ?",
    "groupes": [
        {
            "id": 0,
            "position": "pour",
            "reference": "Les partisans de l'interdiction totale",
            "declarations": [
                ("Sans portable, les élèves se reparlent pendant la récréation", "pour"),
                ("Interdire un objet n'apprend pas à s'en servir", "contre"),
                ("Le harcèlement en ligne commence dans la cour de récréation", "pour"),
                ("Les parents doivent pouvoir joindre leur enfant à tout moment", "contre"),
                ("La concentration en classe s'est effondrée depuis les smartphones", "pour"),
            ],
        },
        {
            "id": 1,
            "position": "contre",
            "reference": "Les opposants au nom du lien familial",
            "declarations": [
                ("Les parents doivent pouvoir joindre leur enfant à tout moment", "pour"),
                ("Sans portable, les élèves se reparlent pendant la récréation", "contre"),
                ("Un collégien qui rentre seul le soir a besoin de son téléphone", "pour"),
                ("La concentration en classe s'est effondrée depuis les smartphones", "contre"),
                ("C'est aux familles d'éduquer, pas à l'école d'interdire", "pour"),
            ],
        },
        {
            "id": 2,
            "position": "autre",
            "reference": "Les partisans de l'éducation au numérique",
            "declarations": [
                ("Interdire un objet n'apprend pas à s'en servir", "pour"),
                ("Il faudrait des cours sur les réseaux sociaux dès la sixième", "pour"),
                ("Sans portable, les élèves se reparlent pendant la récréation", "contre"),
                ("Le téléphone est un outil de travail comme un autre", "pour"),
                ("Le harcèlement en ligne commence dans la cour de récréation", "contre"),
            ],
        },
        {
            "id": 3,
            "position": "autre",
            "reference": "Les partisans d'une interdiction en classe seulement",
            "declarations": [
                ("Rangé pendant les cours, libre à la récréation : c'est simple à appliquer", "pour"),
                ("Une interdiction totale sera contournée dès le premier jour", "pour"),
                ("Interdire un objet n'apprend pas à s'en servir", "contre"),
                ("C'est aux familles d'éduquer, pas à l'école d'interdire", "contre"),
                ("Les surveillants n'ont pas à fouiller les sacs", "pour"),
            ],
        },
    ],
}

DEBATS = [PERMIS, LOYERS, TRANSPORTS, EOLIENNES, PORTABLE]
