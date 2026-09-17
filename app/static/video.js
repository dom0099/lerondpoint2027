// La vidéo de présentation d'un débat, regardée en grand (demande du client du
// 17/09/2026).
//
// Un clic sur une miniature `.lancer-video` ouvre une fenêtre `<dialog>` qui lit la
// vidéo tout de suite, et se referme d'elle-même quand la vidéo se termine. Le même
// script sert la page d'un débat et les cartes de liste — y compris celles chargées
// plus tard au défilement : d'où l'écoute posée sur le document, pas sur chaque
// miniature.
//
// Sans script, ou sans `<dialog>`, rien n'est intercepté : la miniature est un vrai
// lien vers le fichier, que le navigateur lit seul.
//
// Le partage porte l'adresse de la PAGE du débat (`data-partage`), jamais celle du
// fichier : le lien reçu mène au débat, et une vidéo retirée par la modération cesse
// d'être servie. Partage natif du système (téléphones) quand il existe, copie du
// lien sinon.
(function () {
  'use strict';

  if (typeof HTMLDialogElement !== 'function') { return; }

  let fenetre = null;
  let lecteur = null;
  let boutonPartager = null;

  // Créée à la première lecture seulement : la plupart des pages n'en ont pas besoin.
  function creerFenetre() {
    fenetre = document.createElement('dialog');
    fenetre.className = 'fenetre-video';
    fenetre.setAttribute('aria-label', 'Vidéo de présentation du débat');
    fenetre.innerHTML =
      '<video class="fenetre-video-lecteur" controls playsinline></video>' +
      '<div class="fenetre-video-actions">' +
      '<button type="button" class="partager-video">Partager la vidéo</button>' +
      '<button type="button" class="fermer-video">Fermer</button>' +
      '</div>';
    document.body.appendChild(fenetre);
    lecteur = fenetre.querySelector('video');
    boutonPartager = fenetre.querySelector('.partager-video');

    // La fin de la vidéo referme la fenêtre.
    lecteur.addEventListener('ended', () => fenetre.close());
    fenetre.querySelector('.fermer-video').addEventListener('click', () => fenetre.close());
    // Un clic sur le voile (hors de la boîte) ferme aussi : la cible est alors le
    // `<dialog>` lui-même, jamais un de ses enfants.
    fenetre.addEventListener('click', (evenement) => {
      if (evenement.target === fenetre) { fenetre.close(); }
    });
    // Fermée par Échap, le bouton, le voile ou la fin : dans tous les cas on arrête la
    // lecture ET le téléchargement — une vidéo cachée qui continue de parler, ou de
    // charger, est pire qu'une fenêtre qu'on a refermée.
    fenetre.addEventListener('close', () => {
      lecteur.pause();
      lecteur.removeAttribute('src');
      lecteur.load();
    });
  }

  function ouvrir(lien) {
    if (!fenetre) { creerFenetre(); }
    boutonPartager.dataset.titre = lien.dataset.titre || '';
    boutonPartager.dataset.partage = lien.dataset.partage || '';
    boutonPartager.textContent = 'Partager la vidéo';
    lecteur.src = lien.getAttribute('href');
    const couverture = lien.querySelector('img');
    if (couverture) { lecteur.poster = couverture.currentSrc || couverture.src; }
    fenetre.showModal();
    // Dans le geste de l'utilisateur : le navigateur autorise la lecture avec le son.
    const promesse = lecteur.play();
    if (promesse && promesse.catch) { promesse.catch(() => {}); }
  }

  async function partager(bouton) {
    const adresse = new URL(bouton.dataset.partage || location.pathname, location.origin).href;
    const titre = bouton.dataset.titre || document.title;
    if (navigator.share) {
      try {
        await navigator.share({ title: titre, url: adresse });
      } catch (erreur) {
        // Annulé par l'utilisateur : rien à dire.
      }
      return;
    }
    try {
      await navigator.clipboard.writeText(adresse);
      bouton.textContent = 'Lien copié';
    } catch (erreur) {
      // Presse-papiers refusé : on montre l'adresse, à copier à la main.
      window.prompt('Copiez ce lien pour partager la vidéo :', adresse);
    }
  }

  document.addEventListener('click', (evenement) => {
    if (evenement.defaultPrevented || evenement.button !== 0) { return; }
    // Ctrl/Cmd-clic : l'utilisateur veut un nouvel onglet, on le laisse faire.
    if (evenement.metaKey || evenement.ctrlKey || evenement.shiftKey || evenement.altKey) { return; }
    const cible = evenement.target.closest ? evenement.target : evenement.target.parentElement;
    const lien = cible && cible.closest('.lancer-video');
    if (lien) {
      evenement.preventDefault();
      ouvrir(lien);
      return;
    }
    const bouton = cible && cible.closest('.partager-video');
    if (bouton) { partager(bouton); }
  });

  // Le bouton « Partager » de la page du débat est caché dans le gabarit : sans ce
  // script, il ne ferait rien.
  function reveler() {
    document.querySelectorAll('.partager-video[hidden]').forEach((b) => { b.hidden = false; });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', reveler);
  } else {
    reveler();
  }
})();
