# Calibration audio expérimentale

## État de validation

La branche `feat/audio-calibration-engine` contient une campagne bornée à un bouton. Un clic sur « Calibration complète » lance un amorçage puis six chirps et six séquences PN alternés. L’amorçage est exclu des statistiques. Aucune compensation de délai, de gain ou de musique n’est appliquée.

Les tests logiciels du 23 novembre 2026 se terminent sans erreur sur les références effectivement générées par le serveur. Les écarts maximaux observés par le test DSP sont de 0,0002 ms sur les cas synthétiques propres et de 0,1341 ms sur les cas synthétiques perturbés par dérive, bruit et filtres. Ces valeurs décrivent les fixtures testées. Toute précision physique reste à mesurer.

Le protocole Samsung S21FE décrit ci-dessous reste à réaliser. Aucune campagne matérielle analogique ou EDENWOOD n’a été effectuée pour valider ce moteur. Les observations historiques autour de 120 ms restent sans cause confirmée.

## Architecture et contrats

| Composant canonique | Fonction |
| --- | --- |
| [`bin/_kara_micro_test.html`](../bin/_kara_micro_test.html) | Préparation du micro, campagne, horloge de commande, analyse locale et affichage |
| [`bin/_kara_micro_worklet.js`](../bin/_kara_micro_worklet.js) | Capture du premier canal, PCM int16 par paquets de 20 ms, séquence et fin exacte en frames |
| [`bin/_kara_audio_calibration.js`](../bin/_kara_audio_calibration.js) | Rééchantillonnage de référence, corrélation, détection et agrégation, utilisables dans le navigateur et Node |
| [`bin/_kara_micro_test.py`](../bin/_kara_micro_test.py) | HTTPS sur le port 8766, authentification, contrôle d’origine et routage WebSocket |
| [`bin/_kara_audio_calibration.py`](../bin/_kara_audio_calibration.py) | Génération déterministe et émission bornée des sondes par PipeWire |

Le navigateur ouvre `/audio?calibration=1` avec le jeton privé, la sortie choisie et la fréquence de capture. La page et `/worklet.js` exigent le jeton. `/calibration.js` expose le moteur DSP public. Une session audio exclusive protège l’émission pendant la calibration.

Le micro reste dans le navigateur. Le Worklet fonctionne avec un gain de 1 et une destination muette. Aucun PCM micro n’est envoyé au serveur pendant la calibration. Le serveur reçoit les commandes textuelles `probe:warmup`, `probe:chirp`, `probe:pn`, un ping borné et `stop`.

Chaque sonde est un bloc mono de 4 800 échantillons signés int16 little endian à 48 kHz, soit 100 ms et 9 600 octets. Le serveur transmet cette référence binaire après la lecture, suivie d’un objet JSON dont `probe` vaut `warmup`, `chirp` ou `pn`. Le navigateur vérifie la taille exacte, le format déclaré et l’association avec la commande en attente.

Le chirp balaie de 600 à 6 000 Hz. La PN provient d’une séquence pseudo-aléatoire déterministe, limitée spectralement entre 600 et 6 000 Hz avec des épaules progressives. Les deux références ont des rampes de 10 ms et une crête normalisée à 0,18 avant quantification int16. L’amorçage reprend le chirp. Le balayage répartit l’énergie dans le temps et la PN la répartit simultanément dans la bande, ce qui donne deux signatures distinctes à comparer lorsque l’enceinte, le codec ou le micro modifient la réponse. Une impulsion concentre l’énergie sur trop peu d’échantillons et sollicite un spectre plus large. Une MLS répétée pourrait ajouter des ambiguïtés de période. Les sondes choisies sont courtes et émises une seule fois par essai.

### Émission PipeWire

Chaque sonde démarre son propre sous-processus `pw-cat`. Le serveur effectue exactement une écriture du bloc PCM borné dans son entrée standard, attend le drainage du tampon Python, ferme l’entrée pour produire EOF puis attend la fin du processus. La référence retournée correspond au PCM écrit. Le chemin de calibration utilise directement ce PCM, sans transformation non linéaire ni réécriture par paquets.

Les options courantes sont `--playback --raw --format s16 --rate 48000 --channels 1 --latency 10ms`, une cible explicite et les propriétés `node.dont-reconnect=true node.dont-fallback=true node.dont-move=true`. Les 10 ms sont une demande de configuration. Le démarrage du processus, la négociation et le drain participent encore à la campagne.

Aucune attente fixe de 80 ms ne précède l’écriture. `--sample-count` est supprimé du chemin d’émission. Le code de PipeWire 1.6.11 quitte sur cette limite avant le chemin de drain déclenché par EOF, ce qui motive l’écriture bornée suivie de la fermeture d’entrée. Les points de référence sont [la limite de compte dans pw-cat.c](https://github.com/PipeWire/pipewire/blob/1.6.11/src/tools/pw-cat.c#L948) et [le code de retour après vidage](https://github.com/PipeWire/pipewire/blob/1.6.11/src/tools/pw-cat.c#L2500). Ce comportement reste à vérifier acoustiquement sur la machine cible.

La sortie `analog` utilise la cible locale `alsa_output.pci-0000_63_00.6.analog-stereo`. La sortie `default` résout `pactl get-default-sink` au début de la session et conserve cette cible pour la campagne. EDENWOOD doit donc être choisie manuellement comme sortie par défaut avant le clic. Ces choix dépendent de l’installation locale.

### Bornes et arrêt

| Élément | Comportement actuel |
| --- | --- |
| Campagne navigateur | 1 amorçage puis 12 essais utiles, chirp et PN alternés |
| Espacement client | Attente de 1 100 ms avant chaque commande, en plus du traitement précédent |
| Limites serveur | 1 amorçage, 6 sondes par méthode, au moins 1 s entre commandes, une sonde active |
| Session | Maximum serveur de 60 s et inactivité de 15 s, garde globale navigateur de 90 s |
| Préparation client | Autorisation micro bornée à 10 s, reprise du contexte et chargement Worklet à 5 s chacun |
| Réponse et capture | Attentes bornées à 5 s pour le prêt serveur, la réponse de sonde et la capture requise |
| Sous-processus | Démarrage borné à 2 s, drainage Python à 0,3 s, sortie après EOF à 1,5 s |
| Ping/pong | Un ping client après le prêt, valeur décimale à trois chiffres, validation serveur et intervalle minimal de 1 s |
| Mémoire de capture | Historique local de 5 s, fenêtre analysée inférieure à 1,6 s |

L’arrêt manuel, la fermeture réseau, une piste terminée, un contexte suspendu, une réponse invalide ou une rupture de frames interrompent la campagne. Une rupture de séquence ou de continuité invalide la campagne entière. Le navigateur coupe les connexions audio, arrête les pistes, ferme le contexte et le WebSocket et libère les buffers de capture. Une autorisation micro arrivée après annulation provoque également l’arrêt des pistes. Le serveur annule le travail actif et conserve le verrou de session pendant le nettoyage du sous-processus, y compris si une annulation survient pendant l’attente après SIGTERM.

## Horloges et grandeur mesurée

Le point de départ est `AudioContext.currentTime × sampleRate`, conservé avec sa partie fractionnaire juste avant l’envoi de commande. Le Worklet marque chaque paquet avec `endFrame = currentFrame + i + 1`, la fin exacte du dernier échantillon du paquet. L’instant d’arrivée du message sur le thread JavaScript sert à la télémétrie. Sur le thread de contrôle, `currentTime` avance par quantum de rendu et fournit une ancre à la résolution de cette horloge. Un quantum de 128 frames à 48 kHz représente environ 2,67 ms. L’interpolation du pic dans la capture apporte une autre résolution, celle de l’alignement de la forme d’onde.

La capture comprend environ 400 ms avant la commande, jusqu’à 800 ms de recherche après la commande, les 100 ms de référence et 250 ms de marge. L’inclusion des paquets entiers ajoute moins de 40 ms au total. Le bruit ambiant est évalué dans la portion située environ entre −400 et −100 ms. Le DSP cherche un départ de référence entre 0 et 800 ms après l’ancre.

Le délai affiché va de l’ancre de commande à la première arrivée détectable dans le signal micro capturé. Il inclut le transport aller, le traitement serveur, le démarrage du lecteur, les tampons de sortie, la restitution, la propagation acoustique et la chaîne de capture du téléphone. Les horloges du serveur et du téléphone restent distinctes.

| Observable | Interprétation et limite |
| --- | --- |
| Délai commande → micro reçu | Délai global de la campagne selon l’horloge de capture, avec seuils de détection |
| Médiane, MAD et min/max | Répétabilité empirique des essais retenus dans les conditions présentes |
| Corrélation, score de confiance, polarité et réflexions | Qualité heuristique de l’alignement et pics secondaires détectables |
| RMS, crête, nombre de saturés, gain et rapport signal/bruit estimés | Niveaux numériques relatifs à la référence et au bruit local |
| RTT WebSocket | Aller-retour de messages, sans séparation du transport aller |
| Worklet → JS p95 | Âge de livraison des paquets selon l’horloge audio, sur au plus 1 200 paquets |
| Durées serveur | `process_startup_ms`, `request_to_write_ms`, `drain_ms`, `request_to_drain_ms`, `request_to_exit_ms` et préparation serveur |

Le retour de `stdin.drain()` concerne le tampon de transport Python. La sortie réussie de `pw-cat` renseigne le cycle du processus. Aucun de ces événements ne fournit l’instant physique de sortie du haut-parleur.

La campagne ne sépare pas les délais du câble, du Bluetooth, de PipeWire, du Wi-Fi et du micro. Le RTT ne justifie aucune soustraction de sa moitié. La latence du chant transmis en direct et l’alignement avec la musique nécessitent leurs propres mesures. Une comparaison analogique/EDENWOOD reste une comparaison des deux chaînes complètes dans les conditions notées.

Le navigateur demande la désactivation de l’annulation d’écho, de la réduction de bruit et du gain automatique. Une piste déclarant un de ces traitements actif provoque un arrêt. Un réglage absent reste inconnu. Le contexte doit être actif à 44,1 ou 48 kHz. Les tampons et traitements internes du système, du navigateur et du matériel de capture restent inconnus.

## Algorithme et synthèse

La référence serveur est rééchantillonnée vers l’horloge du contexte avec une sinc fenêtrée de rayon 16, qui conserve l’origine temporelle et limite le repliement spectral. La corrélation linéaire utilise une FFT avec remplissage par zéros, une référence centrée et une normalisation par la variance de chaque fenêtre candidate. La valeur absolue permet une inversion de polarité.

Les maxima locaux passent un seuil adaptatif de corrélation `max(0,17, médiane du fond + 9 × MAD du fond)` et un seuil d’amplitude corrélée `max(0,00035, bruit RMS × 0,48, amplitude maximale admissible × 0,22)`. Les oscillations locales sont regroupées avec une garde de 2,5 ms. Le premier pic admissible devient l’arrivée estimée. Une arrivée physique trop faible peut rester sous le seuil et laisser un retour ultérieur être retenu.

Le sommet du pic est affiné par interpolation parabolique sur trois valeurs. Une interpolation cubique du signal aligné permet ensuite d’estimer corrélation, gain, polarité et rapport signal/bruit. Les retours secondaires sont conservés. Une saturation, trop de pics concurrents ou une compétition proche non séparable peuvent rejeter l’essai. Le score `confidence` est une heuristique bornée entre 0 et 1.

Chaque méthode dispose de six essais. La synthèse calcule une première médiane et une MAD sur les essais acceptés, puis marque comme aberrants les écarts supérieurs à `max(2 ms, 3,5 × 1,4826 × MAD)`. Elle recalcule médiane, MAD et minimum/maximum sur les essais retenus. Un essai rejeté ou aberrant reste visible dans le tableau avec son motif ou son écart.

Une méthode est dite stable si au moins cinq essais sont retenus, avec MAD ≤ 4 ms, étendue ≤ 12 ms et qualité médiane ≥ 0,5. Le meilleur résultat est classé par stabilité, nombre retenu, MAD puis qualité. Le statut `reliable`, affiché « Mesure fiable », exige aussi une seconde méthode avec au moins quatre essais retenus, une étendue ≤ 12 ms et des médianes distantes d’au plus 8 ms. Un résultat disponible qui ne satisfait pas ces critères est `indicative`. L’absence de résultat est `failed`.

Ces statuts décrivent les règles de synthèse de la campagne. La MAD et l’étendue sont empiriques. Six essais par méthode, des dépendances possibles entre essais et des biais matériels inconnus ne permettent pas de justifier un intervalle de confiance absolu. La précision d’interpolation synthétique ne constitue aucune garantie de précision physique.

L’interface affiche le meilleur résultat, les niveaux en cours et le bruit ambiant. Les détails repliables comprennent les statistiques par méthode, les douze lignes d’essai et les durées serveur. Les réglages exposés sont limités à la fréquence, au nombre de canaux et aux trois indicateurs de traitement. Les identifiants bruts de cible et de périphérique sont masqués dans le rapport. Les captures restent temporaires en mémoire locale.

## Historique du remplacement

L’ancien reproducer synthétique propre utilisait un délai de 250 ms et les décalages résiduels suivants. Les maxima étaient recherchés dans la fenêtre 200–380 ms.

| Décalage résiduel en ms | Score maximal historique |
| --- | --- |
| 0 | 1 |
| 0,125 | 0,169 |
| 0,25 | 0,208 |
| 0,5 | 0,325 |
| 1 | 0,178 |
| 1,5 | 0,325 |

L’analyse de V6 avait identifié une réduction vers 8 kHz par simple stride sans filtre anti-repliement, une recherche grossière limitée au pas de 2 ms, une référence en décalage avec la transformation serveur non linéaire `tanh` et son traitement par paquets, une fin de paquet Worklet inexacte et une porte de lecture de 520 ms réarmable par `monitor:on` qui acceptait du PCM fourni par le client. Le protocole actuel ferme la session si un client lui envoie du PCM ou une commande de monitoring. Ces constats motivent les références exactes et l’horodatage continu du moteur actuel. Ils ne permettent pas d’attribuer une cause unique aux observations physiques autour de 120 ms.

Les anciens tests V2 à V6 et le diagnostic de forme d’onde sont remplacés par les suites moteur et capture ci-dessous. Les anciens résultats restent des éléments d’historique.

## Commandes reproductibles

Depuis la racine du dépôt, avec Node.js et Python disposant de NumPy, SciPy et aiohttp, les vérifications suivantes ne nécessitent aucun matériel audio et n’affichent aucun jeton.

```bash
node --check bin/_kara_audio_calibration.js
node --check bin/_kara_micro_worklet.js
node tests/test-kara-calibration-engine.js
node tests/test-kara-calibration-capture.js
(for test in tests/test-kara-*.py; do python3 "$test" || exit; done)
git diff --check
```

Le test moteur appelle le générateur Python réel. Il couvre 44,1 et 48 kHz, des délais entiers et fractionnaires, les bords 0 et 800 ms, des échos, une inversion de polarité, du bruit, une dérive synthétique de 450 ppm, des filtres, les rejets et l’agrégation. Le test capture utilise un environnement simulé et couvre les limites de paquet, les pertes, les délais de livraison, l’annulation, le contrat binaire, une campagne complète et le nettoyage. Le test de campagne simulée remplace l’analyse DSP par un double de test.

La suite [serveur](../tests/test-kara-audio-calibration.py) couvre les références, l’authentification, les commandes, les limites, l’exclusivité, les erreurs de lecture et le nettoyage. Les tests de lecture utilisent des sous-processus simulés. Un test de nettoyage utilise aussi un processus Python sans audio qui ignore SIGTERM afin de vérifier la terminaison sous annulations répétées.

Sur le poste HTTPS déjà configuré, lancer le serveur dans un terminal privé.

```bash
python3 -u bin/_kara_micro_test.py
```

Ouvrir dans Chrome sur le téléphone le lien authentifié fourni localement, avec le certificat reconnu et l’accès au réseau local. Garder le lien privé. Les rapports partageables omettent les jetons, les identifiants et les données personnelles.

Pour noter la version et observer le graphe pendant les essais, utiliser séparément les commandes suivantes.

```bash
pw-cat --version
pw-cat --help
pw-top
```

L’aide locale PipeWire 1.6.11 a été inspectée pendant la préparation. Le [manuel pw-cat de la série 1.4](https://docs.pipewire.org/1.4/page_man_pw-cat_1.html) documente les options et l’entrée standard. Le [manuel pw-top](https://docs.pipewire.org/page_man_pw-top_1.html) explique les statistiques du graphe, dont le quantum et la fréquence. La durée `QUANT / RATE` décrit une période d’ordonnancement. Le [contrat Web Audio](https://www.w3.org/TR/webaudio-1.0/) décrit les horloges et le traitement audio utilisés par le navigateur.

## Protocole physique S21FE à réaliser

1. Préparer le poste Linux et le Samsung S21FE sur le réseau local. Noter les versions du navigateur, d’Android et de PipeWire, le placement du téléphone, la distance au haut-parleur et les volumes. Réduire le bruit ambiant et garder une position fixe. Ouvrir la page HTTPS de confiance et autoriser le micro.
2. Commencer par les haut-parleurs intégrés du PC. Vérifier que la sortie analogique les alimente, puis choisir la sortie analogique dans la page. Régler un volume modéré sur la chaîne de sortie avant la campagne. Le gain de capture de calibration est fixé à 1 et les curseurs du retour vocal ne règlent pas l’amplitude des sondes.
3. Cliquer une seule fois sur « Calibration complète ». Laisser l’amorçage puis les douze essais se terminer sans parler, déplacer le téléphone ni changer de sortie. Surveiller bruit, crête et saturation. Le bouton « Annuler » reste disponible.
4. Relever le statut, le meilleur résultat et, pour chaque méthode, acceptés, retenus, médiane, MAD et min/max. Relever les motifs de rejet, aberrants, niveaux, réflexions, réglages connus ou inconnus et télémétrie. Toute rupture de frames interrompt la campagne. Conserver un compte rendu expurgé des données privées.
5. Après la fin de la campagne analogique, connecter EDENWOOD et la choisir manuellement comme sortie par défaut dans le gestionnaire audio du poste. Vérifier cette destination avant de continuer. Choisir la sortie PipeWire active par défaut dans la page. Conserver autant que possible position, distance, environnement et conditions de volume, en notant chaque changement.
6. Cliquer une seule fois pour la campagne EDENWOOD et relever les mêmes informations. Chaque sortie reçoit ainsi son propre clic et sa propre campagne complète. Le moteur ne bascule pas automatiquement entre les sorties.
7. Comparer les deux comptes rendus en tenant compte des conditions physiques, des essais retenus et de l’accord entre méthodes. Toute nouvelle campagne doit être identifiée séparément. Aucune correction automatique ou manuelle de synchronisation n’est appliquée par ce protocole.

Les résultats physiques, les captures système et les latences propres aux composants restent inconnus jusqu’à l’exécution et à l’analyse de ce protocole.
