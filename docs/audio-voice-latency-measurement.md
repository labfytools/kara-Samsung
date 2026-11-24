# Mesure de la latence vocale

## Deux mesures complémentaires

Le diagnostic du retour normal observe les cadences, les attentes et les stockages de chaque côté du WebSocket. Le micro témoin mesure séparément l’intervalle acoustique entre un son direct et le retour de ce même son. La calibration chirp/PN de 12 essais garde son propre trajet, de la commande jusqu’à la capture sur Android. Elle fournit une autre information sur la chaîne de sortie.

Le diagnostic s’affiche pendant une session micro démarrée explicitement. Le profil, la taille des blocs PCM, le lecteur et la latence demandée figurent au-dessus du résumé. Les détails repliables montrent les distributions, les cadences, les paramètres et les compteurs. Les réglages d’atténuation anti-écho, les trois profils et les traitements audio restent indépendants de la mesure.

## Points de mesure

| Observable | Point de départ et point d’arrivée | Portée |
| --- | --- | --- |
| Constitution d’un bloc PCM | Début du premier quantum Worklet qui fournit des échantillons du bloc, puis début de celui où le bloc est complet | Temps logique dans le contexte audio. Le quantum est déjà fourni en entier au callback. Ce chiffre renseigne l’attente de constitution, avec une granularité de quantum. |
| Âge fin Worklet → JS | Fin du dernier échantillon du bloc, puis `AudioContext.currentTime` consulté dans le callback de réception | Indicateur sur l’horloge du contexte. Une stagnation du rendu peut cacher une attente réelle et l’arrondi au quantum peut décaler la lecture. Un âge nul signifie un écart non résolu par cette horloge. |
| Intervalles JS et cadence Worklet | `performance.now()` à la réception du message du Worklet, avec l’index de fin de bloc | Les intervalles courts successifs peuvent signaler une livraison en rafale. La cadence du compteur audio est mesurée relativement à l’horloge du thread principal. Elle inclut les effets de la livraison. |
| JS avant envoi et appel d’envoi | Début du callback principal → horodatage juste avant l’envoi, puis retour de `WebSocket.send()` | Traitement local, copie et disponibilité côté JS. Le retour de l’appel marque la remise à la pile. Le tampon WebSocket est observé en octets, avant et après remise de PCM et lors des synthèses. |
| RTT WebSocket | Envoi du ping applicatif et réception du pong sur la même horloge JavaScript | L’aller-retour inclut Wi-Fi, TCP, TLS et l’ordonnancement des deux extrémités. L’aller simple reste inconnu. |
| Transport relatif | Pour chaque bloc, différence entre deux durées écoulées séparément depuis un premier bloc apparié, côté réception Arch puis côté envoi Android | Distribution au-dessus du minimum de la fenêtre récente. L’origine de chaque horloge s’annule. Leur dérive et les queues avant réception peuvent encore déplacer les valeurs. Un recul de l’horodatage client redémarre la fenêtre de transport. |
| Réception Python et file | Message WebSocket binaire disponible dans aiohttp → enfilement, puis enfilement → retrait par le lecteur | Le premier intervalle mesure le travail sur le message valide. Le deuxième mesure l’attente dans la file applicative. La profondeur courante se lit en blocs ou en millisecondes nominales de PCM. Le pic de profondeur est compté depuis l’ouverture. |
| DSP et encodage | Retrait de la file → blocs stéréo prêts à écrire | Inclut conversion NumPy, statistiques de niveau, rééchantillonnage éventuel, compression et préparation PCM. |
| Écriture et drainage Python | Début de `stdin.write()` → retour de l’appel, puis mesure distincte du même début au retour de `drain()` | La durée de drainage englobe l’écriture et l’attente du transport asyncio. Les deux durées imbriquées ne s’additionnent pas. |
| Tampons avant le lecteur | Taille en mémoire asyncio et octets encore présents dans le tube Linux avec `FIONREAD` | Instantanés à la demande de synthèse, environ toutes les 650 ms. Leur équivalent PCM se calcule à 192 octets/ms en stéréo 48 kHz int16. Les maxima de ces instantanés peuvent rater une pointe entre deux interrogations. |
| Cadence de remise au lecteur | Nombre de frames PCM dont le drainage est terminé, rapporté à la durée Linux de la fenêtre | Échantillons remis au chemin de lecture, en frames/s, sur au moins cinq secondes. Les pertes, pauses, rattrapages et la pression aval influencent cette cadence. L’horloge du périphérique reste inconnue. |

Une perte de continuité sur l’horloge audio apparaît dans `worklet_missing_frames`. Cela peut inclure le fragment abandonné lors d’une rupture du Worklet. Les blocs ignorés sur le téléphone sont comptés par motif, les trous de séquence sont observés côté Arch et les rejets de file sont comptés séparément. Un même bloc absent du fil peut être visible dans les compteurs téléphone et séquence. Le compteur d’attentes source observe la file du lecteur et son seuil de grâce. Les notifications Pulse, quand elles existent, observent une autre couche. Les erreurs du graphe se consultent dans `pw-top`.

Le récapitulatif utilise au plus 1 200 observations par série. Les seuils de publication sont de cinq échantillons pour p50, vingt pour p95 et cent pour p99. Le maximum de fenêtre est disponible dès la première observation. Les quantiles sont empiriques au rang supérieur. Les valeurs absentes restent `null` dans le JSON et « — » dans le résumé. La pente compare les médianes du premier et du dernier quart de fenêtre sur au moins cinq secondes. Une hausse d’au moins 1 ms/s est signalée pour une attente locale ou le transport relatif, et une hausse d’au moins 96 o/s pour la taille du tampon WebSocket. Le seuil de pente sert à repérer les dérives. Il ne fournit ni une précision d’horloge ni une cause à lui seul.

Les p95 séparés, issus d’échantillons et de fenêtres potentiellement différents, ne constituent pas un p95 total. Une hausse de la file Arch et des octets du tube aide à localiser une accumulation aval. Une hausse du transport relatif isolée doit être interprétée avec le RTT, la cadence du Worklet et les autres fenêtres. Un écart de cadence entre le Worklet et la remise au lecteur peut provenir de blocs écartés autant que d’un écart d’horloge.

### Limites de la capture et de la sortie

- **Capture Android avant Worklet.** Le navigateur peut exposer `MediaStreamTrack.getSettings().latency`, une valeur de configuration en secondes qui peut varier par rapport à la réalité. `AudioContext.baseLatency` concerne le chemin de sortie de l’AudioContext. L’horloge audio du Worklet date les échantillons du contexte et ne date pas leur acquisition physique. Les buffers ADC, Android et Chrome et les traitements du micro ne sont donc pas mesurés par cette page. Un quantum de 128 frames à 48 kHz, par exemple, vaut 2,67 ms sur cette horloge logique. [Spécification Web Audio](https://www.w3.org/TR/webaudio-1.0/) · [Réglages de capture](https://w3c.github.io/mediacapture-main/#dom-mediatracksettings-latency).
- **Périphérique côté Arch.** Le tube ne couvre ni la lecture stdio, ni les buffers du flux PipeWire, ni le périphérique. Les attributs Pulse `tlength`, `prebuf` et `minreq` exposés par `pacat` décrivent sa configuration effective de tampon. Les 10 ou 20 ms demandées par le lecteur sont des réglages. `pw-top` permet de relever la période du pilote `QUANT / RATE` et ses erreurs en activité. Le graphe local au repos a rapporté un quantum et une fréquence à zéro, et les champs Pulse de latence exportés par le flux Strawberry au repos valaient zéro. Ces relevés au repos ne caractérisent pas le flux Kara. [PipeWire pw-top](https://docs.pipewire.org/page_man_pw-top_1.html).
- **Temps du graphe.** L’API `pw_stream_get_time_n()` d’un flux natif actif donne l’horloge, la file du flux, son rééchantillonneur et un délai déclaré jusqu’au bord du graphe. Python pilote ici des lecteurs externes et ne dispose pas de ce curseur par bloc. La trace de mise en file dans le lecteur ne permet pas d’identifier l’instant acoustique. Avec A2DP, le transport radio, le codec et le tampon de l’enceinte ajoutent des étapes en aval. Le délai réel sortie et le délai total restent inconnus dans le diagnostic intégré. [Horloge de flux PipeWire](https://docs.pipewire.org/structpw__time.html).

## Diagnostic multisource PipeWire V5

Le V5 observe le micro physique `alsa_input.pci-0000_63_00.6.analog-stereo` (canal FL par défaut) et les deux canaux du monitor de `alsa_output.pci-0000_63_00.6.analog-stereo`. Les ports natifs `monitor_FL` et `monitor_FR` appartiennent au même nœud de sortie que le monitor PulseAudio dont le nom se termine par `.monitor`. Le banc lit les ports natifs. Il utilise NumPy et compile son helper dans un répertoire temporaire avec `cc`, `pkg-config` et les en-têtes de `libpipewire-0.3`. La validation a utilisé PipeWire 1.6.11.

### Prévol non interactif

```bash
python3 bin/_kara_pipewire_latency.py --mode preflight > pipewire-preflight.json
```

Le prévol ouvre une lecture stéréo temporaire de zéros via `pw-cat` sur le sink explicitement sélectionné. Il attend que les deux liens de cette lecture soient actifs, puis exige quatre secondes de PCM continu sur le micro et les deux canaux du monitor. Le JSON expose le pilote, la fréquence, le quantum, les positions de début et de fin, le nombre de cycles et le compteur de xrun observé. Il contient uniquement des métadonnées. Le code de sortie zéro et le statut `pipewire_preflight_ready` indiquent que la capture a réussi et que le témoin et la lecture temporaire ont été fermés. La lecture temporaire appartient au seul prévol.

### Deux passes de trois syllabes

Depuis la racine du dépôt, garder le même placement pour le S21 FE, le micro témoin et les haut-parleurs. Reprendre Stable, le retour à 75 %, la sortie analogique et la protection anti-écho active. Mettre les autres applications audio en pause. Dire un seul « TAC ! » après chaque invitation, puis faire silence.

**Retour Kara coupé**

```bash
python3 bin/_kara_pipewire_latency.py --mode control > pipewire-control.json
```

**Retour Kara activé**, après `pipewire_control_ready`

```bash
python3 bin/_kara_pipewire_latency.py --mode diagnostic --control pipewire-control.json > pipewire-diagnostic.json
```

Les résultats apparaissent dans le terminal en fin de passe. Le JSON indique les niveaux par canal, les événements, les candidats, les plages de sensibilité et les motifs de rejet. Si le sink ne porte aucun lien de lecture actif, les niveaux du monitor sont `null` et le contrôle peut établir l’absence de route de restitution. Si un lien reste actif mais transporte du silence, les niveaux observés sont disponibles. Un lien inactif laissé par une application en pause n’entre pas dans l’attribution. Un monitor qui contient un signal mais reçoit plusieurs sources actives rend l’attribution à Kara indéterminée. Les deux passes gardent le PCM en mémoire et écrivent uniquement le rapport demandé.

Après `pipewire_control_ready` et `pipewire_diagnostic_ready`, dix nouveaux essais permettent une synthèse par observable. Le diagnostic exige trois appariements directs–monitor cohérents. Le mode complet exige au moins huit appariements sur dix, une MAD d’au plus 8 ms et une étendue d’au plus 24 ms. L’intervalle acoustique possède ses propres critères de support et de dispersion. Il peut rester `null` alors que le trajet jusqu’au monitor est établi.

```bash
python3 bin/_kara_pipewire_latency.py --mode measure --control pipewire-control.json \
  --diagnostic pipewire-diagnostic.json
```

### Horloge, points de mesure et limites

Le helper C crée un `pw_filter` synchrone avec trois ports DSP F32. Une fois les ports exacts liés et le format vérifié, il ouvre les abonnements aux nœuds pour lire l’affectation actuelle du pilote. Si le pilote élu omet son propre `node.driver-id`, l’autre nœud doit pointer explicitement vers lui. Le filtre exige le même identifiant dans `SPA_IO_Position` à chaque cycle publié. Chaque paquet associe la position de début du cycle à ses buffers et à sa durée. Le décalage de grille entre les buffers présents dans un même cycle est de zéro frame **aux points d’observation du filtre**. Les liens asynchrones ou de feedback, un pilote qui change, une rupture de position, un xrun détectable, un buffer incomplet et un débordement annulent la campagne. Le banc accepte une grille à 48 kHz sans en imposer une au serveur. Les timestamps de livraison du tube et les invitations servent au déroulement des essais. Les écarts sont calculés à partir des positions des échantillons. [Position de cycle SPA](https://docs.pipewire.org/structspa__io__position.html) · [Ordonnancement du graphe](https://docs.pipewire.org/page_scheduling.html).

| Champ | Ce qui est observé |
| --- | --- |
| `graph_interval` | Écart entre la syllabe directe au **port micro** et la syllabe appariée au **port monitor** du sink analogique. Le monitor est comparé sur ses deux canaux. L’attribution à Kara exige un appariement unique et des liens actifs provenant du seul lecteur Kara sur la sortie surveillée. |
| `monitor_to_witness_graph` | Écart du **monitor** jusqu’à la restitution recaptée au **port micro**, si le PCM du monitor permet d’identifier une copie retardée. Ce trajet comprend la sortie, la propagation, le micro et son retour dans le graphe. |
| `physical_output_delay` | Délai du monitor jusqu’à l’émission physique du haut-parleur. Le banc le laisse à `null`. Une calibration combinant les horloges et les délais matériels du DAC et de l’ADC serait nécessaire pour l’isoler. |
| `acoustic_interval` | Intervalle direct–retour identifié sur **le même canal du micro**, avec le monitor comme référence pour retrouver la copie. Il inclut la différence des trajets acoustiques jusqu’au micro. |

Les buffers et le timing de la carte peuvent décaler les échantillons visibles au port micro par rapport à leur acquisition, et ceux du monitor par rapport à l’entrée du sink ou à l’émission du haut-parleur. `spa_io_clock.delay` concerne le pilote courant et `SPA_PARAM_Latency` décrit les latences annoncées par le graphe. Ces informations ne fournissent pas à elles seules une calibration croisée pour le Vivobook. Le rapport indique `hardware_alignment_bound_ms: null` et `sink_input_to_monitor_bound_ms: null`. Un défaut matériel silencieux qui n’est signalé ni dans les métadonnées ni dans la continuité des cycles peut rester invisible. [Horloge SPA](https://docs.pipewire.org/structspa__io__clock.html) · [Latence annoncée par PipeWire](https://docs.pipewire.org/devel/page_latency.html).

L’appariement combine la corrélation de plusieurs portions de syllabe, leur enveloppe et leur évolution spectrale. Pour le trajet acoustique, le banc compare la référence numérique avec des portions du micro au-delà de la fin plausible du direct, à partir de la voix observée et de la plus longue traîne du contrôle. Un retour très faible, un chevauchement trop long, une réflexion qui varie entre les passes ou une ressemblance périodique peuvent laisser l’intervalle acoustique `inconclusive`. Les plages couvrent la sensibilité de l’alignement entre repères et une marge de 4 ms par comparaison. L’intervalle acoustique propage les deux marges. La grille interne de 0,0625 ms est une résolution de calcul et la précision sur le Vivobook reste à mesurer.

### Validation sur ArchASP

Sur PipeWire 1.6.11 et WirePlumber 0.5.19, un sink déjà en lecture publiait initialement son propre identifiant, 37. Les liens du filtre ont fait élire le micro ALSA, 36. Le serveur et `SPA_IO_Position` reflétaient ce changement, tandis que le proxy déjà ouvert sur le sink gardait 37. Les liens pouvaient aussi livrer leur format avec un événement d’état seul. Le helper vérifie désormais le format porté par le lien actif et ouvre ses abonnements après la jonction du graphe. Les trois liens de capture observés étaient actifs, DSP F32 et synchrones. [Affectation du pilote dans PipeWire 1.6.11](https://github.com/PipeWire/pipewire/blob/1.6.11/src/pipewire/impl-node.c#L1039-L1099) · [Publication du format](https://github.com/PipeWire/pipewire/blob/1.6.11/src/pipewire/impl-link.c#L590-L598).

Avec le micro et le sink analogiques initialement suspendus, le prévol a vérifié **94 cycles de 2 048 frames à 48 kHz**, soit 192 512 frames par canal avec les trois plans présents, des positions contiguës, et un compteur de xrun de zéro. Les deux PCM ALSA étaient en cours d’exécution pendant la capture. Les liens et les nœuds temporaires ont disparu à la fermeture et les deux nœuds ALSA sont revenus à l’état suspendu. La commande `control` a également atteint sa première invitation depuis cet état, sans ouvrir de lecteur. Les mesures acoustiques requièrent encore les essais vocaux de la campagne.

### Validation sur un graphe isolé

`python3 tests/test-kara-pipewire-latency.py` lance un serveur PipeWire privé avec un vrai monitor de sink virtuel, un générateur de syllabes en mémoire et le helper natif. Le stimulus introduit 174,25 ms entre le direct et l’entrée du sink, puis un retour simulé à 231,75 ms dans le témoin. Le sink pilote du test rend son monitor disponible au cycle suivant. À 256 puis à 512 frames, les mesures ont donné environ **179,6 puis 184,9 ms au monitor** et **52,2 puis 46,8 ms du monitor à la recapture**. L’intervalle sur le micro est resté proche de **231,75 ms**. Cet écart d’un cycle a été observé sur le sink du test et aucune correction de ce type n’est appliquée au Vivobook.

La suite V5 exécute notamment trois contrôles, trois diagnostics et dix mesures sur le serveur privé. Elle vérifie le PCM des trois canaux lorsqu’un sink déjà en lecture rejoint un autre pilote à la connexion du témoin. Elle couvre aussi un démarrage de capture décalé, une mise en attente du lecteur de paquets, un retour faible et déformé, une réflexion, un monitor inactif, un trajet numérique avec absence de retour acoustique, des liens non synchrones et des pertes de trames. Les essais physiques avec le S21 FE et le Vivobook utilisent les deux commandes de diagnostic ci-dessus.

## Mesure acoustique par syllabe « TAC ! »

Depuis la racine du dépôt, garder le S21 FE, la bouche, le micro témoin et les haut-parleurs aux mêmes positions entre les passes. Pour le premier diagnostic sur le Vivobook, reprendre le profil Stable, la sortie analogique, le retour à 75 % et la protection anti-écho activée. Laisser Strawberry en pause. À chaque invitation, dire **un seul « TAC ! » bref**, puis garder le silence jusqu’à la suivante. Les deux premières passes demandent trois syllabes chacune, sans nouvelle tentative automatique.

1. **Retour Kara coupé**, faire le contrôle vocal. Le statut `voice_control_ready` indique que les trois directs ont pu être examinés. Des candidats de réflexion plausibles peuvent figurer dans le contrôle.

   ```bash
   python3 bin/_kara_voice_latency_witness.py --mode voice --voice-control > voice-control.json
   ```

2. **Retour Kara activé**, lancer le diagnostic vocal. Les résultats par essai apparaissent dans le terminal à la fin de la passe. Le statut `voice_diagnostic_ready` indique trois candidats uniques et cohérents après comparaison avec le contrôle. Si le contrôle s’est terminé avec `inconclusive`, le diagnostic affiche les candidats et garde ce statut.

   ```bash
   python3 bin/_kara_voice_latency_witness.py --mode voice --voice-reference voice-control.json > voice-diagnostic.json
   ```

Les invitations et les candidats restent visibles sur stderr avec la redirection. Les fichiers JSON contiennent les résultats numériques, dont le seuil, la source, les délais candidats, les scores et les plages de sensibilité. Le PCM reste en mémoire. Chaque passe ouvre un seul `parec`, mono S16LE à 48 kHz, avec les mêmes contrôles de cadence et de fermeture que la mesure par claquement. L’analyse en mémoire commence après la fermeture de la capture. Avec un autre micro physique, passer le même `--source NOM` aux trois commandes.

### Analyse et publication

La première arrivée soutenue, vérifiée au seuil nominal et à ±3 dB, sert de repère du direct. Le détecteur cherche ensuite de **35 à 1 100 ms** sur la même suite d’échantillons. Il compare plusieurs portions de la syllabe par corrélation normalisée après filtrage et rééchantillonnage à 16 kHz. Il vérifie aussi l’évolution de l’enveloppe et de l’énergie dans six bandes de 125 Hz à 7 kHz, sur des fenêtres de 16 ms espacées de 4 ms. Pour un retour qui commence pendant le direct, un modèle à copie retardée et atténuée propose une fin du direct. La décision exige encore une portion retardée mesurable dans le PCM observé après cette fin estimée et plusieurs repères concordants. [Knapp et Carter, estimation de retard par corrélation](https://www.ee.iitb.ac.in/course/~sachinnayak/finalpaper2.pdf).

La confiance affichée est qualitative. Un alignement concurrent, un son supplémentaire ou une ressemblance d’enveloppe et de spectre sans corrélation suffisante peuvent laisser des candidats de confiance modérée. Une plage candidate qui recoupe une plage plausible du contrôle, avec une marge de 12 ms, reste compatible avec une réflexion. Le diagnostic exige un seul candidat plausible hors de ces plages, de confiance élevée. Les trois délais soutenus doivent avoir une MAD d’au plus 8 ms et une étendue d’au plus 24 ms. Si un essai manque de preuve ou si la passe ne satisfait pas ces critères, elle affiche `inconclusive`. Refaire les deux diagnostics après un changement de géométrie, de micro, de gain ou de sortie.

Après les deux diagnostics validés, la mesure complète utilise les deux JSON issus des passes précédentes et dix nouvelles syllabes.

```bash
python3 bin/_kara_voice_latency_witness.py --mode voice --voice-reference voice-control.json \
  --voice-diagnostic voice-diagnostic.json --trials 10
```

`voice_witness_intervals` publie la médiane, la MAD, le min–max et la plage de sensibilité médiane si au moins huit essais sur dix sont soutenus, avec les mêmes critères de dispersion que le diagnostic. Une rupture de capture invalide la campagne entière. Le diagnostic est lié au contrôle exact qui a servi à l’évaluer. Les passes conservent la même source et le même seuil. Une variation du p95 de fond de plus de 6 dB après application d’un plancher de comparaison à −70 dBFS arrête la passe avant les invitations.

### Précision et limites

Le pas de corrélation de 0,0625 ms est celui de la grille numérique. Pour un délai soutenu, la plage par essai couvre les variations d’alignement entre les portions comparées et ajoute 4 ms de part et d’autre. Les candidats repérés seulement par l’enveloppe et le spectre affichent une marge de diagnostic de ±24 ms. Ces plages décrivent la sensibilité au modèle et aux repères retenus. La MAD mesure la dispersion entre essais. Les biais du traitement Android, du haut-parleur, des réflexions et des trajets acoustiques peuvent dépasser ces plages. À géométrie fixe, le délai commun de livraison du PCM témoin s’annule. Un placement différent change la mesure suivant la correction de distance décrite ci-dessous.

Avec un seul micro, un retour très atténué, presque entièrement recouvert, fortement modifié ou proche d’un délai de réflexion peut rester indéterminé. Les copies à moins de 35 ms font partie du signal entendu autour du direct. Un changement de réflexion entre les deux passes peut encore imiter une restitution. Les trois essais de contrôle échantillonnent la pièce dans les conditions de ces passes. Le rapport conserve les candidats utiles pour examiner ces cas et laisse la médiane à `null` en cas d’ambiguïté. La précision sur le S21 FE et le Vivobook reste à vérifier avec les diagnostics réels.

## Mesure acoustique par claquement sur une seule horloge

Le **S21 FE** capte le claquement et envoie le retour à Kara. Le **micro intégré du Vivobook** entend à la fois le claquement direct et le retour des haut-parleurs du PC. Placer le téléphone à portée des mains, le Vivobook orienté vers la zone de claquement et le micro dégagé. Choisir un endroit où le direct et le haut-parleur sont tous deux audibles, à distance des surfaces très réfléchissantes. Garder la position des mains, du téléphone, du témoin et du haut-parleur fixe entre les passes. Un autre micro physique peut être sélectionné avec `--source NOM`. La liste des sources est disponible via `pactl list short sources`.

Depuis la racine du dépôt sous ArchASP, lancer le contrôle avec le retour Kara coupé.

```bash
python3 bin/_kara_voice_latency_witness.py --mode control
```

Une fois le contrôle validé, activer manuellement le retour Kara sur la sortie à mesurer, laisser Strawberry en pause et lancer la mesure.

```bash
python3 bin/_kara_voice_latency_witness.py --mode return
```

### Déroulement

- Chaque commande lance un seul `parec` via l’interface PulseAudio de PipeWire. La source par défaut est `alsa_input.pci-0000_63_00.6.analog-stereo`. Si la source choisie manque, l’outil arrête la campagne. Le flux est mono S16LE à 48 kHz. Le script garde les fenêtres PCM en mémoire pendant le traitement et affiche les invitations sur stderr et les résultats JSON sur stdout. [Manuel de parec](https://man.archlinux.org/man/pacat.1)
- Garder le silence pendant les deux secondes de stabilisation et la seconde de mesure du fond. Le seuil automatique retient le plus haut du RMS global et du p95 des bins RMS de 2 ms, puis ajoute 10 dB et arrondit au dB supérieur. Il est borné entre −44 et −22 dBFS. Le programme indique le seuil, le RMS et le p95, et arrête la passe si le fond est trop élevé ou instable. Pour des comparaisons à seuil constant, reprendre la valeur du contrôle avec `--threshold-dbfs VALEUR` et l’utiliser pour chaque retour. La marge de bruit reste exigée avec un seuil fixe.
- À chaque préparation, garder le silence. Faire **un seul claquement bref** après « CLAQUE MAINTENANT ! », puis attendre le diagnostic et l’invitation suivante. Le programme capture au moins 1,35 s de préparation et une fenêtre de 3,7 s par invitation. Il contrôle la cadence de réception et rattache l’invitation à un curseur PCM, après avoir absorbé dans la préparation les échantillons déjà lisibles. La recherche du direct commence après une garde de 250 ms et se termine à 2,2 s sur cette fenêtre PCM. Les invitations sont espacées d’environ cinq secondes en fonctionnement normal.
- En contrôle, viser **cinq claquements acceptés**, chacun avec un seul événement. Le statut `single_events` valide cette vérification. En retour, viser **dix paires**, chacune avec un direct suivi d’un retour entre 35 et 1 100 ms. Prévoir environ 30 s pour le contrôle et 55 s pour le retour. Par défaut, l’outil propose au maximum deux tentatives supplémentaires en fin de passe. Tous les rejets restent visibles dans le JSON et comptent dans le taux d’acceptation. `--retries 0` permet une passe sans tentative supplémentaire.
- Chaque essai affiche le pic capté et sa décision. En cas de claquement faible, rapprocher les mains du témoin en gardant une géométrie reproductible. Près du rail PCM, claquer plus doucement ou plus loin, ou réduire manuellement le gain d’entrée avant de refaire le contrôle. Un écho possible, des sons multiples, un événement hors fenêtre ou une décision sensible au seuil rendent l’essai inutilisable. Si une modification de placement, de volume ou de gain est nécessaire, refaire le contrôle avant de comparer les retours.
- **Ctrl+C** arrête la capture et termine avec le code 130 sans publier de résultat de campagne. Si le retour Kara boucle, couper le retour manuellement. Une fin prématurée, un retard marqué ou une rafale de PCM invalide toute la campagne, même si des essais antérieurs étaient acceptés.

### Interprétation et incertitudes

Le mode `return` publie `witness_intervals` à partir d’au moins huit paires et d’au moins 80 % de tentatives acceptées. Tout autre statut laisse la latence de la campagne indéterminée. Lire `median_witness_ms` avec la MAD, le min–max et `median_threshold_and_bin_range_ms`. Les transitoires sont repérés par énergie en bins de 2 ms. La paire doit rester séparable au seuil nominal et à ±3 dB. La plage publiée rend compte de ces variations et du binning. Le délai des deux événements est calculé sur les positions PCM du même flux. Le délai de livraison du témoin et les tampons de l’affichage influencent le guidage, alors qu’un délai commun aux deux événements s’annule dans la différence de leurs positions.

La cadence vérifiée est celle des octets reçus. Elle permet de rejeter les rafales et les retards observables, avec une tolérance de 150 ms sur la cadence et de 200 ms sur l’absence de données après démarrage. Le PCM brut ne porte ni les timestamps de l’ADC ni les indicateurs d’erreur du graphe. Une mise en tampon constante décale la fenêtre PCM par rapport à l’invitation. Surveiller les erreurs du témoin et de son pilote dans `pw-top` et écarter toute passe avec une discontinuité. L’ouverture du témoin peut aussi modifier le graphe, relever le quantum du pilote pour comparer des passes avec le même témoin actif. [PipeWire pw-top](https://docs.pipewire.org/page_man_pw-top_1.html)

L’intervalle est conditionnel à l’attribution correcte des deux transitoires. Un retour mélangé au direct, une réflexion isolée qui imite le retour ou une saturation en amont des rails PCM peuvent échapper au détecteur. La plage seuil/bin ne borne pas ces effets ni le biais géométrique. Pour estimer le délai entre le son arrivant au téléphone et l’émission du haut-parleur, soustraire à l’intervalle brut `(distance claquement–S21 FE + distance haut-parleur–témoin − distance claquement–témoin) / vitesse du son`. À température ambiante, prendre environ 343 m/s. Une erreur de 0,5 m sur ce terme représente environ 1,5 ms. Si les distances sont mal connues, comparer les intervalles bruts à géométrie fixe sur la même sortie.

### Comparaison des profils sur ArchASP

Tester **Stable → Équilibré → Faible latence → Stable**, en passant à Faible latence si les premiers essais sont stables. Relever séparément le résumé acoustique, le JSON des délais internes, la latence perçue et la stabilité sonore. Terminer par parole et chant une fois le témoin arrêté. Une session d’environ 75 s par profil permet normalement de réunir cent RTT. Si l’EDENWOOD est utilisé, vérifier le routage et le codec A2DP, puis refaire le contrôle et la séquence de profils sur cette sortie. Les erreurs du lecteur Kara et la calibration chirp/PN sont des relevés séparés.

Désigner un maillon prioritaire seulement si une attente ou une hausse se répète et concorde avec l’écart acoustique. En cas d’ambiguïté, garder les délais internes et les observations de stabilité et laisser le total indéterminé.

## Reproduction logicielle

```bash
node --check bin/_kara_micro_worklet.js
node tests/test-kara-audio-monitor.js
node tests/test-kara-calibration-capture.js
node tests/test-kara-calibration-engine.js
(for test in tests/test-kara-*.py; do python3 "$test" || exit; done)
git diff --check
```

Les tests vérifient les origines d’horloge distinctes, le décalage fixe, une dérive imposée, des rafales, la croissance progressive d’attente, les reconnexions, les changements de profil et les saturations de file. Le détecteur a accepté dix intervalles synthétiques avec gigue, médiane **222 ms**, MAD **2 ms** et écart maximal **1,4 ms** par rapport aux intervalles injectés. Les tests synthétiques couvrent aussi la stabilisation, le bruit de fond, cinq invitations espacées, les niveaux faibles et saturés, les événements hors fenêtre, les réflexions possibles, les rafales de PCM, le flux tronqué et la fermeture du processus, dont Ctrl+C. Le résultat synthétique ne borne pas l’erreur sur matériel.

Les tests V4 dans `tests/test-kara-voice-witness.py` passent des syllabes synthétiques de 100 à 300 ms par le détecteur et par les trois commandes. Ils couvrent notamment un retour atténué, filtré et comprimé, le chevauchement, un contrôle avec réflexion, une voix secondaire sans lien, deux retards concurrents et une tonalité isolée. Un balayage complémentaire a combiné quatre voix synthétiques, quatre durées, dix délais de 35 à 1 100 ms et deux traitements au même gain de retour (0,17). Il a soutenu **308/320** délais, dont **174/184** en chevauchement. L’écart maximal observé sur les délais soutenus était de **0,05 ms**. La plage de sensibilité contenait le délai injecté dans les 308 cas. Aucun des **100** signaux synthétiques sans retour de ce balayage n’a été soutenu. Ces résultats décrivent les signaux construits pour la validation. Ils ne mesurent ni la précision ni le taux de faux positifs sur le Vivobook.

Sur ArchASP, le microbanc Python isolé mesure environ **0,82 µs CPU par bloc** pour les opérations de collecte et **0,87 ms pour une synthèse pleine** avec lecture `FIONREAD`. Dans Node.js, huit séries JavaScript prennent environ **0,19 µs par bloc** en collecte et **1,54 ms** pour leur synthèse pleine. Le Worklet simulé est passé de 54,02 à 54,10 µs CPU par quantum dans ce banc. L’IPC Chrome et le CPU du S21 FE ne sont pas reproduits par ces microbancs. Les sondes n’ajoutent aucune attente programmée au flux. Le microbanc et les tests se sont déroulés sans ouverture de micro ou émission acoustique.
