/*
  Le banc d'essai du script de saisie (chantier Modération, MOD-2).

  Ce que les tests Python ne peuvent pas faire : **exécuter** `app/static/detection.js`.
  Ils vérifient que le fichier est servi et qu'il contient les bonnes phrases ; ils ne
  disent rien de ce qui se passe quand on tape. Ce banc charge le script dans un vrai
  DOM (jsdom), simule la frappe, et regarde la ligne apparaître.

  Il n'est PAS dans `tests/` et ne tourne pas avec pytest : il demande Node et jsdom,
  que la machine de production n'a pas. C'est un banc, comme ceux de `d0_mesures/` —
  on le joue à la main quand on touche au script.

      node bancs/detection.banc.mjs     (depuis n'importe où dans le dépôt)

  Le `fetch` du navigateur est remplacé par une fonction qui rend les signaux qu'on
  veut : le but est d'éprouver le script, pas de refaire les tests du détecteur.
*/
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { JSDOM } from './node_modules/jsdom/lib/api.js';

// Résolus depuis CE fichier et non depuis le dossier courant : le banc doit se jouer
// aussi bien depuis la racine du dépôt que depuis `bancs/`.
const ICI = fileURLToPath(new URL('.', import.meta.url));
const SCRIPT = readFileSync(`${ICI}../app/static/detection.js`, 'utf8');

let reussis = 0;
let echoues = 0;

function verifie(nom, condition, detail = '') {
  if (condition) { reussis++; console.log(`  ok   ${nom}`); }
  else { echoues++; console.log(`  ÉCHEC ${nom} ${detail}`); }
}

const attendre = (ms) => new Promise((r) => setTimeout(r, ms));

/* Monte une page avec un champ branché, et un faux serveur qui rend `signauxPour(texte)`. */
function page(signauxPour, { maxlength = 500 } = {}) {
  const dom = new JSDOM(
    `<!doctype html><body>
       <form><textarea data-detection maxlength="${maxlength}"></textarea></form>
     </body>`,
    { runScripts: 'outside-only' },
  );
  const { window } = dom;
  const appels = [];
  window.fetch = async (url, options) => {
    const texte = JSON.parse(options.body).text;
    appels.push(texte);
    if (options.signal?.aborted) { throw Object.assign(new Error('abort'), { name: 'AbortError' }); }
    const signaux = signauxPour(texte);
    return { ok: signaux !== null, json: async () => ({ signaux: signaux ?? [] }) };
  };
  window.eval(SCRIPT);
  const champ = window.document.querySelector('textarea');
  const taper = (texte) => {
    champ.value = texte;
    champ.dispatchEvent(new window.Event('input'));
  };
  const ligne = () => window.document.querySelector('.question-detection');
  return { window, champ, taper, ligne, appels };
}

const signal = (code) => [{ code, extrait: 'x', debut: 0, fin: 1 }];

console.log('\nBanc du script de saisie — MOD-2\n');

// --- La ligne apparaît, avec la bonne phrase --------------------------------------
{
  const p = page(() => signal('cible_personnes'));
  verifie('la ligne est créée dès le chargement, vide', p.ligne() !== null && p.ligne().textContent === '');
  verifie('elle porte aria-live=polite', p.ligne().getAttribute('aria-live') === 'polite');
  p.taper('Les fonctionnaires travaillent moins');
  verifie('rien ne part avant la temporisation', p.appels.length === 0);
  await attendre(600);
  verifie('la requête est partie', p.appels.length === 1, `(${p.appels.length})`);
  verifie(
    'la phrase de cible_personnes s’affiche',
    p.ligne().textContent === 'Quelle est votre proposition à soumettre au vote, exactement ?',
    `-> ${JSON.stringify(p.ligne().textContent)}`,
  );
}

// --- Une phrase par signal ---------------------------------------------------------
for (const [code, attendu] of [
  ['deux_idees', 'Cette proposition contient deux idées.'],
  ['affirmation_de_fait', 'Ceci semble être une affirmation de fait, plus qu\'une proposition.'],
  ['interrogation', 'Ceci semble être une question, plus qu\'une proposition.'],
  ['longueur', 'Cette proposition est trop courte pour qu\'on puisse voter dessus.'],
]) {
  const p = page(() => signal(code));
  p.taper('un texte assez long pour être examiné');
  await attendre(600);
  verifie(`la phrase de ${code} s’affiche`, p.ligne().textContent === attendu,
    `-> ${JSON.stringify(p.ligne().textContent)}`);
}

// --- Un signal SANS phrase reste ignoré, sans rien casser --------------------------
// Les cinq signaux ont désormais la leur ; on éprouve le mécanisme avec un code
// inventé, parce que c'est lui qui permettra d'ajouter un sixième signal sans rien
// afficher tant que son texte n'est pas écrit.
{
  const p = page(() => signal('signal_sans_phrase'));
  p.taper('Un texte quelconque, assez long pour être examiné');
  await attendre(600);
  verifie('un signal sans phrase n’affiche rien', p.ligne().textContent === '');
}

// --- Plusieurs signaux : une seule phrase, la plus prioritaire ---------------------
{
  const p = page(() => [...signal('affirmation_de_fait'), ...signal('cible_personnes')]);
  p.taper('Les fonctionnaires coûtent 25 % du budget');
  await attendre(600);
  verifie(
    'quatre signaux, une seule ligne : cible_personnes gagne',
    p.ligne().textContent === 'Quelle est votre proposition à soumettre au vote, exactement ?',
    `-> ${JSON.stringify(p.ligne().textContent)}`,
  );
}

// --- Aucun signal : la ligne reste vide --------------------------------------------
{
  const p = page(() => []);
  p.taper('Il faut rénover les logements anciens');
  await attendre(600);
  verifie('aucun signal, aucune ligne', p.ligne().textContent === '');
}

// --- La ligne DISPARAÎT quand on corrige -------------------------------------------
{
  let vise = true;
  const p = page(() => (vise ? signal('cible_personnes') : []));
  p.taper('Les fonctionnaires travaillent moins');
  await attendre(600);
  verifie('elle est là avant correction', p.ligne().textContent !== '');
  vise = false;
  p.taper('Il faut revoir l’organisation du service public');
  await attendre(600);
  verifie('elle disparaît après correction', p.ligne().textContent === '');
}

// --- Temporisation : une seule requête pour une rafale de frappes -------------------
{
  const p = page(() => signal('cible_personnes'));
  for (const t of ['Les f', 'Les fonc', 'Les fonction', 'Les fonctionnaires travaillent']) {
    p.taper(t);
    await attendre(50);
  }
  await attendre(600);
  verifie('une rafale de frappes ne produit qu’une requête', p.appels.length === 1, `(${p.appels.length})`);
}

// --- Trop court : on n'interroge pas ------------------------------------------------
{
  const p = page(() => signal('longueur'));
  p.taper('Oui');
  await attendre(600);
  verifie('un texte trop court ne part pas au serveur', p.appels.length === 0, `(${p.appels.length})`);
}

// --- Trop long : on n'interroge pas non plus ---------------------------------------
{
  const p = page(() => signal('cible_personnes'), { maxlength: 500 });
  p.taper('a'.repeat(501));
  await attendre(600);
  verifie('au-delà du plafond du champ, rien ne part', p.appels.length === 0, `(${p.appels.length})`);
}

// --- Le serveur répond mal : on n'affiche rien, et rien ne casse --------------------
{
  const p = page(() => null); // ok: false
  p.taper('Les fonctionnaires travaillent moins');
  await attendre(600);
  verifie('un serveur en erreur laisse la ligne vide', p.ligne().textContent === '');
}

// --- Le texte a changé pendant l'aller-retour : on n'affiche pas une réponse périmée -
{
  const p = page((texte) => (texte.startsWith('Les fonctionnaires') ? signal('cible_personnes') : []));
  p.taper('Les fonctionnaires travaillent moins');
  await attendre(600);
  const avant = p.ligne().textContent;
  // On réécrit tout : la réponse précédente ne doit pas se rappliquer.
  p.taper('Il faut revoir l’organisation du service public');
  await attendre(600);
  verifie('une réponse périmée ne se réaffiche pas',
    avant !== '' && p.ligne().textContent === '');
}

// --- Les champs ajoutés après coup sont branchés -----------------------------------
{
  const p = page(() => signal('cible_personnes'));
  const { window } = p;
  const nouveau = window.document.createElement('textarea');
  nouveau.setAttribute('data-detection', '');
  nouveau.maxLength = 500;
  window.document.querySelector('form').appendChild(nouveau);
  // L'observateur travaille en micro-tâche : on lui laisse un tour.
  await attendre(50);
  verifie('un champ ajouté après coup est branché', nouveau.dataset.detectionBranchee === '1');
  nouveau.value = 'Les fonctionnaires travaillent moins';
  nouveau.dispatchEvent(new window.Event('input'));
  await attendre(600);
  const lignes = window.document.querySelectorAll('.question-detection');
  verifie('il a sa propre ligne', lignes.length === 2);
  verifie('et elle porte la phrase',
    lignes[1].textContent === 'Quelle est votre proposition à soumettre au vote, exactement ?',
    `-> ${JSON.stringify(lignes[1].textContent)}`);
}

// --- Un champ n'est branché qu'une fois --------------------------------------------
{
  const p = page(() => signal('cible_personnes'));
  const { window } = p;
  // Une mutation quelconque relance l'observateur : le champ existant ne doit pas
  // recevoir une seconde ligne ni un second écouteur.
  window.document.body.appendChild(window.document.createElement('div'));
  await attendre(50);
  verifie('aucune ligne en double', window.document.querySelectorAll('.question-detection').length === 1);
  p.taper('Les fonctionnaires travaillent moins');
  await attendre(600);
  verifie('aucune requête en double', p.appels.length === 1, `(${p.appels.length})`);
}

console.log(`\n${reussis} réussis, ${echoues} échoués\n`);
process.exit(echoues === 0 ? 0 : 1);
