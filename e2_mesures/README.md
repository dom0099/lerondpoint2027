# Banc du chantier E2 — deux modèles 7B, sur CPU, pour nommer des groupes

Ces scripts ne font partie ni de l'application ni de `pytest`. Ils tournent **hors
production**, comme les bancs de `d0_mesures/`, et n'écrivent que dans
`/home/ubuntu/chantier-e2/resultats/`.

## Ce que le E2 doit trancher

Le plan du chantier E (`chantier-e-decisions.md`) demande deux choses, dans cet ordre :
la **qualité des noms produits en français**, qui décide du modèle, et le **débit en
charge réaliste**, qui décide de la machine. Il en ajoute une troisième que le document
du 3 septembre rend indispensable : chercher le **biais** mesuré chez Pol.is, dont les
résumés générés se sont révélés statistiquement plus proches d'un groupe d'opinion que
de l'autre.

## Où est quoi

| | |
| --- | --- |
| `corpus.py` | cinq débats — un vrai, quatre fabriqués avec leur vérité de terrain |
| `invite.py` | l'invite envoyée au modèle, et la lecture de sa réponse |
| `banc_e2.py` | le banc : séquentiel pour la qualité, parallèle pour le débit |
| `/home/ubuntu/chantier-e2/` | le binaire llama.cpp, les modèles, les résultats — **hors du dépôt** |

Les modèles (8,6 Go) et le binaire ne sont pas versionnés : ils se retéléchargent, et un
dépôt n'est pas un entrepôt.

## Rejouer

```bash
cd /home/ubuntu/chantier-c
nice -n 10 python3 e2_mesures/banc_e2.py --modele mistral --fils 6 --creneaux 4
nice -n 10 python3 e2_mesures/banc_e2.py --modele qwen    --fils 6 --creneaux 4
```

**Le `nice` n'est pas décoratif** : la machine qui fait tourner ce banc est celle qui
sert `vote.lerondpoint2027.fr` et le blog. Six fils sur huit cœurs, en priorité basse,
laissent la production répondre — vérifié pendant le banc, l'accueil restait à 0,14 s
sous une charge de 4. C'est aussi ce que « preuve de concept hors production » veut dire
en pratique quand on n'a qu'un serveur.

Le serveur `llama-server` écoute sur `127.0.0.1` seulement, le temps du banc, et s'arrête
avec lui. Rien n'est ajouté au système : le binaire est autonome et `libgomp` est copiée
à côté de lui plutôt qu'installée.

## Les trois axes, et pourquoi ils sont dans cet ordre

**1. La justesse.** Un nom maladroit se corrige en modération ; un nom qui INVERSE la
position d'un groupe est une contrevérité affichée sous le drapeau du débat. C'est le
seul axe où l'échec est grave, et les quatre débats fabriqués existent pour le rendre
mesurable — un débat réel ne dit pas quelle est la bonne réponse.

**2. Le biais.** Chaque débat est joué deux fois, groupes présentés dans un ordre puis
dans l'autre. Si les noms changent selon la PLACE du groupe dans l'invite plutôt que
selon le groupe, le modèle ne nomme pas, il récite. C'est la version mesurable, sur nos
données, du biais trouvé chez Pol.is.

**3. Le débit.** Le chiffre le plus incertain sur cette machine. Le banc de 20-28 tok/s
cité au 5 septembre venait d'un **double EPYC** ; ce serveur est un Haswell virtualisé
**sans AVX-512**. Et surtout le banc mesure le débit **agrégé sous charge parallèle**,
pas celui d'un appel isolé : c'est l'agrégé qui figure dans le chiffrage, et les deux
n'ont aucune raison d'être égaux — le traitement par lots partage la lecture des poids,
donc l'agrégé monte quand le débit d'un appel seul descend.

## Le corpus, et pourquoi il est fabriqué aux trois quarts

Au 6 septembre 2026 la plateforme compte deux débats en ligne, dont un d'essai (« à
cloche pied », « sur les fesses ») dont les groupes ne veulent rien dire. Il en reste
**un**, à deux groupes.

Les quatre débats fabriqués sont construits à l'image du vrai, c'est-à-dire à l'image de
ce que le E1 a mesuré : **deux groupes opposés partagent l'essentiel de leurs
déclarations représentatives, avec des sens inverses.** C'est la forme normale, puisque
la `repness` retient ce qui SÉPARE les groupes. Un corpus où chaque groupe aurait ses
propres déclarations aurait rendu la tâche artificiellement facile et n'aurait rien
prouvé.

Chacun porte un piège précis :

| Débat | Ce qu'il éprouve |
| --- | --- |
| `permis-16` (réel) | le cas de référence, tel qu'il sort de la production |
| `loyers` | les textes sont **identiques** d'un groupe à l'autre, seul le sens sépare |
| `transports` | un troisième groupe qui n'est ni pour ni contre, mais déplace la question |
| `eoliennes` | deux groupes qui invoquent tous deux l'écologie et s'opposent — piège lexical |
| `portable` | quatre groupes, l'échelle supposée par le chiffrage du 5 septembre |
