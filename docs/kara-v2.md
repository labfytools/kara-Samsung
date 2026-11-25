# Kara V2 · Soirée depuis le téléphone et la télévision

La page accessible par le QR code réunit le prénom et la recherche par titre,
artiste ou album. Le joueur choisit un résultat puis valide. Il peut changer
sa sélection avant son tour et ajouter d’autres morceaux. Le navigateur garde
son identité de salle et sa recherche. Le serveur garde les choix, l’ordre,
les résultats et les préférences audio pendant la vie du processus.

## Imports et préparation

Quand la recherche locale est vide, **Rechercher sur YouTube** demande le titre
et l’artiste. Le moteur YouTube Music affiche les éditions et leurs pistes.
**Importer cet album** exige la confirmation des droits, puis enregistre la
demande dans la file SQLite commune avec Parole. Deux demandes pour le même
album actif partagent le lot. Le joueur revient à sa recherche après
l’indexation et choisit explicitement son morceau.

La file conserve les journaux, les résultats et les données de reprise.
Un lot réussi passe dans l’historique une fois ses pistes visibles dans le
catalogue. Les détails d’un lot partiel gardent les éléments qui demandent
une action. En cas de relance, les pistes déjà publiées servent de reçus.
Les paramètres et protections de la [file Parole](parole-imports.md) s’appliquent
aussi aux demandes mobiles. ArchASP utilise la surveillance de collection de
Strawberry pour indexer les fichiers publiés.

Valider un choix planifie sa préparation immédiatement. Le contrôleur partage
le traitement par chemin audio et réutilise les reçus Turbo et les pistes
Demucs vérifiés. Les LRC sources et les seuils existants restent les références.
La préparation anticipée tourne à priorité CPU réduite et laisse passer un
tour prêt. Une préparation interrompue peut repartir depuis ses caches.
Les imports déjà admis poursuivent leur travail avec les limites de la file.

## Tour et organisation

Le premier morceau prêt permet d’ouvrir le salon. Les suivants attendent leur
préparation. L’organisateur peut changer l’ordre, reporter puis replacer un
joueur, relancer un traitement et autoriser un tour sans note quand le mix
est disponible. La télévision présente les joueurs, le départ et les paroles.
**Nouvelle partie** revient aux inscriptions.

Le compte à rebours est piloté par le serveur. Le joueur actif et
l’organisateur peuvent le demander lorsque le téléphone transmet, que la
télévision authentifiée répond et que Strawberry est disponible. Une
interruption pendant le compte à rebours ramène le chanteur à l’attente.
Le retour vocal est activé au départ de la chanson.

Pendant le tour, **Musique**, **Chanteur original** et **Ma voix** règlent
trois flux distincts. Le fader de retour et sa coupure laissent le signal
d’analyse disponible. Le plafond musical et les protections de retour
s’appliquent toujours. Les valeurs définies par l’organisateur sont attribuées
aux nouveaux joueurs. L’organisateur peut aussi rétablir ces valeurs pour un
joueur déjà inscrit.

Le fond de la page passe du neutre au rouge, à l’orange, au vert olive ou au
vert selon les scores publiables par le moteur. Les paliers sont à 40, 70
et 90 %. Quand la couverture empêche le moteur de publier un score, la page
reste neutre et le résultat final indique la couverture disponible.

## BlackBox

Parole et l’organisateur mobile pilotent la même session BlackBox. Le panneau
mobile affiche l’état de collecte, la place disponible et les erreurs connues.
Les diagnostics sont disponibles avant l’ouverture. Avec l’accord de
l’organisateur et celui du chanteur pour son tour, la collecte conserve les
pistes audio cochées, dans le format BlackBox existant. Le bouton **Signaler
un problème** ajoute un marqueur. À la fin, le même vérificateur d’intégrité
que le CLI contrôle les fichiers et les index PCM clos.

## Télévision et HTTPS

Le WGT interroge `https://kara.labfytools.com:8443/tv/etat` à son ouverture.
Ce chemin fonctionne dès que le service répond, même sans joueur ni tour.
Le téléviseur authentifie sa requête avec une clé locale dédiée. Le PIN à
quatre chiffres est renouvelé toutes les cinq minutes et le budget de cinq
essais est partagé sur cette période. Le QR encode le lien HTTPS de jeu.
Le téléviseur réessaie automatiquement entre 750 ms et 12 secondes en cas de
panne, puis retrouve l’état du serveur.

Voir la [procédure WGT](../tizen/app/README.md) pour préparer et signer une
mise à jour. Garder le paquet signé dans un dossier privé. L’application
embarquée contient la clé de cet écran.

## Démarrage avec Sway

L’unité [kara.service](../contrib/systemd/user/kara.service) est prête pour la
session utilisateur d’ArchASP. Elle attend PipeWire et demande le lancement de
l’unité d’autostart Strawberry. La préférence Strawberry
`resumeplayback=false` évite de relancer la musique à l’ouverture. Les
certificats restent fournis par la configuration HTTPS existante et les
connexions renouvelées récupèrent la génération TLS courante.

`paroles-strawberry --service` garde invisible la fenêtre du contrôleur et
utilise la même identité D-Bus que le raccourci Parole. Le raccourci présente
cette fenêtre au besoin. L’unité journalise ses sorties et redémarre sur
échec. Le démarrage de Kara laisse Whisper, le microphone et BlackBox au
repos. Activer l’unité avec l’accord du propriétaire de la session et après
la fin des imports actifs. Une coupure du processus conserve les imports
dans la file SQLite pour reprise et termine les sessions BlackBox récupérables.
Les identités et tours de la partie résident en mémoire et sont récupérés
par les clients tant que le serveur de cette partie continue de tourner.
