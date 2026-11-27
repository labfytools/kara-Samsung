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

Confirmer une chanson conserve le choix sans lancer de calcul. Le fichier
audio local suffit pour ouvrir le salon. La recherche multisource de paroles
et de LRC continue dans Parole et après les imports. Les fichiers LRC existants
restent en lecture seule pendant la partie. Le PC et la TV suivent leurs
timestamps de ligne et leurs repères Enhanced LRC quand ils sont présents.
Le décalage manuel par morceau s’applique aux deux affichages.

Le salon affiche le titre, l’artiste, la pochette et les paroles du morceau
en cours dans Strawberry. Le QR reste visible pendant la sélection des
joueurs. Sans LRC, la TV affiche le texte simple disponible ou la pochette.
Une pause fige la position. Un déplacement ou un changement de morceau
repositionne l’affichage.

## Tour et organisation

L’organisateur peut changer l’ordre, reporter puis replacer un joueur.
La télévision passe au jeu à l’ouverture du salon. Strawberry joue le mix
original et le serveur passe au tour suivant à la fin du morceau. Une fois
la partie terminée, la TV suit de nouveau la lecture Strawberry et retrouve
le QR. **Nouvelle partie** permet aux joueurs de confirmer de nouveaux choix.

Le compte à rebours est piloté par le serveur. Le joueur actif et
l’organisateur peuvent le demander après l’essai microphone, lorsque le
téléphone transmet, que la télévision authentifiée répond et que Strawberry
est disponible. Une interruption pendant le compte à rebours ramène le
chanteur à l’essai.

1. Sur le téléphone, **Activer le micro** ouvre le retour avec les protections.
2. Vérifie le nom de la sortie. Parle et observe **Micro reçu** et **Retour
   après traitement**. Les curseurs **Musique** et **Ma voix** sont indépendants.
3. Confirme **J’entends ma voix** après écoute sur l’enceinte, puis choisis
   **Démarrer le tour**. **Couper le retour** reste disponible pendant
   l’essai et le chant. Une reconnexion demande une nouvelle confirmation.

BlackBox consigne les niveaux et la confirmation. Les pistes audio de
l’essai suivent les mêmes autorisations explicites que celles du tour.
[Mesures et structure de gains](audio-latency-optimization.md#retour-en-salle)

Pendant le tour, **Musique** et **Ma voix** règlent séparément le morceau
et le retour. Le plafond musical réserve une marge pour la voix. Les valeurs
par défaut de l’organisateur sont attribuées aux nouveaux joueurs et peuvent
être rétablies pour un joueur déjà inscrit.

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
