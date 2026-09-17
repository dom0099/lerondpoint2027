# Mesures du chantier Vidéo

Le banc du VIDEO-1 : combien pèse une vidéo de 25 s au profil économique, et combien
de temps elle coûte à encoder.

```bash
python video_mesures/preparer_corpus.py            # fabrique le corpus (~400 Mo)
python -m app.cli banc-video --dossier media/banc-corpus/corpus
```

`ffmpeg` doit être installé sur la machine — c'est une dépendance **système**, pas une
bibliothèque Python (`apt install ffmpeg`, ou l'image de `Dockerfile` qui l'embarque
depuis ce chantier).

Rien n'est écrit en base : `banc-video` transcode dans un répertoire temporaire, mesure,
puis efface. Le corpus vit sous `media/`, ignoré par git — aucun binaire n'est versionné.

## D'où viennent les échantillons

Il n'y a pas de téléphone sur la machine de développement, et une mire `testsrc` de
ffmpeg ne mesurerait rien de réel : une image de synthèse, sans grain ni tremblement, se
compresse trois à quatre fois mieux qu'un visage filmé à la main, et donnerait un poids
flatteur et faux.

Le corpus est donc fait en deux temps, honnêtes séparément :

1. **Le contenu est réel.** Neuf extraits de prises de vue de
   [Wikimedia Commons](https://commons.wikimedia.org), librement licenciées (CC BY,
   CC BY-SA ou domaine public — chaque page de fichier porte sa licence), choisies pour
   ressembler à l'usage visé : des gens qui parlent face caméra, des plans à la main, du
   portrait, et deux cas difficiles à encoder (eau en mouvement, animal qui se déplace).
   Les titres exacts sont dans `preparer_corpus.py`, et c'est lui qui les retélécharge.

2. **Le contenant est refait aux réglages d'un téléphone.** Commons ne distribue que du
   WebM/VP9 ré-encodé autour de 2 Mbit/s ; un téléphone rend du H.264 ou du HEVC entre 6
   et 20 Mbit/s. Sans cette étape, le « taux de compression » rapporté par le banc
   comparerait notre sortie à un fichier déjà compressé, donc ne dirait rien.

Les cinq réglages d'enregistrement reproduits :

| Réglage | Conteneur | Codec | Débit | Ce qu'il éprouve |
| --- | --- | --- | --- | --- |
| `android-1080p` | `.mp4` | H.264 | 17 Mbit/s | le cas le plus courant |
| `android-1080p60` | `.mp4` | H.264 | 20 Mbit/s | 60 i/s, que le pipeline ramène à 30 |
| `iphone-hevc` | `.mov` | HEVC (`hvc1`) | 14 Mbit/s | le format qu'aucun navigateur ne lit partout |
| `iphone-rotation` | `.mov` | H.264 | 16 Mbit/s | image couchée **+ matrice de rotation 90°** |
| `entree-de-gamme` | `.mp4` | H.264 | 6 Mbit/s | petite définition, faible débit |

Un neuvième fichier, `trop-long-27s.mp4`, dure volontairement 27 s : c'est celui que le
pipeline doit **refuser**. Un banc qui ne montre que des réussites ne prouve pas que le
contrôle de durée s'applique aussi à de vrais fichiers.

## Ce que ce corpus ne dit pas

- **Pas de 4K.** Les sources sont en 1080p au mieux ; les agrandir fabriquerait une image
  artificiellement facile à encoder, donc un temps d'encodage faussement rassurant.
- **Contenu déjà compressé une fois** chez Commons : notre encodeur travaille sur une
  image très légèrement plus lisse qu'un fichier sorti d'un capteur.
- **Le temps d'encodage dépend de la machine et de sa charge.** Le banc imprime le
  processeur et la version de ffmpeg en tête de son rapport pour cette raison.
