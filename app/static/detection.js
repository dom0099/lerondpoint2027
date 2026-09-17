/*
  La question posée pendant la frappe (chantier Modération, MOD-2).

  Quand quelqu'un écrit une proposition de la forme « Les X + verbe », une ligne
  apparaît sous le champ et lui demande ce qu'il veut mettre au vote. Elle NE BLOQUE
  RIEN : le bouton reste actif, la proposition part si la personne la maintient. C'est
  une question, pas un refus — et c'est toute la différence entre ce dispositif et une
  modération automatique.

  **Pourquoi un aller-retour au serveur.** Le détecteur, c'est `app/services/
  detection.py` et ses 253 désignations. Le refaire ici créerait deux détecteurs à
  tenir d'accord, et le jour où ils divergeraient, la ligne affichée ne dirait plus la
  même chose que le rapport qui sert à décider.

  **Une phrase par signal, et une seule affichée à la fois.**

  Le 12 septembre, seul `cible_personnes` s'affichait : les autres signaux n'avaient pas
  de texte, et leur en donner celui de `cible_personnes` aurait été du bruit — « quelle
  est votre proposition, exactement ? » devant quelqu'un qui vient d'écrire une date ne
  veut rien dire. Le 13, le client a écrit les phrases manquantes ; l'objection tombe,
  puisque chaque signal dit désormais ce qui le concerne.

  Reste qu'une proposition en déclenche souvent plusieurs. **On n'en montre qu'une**,
  la première dans l'ordre de `PHRASES` : quatre lignes empilées sous un champ ne se
  lisent pas, et la personne n'a de toute façon qu'une chose à faire à la fois. L'ordre
  est celui de `CODES` côté serveur — `cible_personnes` d'abord, parce que c'est le
  signal qui a motivé tout le chantier.

  Les cinq signaux ont désormais leur phrase. Le mécanisme qui ignore un signal sans
  phrase reste en place et reste testé : c'est lui qui permettra d'en ajouter un sixième
  sans rien afficher tant que son texte n'est pas écrit.

  **Tout échoue en silence.** Réseau coupé, serveur muet, script non chargé : le champ
  reste un champ ordinaire et le formulaire fonctionne exactement comme avant. Une aide
  qui se met à afficher des erreurs pour un service que personne n'a demandé est pire
  que pas d'aide du tout.
*/
(() => {
  'use strict';

  /* Les phrases. Écrites par le client, sauf celle d'`interrogation`, calquée sur celle
     d'`affirmation_de_fait` — les deux disent la même chose : ce que vous avez écrit est
     d'une autre nature qu'une proposition. Elles ne sont pas dans le code Python et n'ont
     pas à y être : `detection.py` nomme des formes, pas ce qu'on en dit. Les réécrire ne
     demande que de toucher ce bloc.

     L'ORDRE EST LA PRIORITÉ d'affichage, et il reprend celui de `detection.CODES` — un
     test le vérifie, parce qu'une proposition qui cible des personnes ET pose une
     question doit montrer la première phrase, pas la seconde.

     Un signal absent de cette liste ne s'affiche pas. Les cinq ont désormais la leur ;
     le mécanisme reste là pour qu'un sixième signal puisse exister sans rien afficher
     tant que son texte n'est pas écrit. */
  const PHRASES = [
    ['cible_personnes', 'Quelle est votre proposition à soumettre au vote, exactement ?'],
    ['deux_idees', 'Cette proposition contient deux idées.'],
    ['affirmation_de_fait',
     'Ceci semble être une affirmation de fait, plus qu\'une proposition.'],
    ['interrogation', 'Ceci semble être une question, plus qu\'une proposition.'],
    ['longueur', 'Cette proposition est trop courte pour qu\'on puisse voter dessus.'],
  ];

  // Le délai après la dernière frappe. Assez long pour ne pas interroger au milieu
  // d'un mot, assez court pour que la ligne paraisse répondre à ce qu'on vient
  // d'écrire.
  const ATTENTE_MS = 450;
  // En dessous, il n'y a pas encore de phrase à regarder.
  const LONGUEUR_MINIMALE = 12;

  /* La ligne est créée TOUT DE SUITE et laissée vide, jamais masquée par `hidden`.

     C'est ce qui la rend annonçable : une région `aria-live` retirée de l'arbre
     d'accessibilité — ce que fait `hidden` — n'annonce rien quand elle réapparaît. En
     la laissant vide et présente, le lecteur d'écran annonce le simple changement de
     contenu, et seulement quand la personne s'arrête de taper (`polite`).
     C'est `.question-detection:empty` qui l'efface visuellement. */
  function creer(champ) {
    const element = document.createElement('p');
    element.className = 'question-detection aide-saisie';
    element.setAttribute('aria-live', 'polite');
    champ.insertAdjacentElement('afterend', element);
    return element;
  }

  function brancher(champ) {
    const ligne = creer(champ);
    // Le plafond est lu sur le champ lui-même plutôt que recopié ici : c'est le même
    // nombre que `MAX_STATEMENT_LENGTH` côté serveur, et il n'a pas à exister deux
    // fois. Au-delà, le serveur répond 422 — inutile de lui demander.
    const maximum = champ.maxLength > 0 ? champ.maxLength : Infinity;
    let minuterie = null;
    let encours = null;

    const demander = async () => {
      const texte = champ.value;
      if (texte.trim().length < LONGUEUR_MINIMALE || texte.length > maximum) {
        ligne.textContent = '';
        return;
      }
      // Une requête en cours est abandonnée dès qu'une nouvelle part : sans ça, deux
      // réponses lentes pourraient revenir dans le désordre et la ligne afficherait le
      // résultat d'un texte déjà réécrit.
      if (encours) { encours.abort(); }
      encours = new AbortController();
      try {
        const reponse = await fetch('/api/detection', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: texte }),
          signal: encours.signal,
        });
        if (!reponse.ok) { ligne.textContent = ''; return; }
        const { signaux } = await reponse.json();
        // Le texte a pu changer pendant l'aller-retour : on ne montre une question que
        // si elle porte encore sur ce qui est à l'écran.
        if (champ.value !== texte) { return; }
        // La première phrase dont le signal est présent, et elle seule.
        const codes = new Set(signaux.map(signal => signal.code));
        const trouvee = PHRASES.find(([code]) => codes.has(code));
        ligne.textContent = trouvee ? trouvee[1] : '';
      } catch (erreur) {
        // Une requête abandonnée n'est pas une panne : elle veut dire que la personne
        // tape encore, et la réponse suivante tranchera.
        if (erreur.name !== 'AbortError') { ligne.textContent = ''; }
      }
    };

    champ.addEventListener('input', () => {
      window.clearTimeout(minuterie);
      minuterie = window.setTimeout(demander, ATTENTE_MS);
    });
  }

  // Les champs se désignent par `data-detection` et non par leur identifiant : la même
  // ligne doit pouvoir apparaître sous les amorces de `/proposer`, qui sont plusieurs.
  const brancher_tout = () =>
    document.querySelectorAll('textarea[data-detection]').forEach(champ => {
      if (champ.dataset.detectionBranchee) { return; }
      champ.dataset.detectionBranchee = '1';
      brancher(champ);
    });

  brancher_tout();

  /* Les amorces de `/proposer` s'ajoutent au clic, après le chargement. Un observateur
     les prend quelle que soit la façon dont elles arrivent — c'est plus sûr que
     d'écouter le bouton, qui n'existe que sur une des deux pages, et plus propre que
     de guetter tous les clics du document. */
  if (window.MutationObserver && document.body) {
    new MutationObserver(brancher_tout).observe(document.body, {
      childList: true,
      subtree: true,
    });
  }
})();
