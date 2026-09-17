"""Configuration de l'application, lue depuis l'environnement (et .env en local).

Aucune valeur secrète n'est versionnée : voir .env.example pour la liste des clés.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_KEY = "dev-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "dev"

    # Défaut de développement uniquement : docker compose fournit toujours la vraie
    # valeur, et la base n'écoute que sur 127.0.0.1.
    database_url: str = "postgresql+asyncpg://chantierc:chantierc@localhost:5433/chantierc"

    # Base des liens envoyés par e-mail (réinitialisation, vérification).
    # Reste en local tant que l'étape C8 (domaine + TLS) n'est pas faite.
    public_base_url: str = "http://127.0.0.1:8001"

    # Utilisée pour signer les cookies (participant anonyme en C1, session en C1).
    secret_key: str = DEV_SECRET_KEY

    # Adresse publiée dans les mentions légales et la politique de confidentialité.
    # C'est par elle que s'exercent les droits RGPD : elle doit être **relevée**.
    # Réglable par l'environnement pour ne pas figer une adresse dans le code.
    contact_email: str = "contact@lerondpoint2027.fr"

    ###### Analyse (red-dwarf) ######
    #: Période de recalcul du worker, en secondes. Le worker cale ses passages sur une
    #: grille absolue (voir app/services/recalcul.py) : à 600, les calculs ont lieu à 0,
    #: 10, 20, 30, 40 et 50 minutes de chaque heure, et non « dix minutes après le
    #: précédent ».
    #:
    #: Passée de 3600 à 900 au G9, puis de 900 à 600 au chantier E0, sur mesure et non
    #: à l'estime : un passage complet sur 300 débats de 300 participants prend 195 s,
    #: soit 33 % d'une fenêtre de 10 minutes (contre 22 % au quart d'heure).
    #:
    #: **Le coût de ce réglage n'est pas le calcul, ce sont les durées qu'il redéfinit
    #: en silence.** Tout ce qui se compte en TOURS change de sens quand il change de
    #: valeur, sans qu'aucun test ne rougisse. Au G9 il y en avait deux ; l'inventaire
    #: refait au E0 en a trouvé trois, et ils ne se traitent pas de la même façon :
    #:
    #:  - le lisseur du nombre de groupes est désormais réglé en SECONDES
    #:    (`analysis_k_buffer_seconds`), parce que ce qu'on en attendait était une
    #:    durée d'attente avant qu'un groupe nouveau paraisse ;
    #:  - `clivage.CALCULS_POUR_ETIQUETTE` reste compté en tours, délibérément : c'est
    #:    un anti-rebond, et un anti-rebond se compte en mesures, pas en minutes ;
    #:  - la fenêtre d'oubli des couleurs a disparu au G10, la couleur ne dépendant
    #:    plus de l'histoire d'un groupe mais de son effectif.
    analysis_interval_seconds: int = 600
    #: Fixé pour que deux exécutions sur les mêmes données donnent le même résultat.
    #: Sans cela, k-means part d'une initialisation aléatoire et les groupes bougent.
    analysis_random_state: int = 42
    #: Défaut de red-dwarf : un participant ayant moins de 7 votes est écarté de la
    #: matrice. Élevé pour une conversation jeune — d'où le réglage explicite ici.
    analysis_min_user_votes: int = 7
    #: En dessous, PCA + k-means ne veut rien dire : le calcul est marqué
    #: « insufficient_data » plutôt que d'inventer des groupes.
    analysis_min_participants: int = 4
    #: Combien de TEMPS un nombre de groupes candidat doit tenir avant de remplacer le
    #: nombre affiché (chantier D). Le nombre de tours effectif s'en déduit à la
    #: cadence courante — voir `recalcul.tours_de_lissage()`.
    #:
    #: **Pourquoi une durée, alors que le lisseur compte des tours.** Ce réglage a
    #: toujours eu deux faces : côté bénéfice il compte des MESURES (trois silhouettes
    #: d'accord valent mieux qu'une, c'est le défaut mesuré au D0) ; côté coût il
    #: compte des MINUTES (le temps qu'un groupe réellement nouveau attend avant de
    #: paraître). C'est la face « coût » qu'on veut tenir stable quand la cadence
    #: bouge : à 3 tours figés, l'attente valait 3 h au rythme horaire, 45 min au
    #: quart d'heure et n'aurait plus valu que 30 min à 10 minutes — trois réglages
    #: différents portant le même nom.
    #:
    #: Une heure est retenue : entre les 3 h d'origine et les 45 min du G9, et cela
    #: fait 6 tours à la cadence de 600 s, donc plus de mesures qu'avant et non moins.
    analysis_k_buffer_seconds: int = 3600
    #: Âge au-delà duquel un calcul dépassé rend ses grosses tables (chantier G9).
    #: Le calcul lui-même est conservé : la ligne `analysis_run` pèse quelques octets
    #: et c'est la trace qu'on relit quand un résultat est contesté. Ce sont
    #: `participant_projection` (une ligne par personne et par calcul) et
    #: `statement_stat` qui sont purgées, et jamais celles du DERNIER calcul abouti
    #: d'une conversation — c'est lui que le site entier lit.
    #:
    #: Sans cette purge, 300 débats de 300 participants recalculés toutes les 10
    #: minutes écrivent 12,9 millions de lignes de projection par jour (8,6 au quart
    #: d'heure). 48 heures laissent de quoi enquêter sur un calcul douteux, et bornent
    #: la table : c'est une DURÉE, elle ne change donc pas de sens avec la cadence —
    #: seul le régime permanent monte, d'un facteur 1,5.
    analysis_history_retention_hours: int = 48
    ###### Nommage des groupes par modèle de langue (chantier E4) ######
    #: **Désactivé par défaut, et ce n'est pas de la prudence de façade.** Le nommage
    #: est la seule partie de ce projet qui dépend d'un service extérieur au processus.
    #: Tant que ce drapeau est faux, la file du E1 s'empile et rien ne la vide : le site
    #: se comporte exactement comme avant, et « Groupe B » reste affiché. Allumer le
    #: nommage doit être un geste délibéré, fait le jour où un serveur d'inférence
    #: tourne pour de bon.
    naming_enabled: bool = False
    #: Le serveur llama.cpp, sur la boucle locale. Aucune donnée de vote ne sort de la
    #: machine — c'est ce qui rend l'architecture du E3 indépendante de l'avis juridique
    #: sur l'article 9 RGPD, toujours en attente par ailleurs.
    naming_base_url: str = "http://127.0.0.1:8081"
    #: Plafond de charge, par passage du worker. C'est le débit borné du E3 : la file
    #: absorbe les pics, le consommateur ne les subit pas. À 10 noms par passage et un
    #: passage toutes les 10 minutes, on tient 1 440 noms par jour — très au-delà de ce
    #: que la règle « dès que nécessaire » devrait produire, et très en deçà de ce qui
    #: saturerait la machine (mesuré au E2 : ~37 s par débat de 4 groupes).
    naming_max_per_pass: int = 10
    #: Au-delà, on renonce à ce nom plutôt que de réessayer sans fin. Une déclaration
    #: qui fait suffoquer le modèle bloquerait sinon la file pour toutes les autres.
    naming_max_attempts: int = 3
    #: Un appel qui dépasse ce délai est abandonné. Mesuré au E2 : un débat de 4 groupes
    #: prend ~75 s sur cette machine, donc 300 s laissent une marge de quatre sans
    #: permettre à un appel parti en vrille de tenir le consommateur indéfiniment.
    naming_timeout_seconds: int = 300
    #: Plafond de jetons produits par appel. Relevé de 400 à 900 après le E2b : les deux
    #: seules réponses perdues sur vingt-six l'avaient été par dépassement, pas par
    #: hallucination. La grammaire borne déjà chaque champ ; ce plafond n'est plus qu'un
    #: garde-fou de dernier recours.
    naming_max_tokens: int = 900

    ###### Vidéo de présentation d'un débat (chantier Vidéo, VIDEO-1) ######
    #: Où vivent les fichiers vidéo transcodés et leurs miniatures. Seul réglage de ce
    #: chantier à passer par l'environnement, et c'est délibéré : la résolution, le débit
    #: et le seuil de durée sont des décisions de produit, écrites une fois en tête de
    #: `app/services/video.py` ; ce chemin-ci est une décision de MACHINE — il n'est pas
    #: le même sur un poste de développement et sur le VPS, où il désignera le répertoire
    #: que `nginx` sert déjà pour les fichiers statiques.
    #:
    #: La base ne contient que des chemins RELATIFS à cette racine : déplacer les médias
    #: (ou monter un disque dédié) ne demandera donc aucune migration.
    video_racine: str = "media/videos"

    ###### Envoi et alerte (chantier Vidéo, VIDEO-2 — décisions du 13/09/2026) ######
    #: Plafond du fichier REÇU, en octets, avant tout transcodage. Une vidéo 4K de 25 s
    #: depuis un téléphone récent pèse ~150 Mo ; 200 Mo laisse de la marge sans laisser un
    #: envoi interminable occuper un gabarit de modération pour un fichier qui sera de
    #: toute façon refusé sur sa durée. Vérifié PENDANT la réception (voir
    #: `app/routers/moderation.py`), pas seulement après : inutile de laisser grossir un
    #: fichier temporaire au-delà de ce qu'on acceptera jamais.
    video_max_octets: int = 200_000_000

    #: Adresse prévenue quand `ffmpeg`/`ffprobe` manque (`video.OutilAbsent`) — une panne
    #: de serveur, jamais la faute de qui a envoyé le fichier. Vide en développement : le
    #: journal suffit tant qu'il n'y a personne à réveiller pour de vrai.
    alert_email: str = ""

    #: Concavité des enveloppes de groupe (chantier D4). red-dwarf fige 4.0 dans
    #: son code de tracé ; ici c'est un réglage, parce que ce nombre décide à quel
    #: point le contour épouse les creux d'un groupe — une décision d'affichage,
    #: pas un invariant. Plus petit = plus épousant, plus grand = plus lisse.
    hull_concavity: float = 4.0

    # Relais SMTP — le même Brevo que le déploiement Pol.is, mais avec sa propre copie
    # des identifiants : couper Pol.is ne doit pas casser l'authentification d'ici.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_secure: bool = False
    smtp_user: str | None = None
    smtp_password: str | None = None
    # L'adresse doit rester un expéditeur vérifié côté Brevo ; seul le nom d'affichage
    # est libre (leçon du 2026-08-31, voir DEPLOY_LOG.md de Pol.is).
    mail_from: str = "Le Rond-Point 2027 <noreply@lerondpoint2027.fr>"


settings = Settings()


def check_production_safety() -> None:
    """Refuse de démarrer hors dev avec la clé de signature de développement."""
    if settings.environment != "dev" and settings.secret_key == DEV_SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY vaut encore la valeur de développement alors que "
            f"ENVIRONMENT={settings.environment!r}. Générez-en une : openssl rand -hex 32"
        )
