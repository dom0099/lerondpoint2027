# Bancs d'essai JavaScript

Ce dossier ne contient **rien qui soit servi ni déployé**. Il tient les bancs qui
exécutent le JavaScript du site dans un DOM simulé — ce que `pytest` ne peut pas faire,
puisqu'il ne rend que du HTML.

## Jouer le banc du script de saisie (MOD-2)

```
cd bancs && npm install     # une fois : installe jsdom
node detection.banc.mjs     # depuis la RACINE du dépôt
```

Le banc lit `app/static/detection.js`, remplace `fetch` par un faux serveur, simule la
frappe et vérifie ce qui s'affiche. Il sort en code 1 si quelque chose échoue.

**À rejouer dès qu'on touche à `app/static/detection.js`.** Il n'est pas dans la suite
`pytest` parce qu'il demande Node et jsdom, que la machine de production n'a pas.
