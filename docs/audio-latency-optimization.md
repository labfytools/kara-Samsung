# Latence du retour microphone

## Chaîne et audit

Chrome sur le S21 FE capte le premier canal via `getUserMedia` puis AudioWorklet. Le Worklet constitue des blocs mono int16 et en transfère la mémoire au JavaScript principal. Le navigateur ajoute un en-tête de 16 octets et envoie les blocs par WebSocket HTTPS. Sur ArchASP, aiohttp alimente une file FIFO bornée. NumPy mesure les niveaux et applique la compression douce de la voix. SciPy rééchantillonne les blocs de 44,1 kHz à 48 kHz si nécessaire. Le serveur duplique le signal sur deux canaux et le remet à `pw-cat` en Stable ou à `pacat` pour les variantes. Les deux entrent dans le graphe PipeWire, vers la sortie analogique ou Bluetooth.

| Poste | Ce qu’on sait et ce qui reste à mesurer |
| --- | --- |
| Capture Android | Kara demande l’annulation d’écho et la réduction du bruit, avec gain automatique désactivé. Le diagnostic expose les réglages que la piste déclare. Une éventuelle valeur `latency` de la piste est exprimée en secondes. `AudioContext.baseLatency` décrit le chemin de sortie local du contexte, ici muet. Le délai réel de capture du téléphone reste inconnu. |
| Worklet et thread principal | La constitution d’un bloc attend jusqu’à sa durée selon la position de l’échantillon dans le bloc. Diviser 20 ms par deux réduit cette composante jusqu’à environ 10 ms, soit environ 5 ms en moyenne pour des instants uniformément répartis en phase. Le quantificateur int16 et le transfert du tampon sont identiques. Les observations fournies donnent 0–3 ms au p95 entre fin de bloc et JS en régime courant. Un blocage du thread principal peut néanmoins libérer plusieurs messages d’un coup. |
| WebSocket et Wi-Fi | À 48 kHz, les messages utiles font 1 936 octets à 50/s ou 976 octets à 100/s, hors trames, TCP et TLS. La charge PCM reste de 96 kB/s, avec davantage d’en-têtes et de réveils à 10 ms. TCP livre les messages dans l’ordre. Une retransmission peut retarder les suivants ensemble. Les séquences manquantes indiquent des blocs audio absents et ne comptent pas les retransmissions. Le RTT historique de 12–20 ms et le retard relatif des arrivées ne séparent pas Wi-Fi, TLS et traitement aux extrémités. |
| Serveur | Le coût DSP et le temps d’écriture stdin sont mesurés séparément. La charge Android, les pointes de CPU et le coût du chemin 44,1 kHz en conditions réelles restent à vérifier. Sur les variantes à 44,1 kHz, le même filtre polyphasé conserve son historique aux frontières et décale le signal de 12 échantillons à 48 kHz, soit 0,25 ms. Le calcul de percentiles est fait à la demande de diagnostic, hors du traitement par bloc. |
| Tube et PipeWire | Le tube Linux de référence a une capacité de 65 536 octets sur ArchASP, soit 341 ms de PCM stéréo 48 kHz s’il était plein. Un tampon asyncio et la lecture stdio de `pw-cat` peuvent s’y ajouter. La capacité n’indique pas le remplissage. Les profils expérimentaux mesurés utilisent un tube de 4 096 octets, soit 21,3 ms de PCM au maximum dans ce tube, et suspendent l’écriture suivante tant que la précédente attend côté asyncio. |
| Sortie analogique | La file du graphe, les périodes du pilote, les conversions éventuelles et le matériel restent en aval. Les paramètres des lecteurs sont des demandes. `stdin.drain()` indique que le transport Python accepte la suite des données. Le rapport du pilote `QUANT / RATE` donne une période. Le graphe analogique a tourné à 256 frames sur 48 kHz pendant les tests, soit une période de 5,33 ms. Aucun de ces observables n’est un délai acoustique. |
| EDENWOOD | ArchASP a identifié `a2dp-sink` avec le codec SBC. Une lecture de `MediaTransport1.Delay` a donné 30 unités de 0,1 ms, soit 3 ms déclarées pour ce transport actif. Cette propriété facultative laisse inconnus le retard acoustique et la profondeur du tampon interne de l’enceinte. La préparation SBC, le transport et le tampon de restitution A2DP restent en aval des profils. |

Références techniques. [Web Audio](https://www.w3.org/TR/webaudio-1.0/#dom-audiocontext-baselatency), [Media Capture and Streams](https://w3c.github.io/mediacapture-main/#dom-mediatracksettings-latency), [WebSocket bufferedAmount](https://websockets.spec.whatwg.org/#dom-websocket-bufferedamount), [lecture stdin dans pw-cat 1.6.11](https://github.com/PipeWire/pipewire/blob/1.6.11/src/tools/pw-cat.c#L1573), [lecture à la demande dans pacat](https://github.com/pulseaudio/pulseaudio/blob/v17.0/src/utils/pacat.c#L485), [pw-top](https://docs.pipewire.org/page_man_pw-top_1.html), [BlueZ MediaTransport1](https://bluez.readthedocs.io/en/latest/media-api/).

La campagne fournie donne 216,6 ms sur PC et 364,7 ms sur EDENWOOD en PN, soit un écart indicatif de 148,1 ms entre les deux chaînes. Les MAD sont de 2,7 et 5,2 ms, les deux séries PN ont 6/6 essais acceptés. Les chirps donnent respectivement 235,5 et 358,0 ms, avec 6/6 puis 4/6 essais acceptés. Deux essais Bluetooth ont saturé. Le trajet de cette mesure indépendante part de la commande de sonde et se termine dans le micro du téléphone. Il inclut la sortie, la propagation et la capture. La différence entre sorties ne fournit pas le délai propre au codec SBC.

## Profils du retour

| Profil | Bloc WebSocket | File Arch maximale | Rejet avant envoi | Tube et asyncio | Lecteur et paramètres demandés |
| --- | ---: | ---: | --- | --- | ---: |
| Stable | 20 ms | 4 × 20 ms = 80 ms | Seuil de référence `bufferedAmount < 4000` avant envoi | Valeurs par défaut | `pw-cat` natif, 10 ms |
| Équilibré | 10 ms | 4 × 10 ms = 40 ms | Rejet après 40 ms d’âge Worklet ou si l’envoi porterait `bufferedAmount` au-delà de quatre messages | Une page Linux, seuils asyncio à zéro | `pacat` via PipeWire Pulse, latence demandée 20 ms et traitement demandé 5 ms |
| Faible latence | 10 ms | 2 × 10 ms = 20 ms | Rejet après 20 ms d’âge Worklet ou si l’envoi porterait `bufferedAmount` au-delà de deux messages | Une page Linux, seuils asyncio à zéro | `pacat` via PipeWire Pulse, latence demandée 20 ms et traitement demandé 5 ms |

La file Python n’impose aucun préremplissage. Lorsqu’elle est pleine, le serveur écarte le plus ancien bloc encore en attente et garde l’ordre des blocs restants. Le paquet déjà remis au lecteur suit le chemin de lecture normal. Le serveur ignore les doublons et les retours en arrière de séquence. Le navigateur peut écarter un bloc avant qu’il n’entre dans TCP. Il ne peut pas retirer le PCM déjà envoyé. La réduction des capacités limite l’accumulation si une sortie prend du retard, au prix de moins de marge en cas de pause ou de rafale. La limite du tube exclut les tampons du lecteur, de PipeWire et de la sortie.

Pendant les essais à cadence régulière, le lecteur natif a encore accumulé des erreurs de nœud après l’amorçage avec des blocs de 20 ms et de 10 ms. Le chemin raw de `pw-cat` effectue une lecture bloquante depuis stdin dans son callback de traitement. `pacat` lit à la demande, en coordination avec le tampon du serveur PipeWire Pulse déjà installé. Les demandes de 15 et 20 ms ont produit zéro sous-alimentation à cadence régulière dans l’essai court. Les deux variantes utilisent 20 ms pour garder une marge commune côté sortie. Le contrôle de volume cible le nom unique du nœud du retour. Le choix du lecteur est local au flux Kara. Les variantes nécessitent `pacat` et un serveur PipeWire Pulse actif. Si le lecteur expérimental est indisponible, revenir à Stable.

**Revenir à Stable** coupe immédiatement le retour et sélectionne le profil de référence pour le prochain essai sur la sortie courante. Appuyer sur **Activer le micro** pour repartir avec une session neuve. Le micro se coupe aussi si sa piste se termine, si sa connexion est perdue ou si la page passe en arrière-plan.

### Réglages par sortie

Kara charge les réglages de la sortie sélectionnée à l’ouverture de la page, au changement de sortie et avant une nouvelle activation si le périphérique a changé. L’identité ALSA associe le nœud et le port actif. Les haut-parleurs et la prise jack peuvent partager le même nœud. Une enceinte Bluetooth est identifiée par son adresse, indépendamment du suffixe de session et du codec A2DP. Le flux est ouvert sur la sortie vérifiée. Si elle a changé entre la lecture du profil et l’ouverture du flux, l’activation est rejetée. Pour changer de sortie pendant le retour, couper le micro puis le réactiver avec la nouvelle sortie.

Une sortie sans profil utilise Équilibré, retour à 50 %, voix à 55 %, protections actives et tampon standard. Stable sert de repli si `pacat` n’est pas installé. Les changements pour une écoute restent temporaires. Sur ArchASP, le profil confirmé des haut-parleurs du Vivobook utilise Faible latence, retour à 60 %, voix à 55 %, protection Kara, annulation et réduction de bruit Chrome actives et tampon Pulse standard. Deux essais acoustiques V5.1 ont donné 137,1 et 152,6 ms. Le troisième était ambigu. La série quantitative est partielle, avec une validation à l’écoute pendant le chant accompagné.

L’enregistrement définitif passe par la commande locale `python3 bin/_kara_monitor_profiles.py save` et l’option `--help` donne les réglages. Utiliser `--experimental` pour mémoriser un essai qui reste à valider. Pour revenir au repli avant une recalibration, la commande `reset` prend le même nom de nœud et, en ALSA, son port. Par exemple, la réinitialisation des seuls haut-parleurs du Vivobook s’écrit ainsi.

```bash
python3 bin/_kara_monitor_profiles.py reset alsa_output.pci-0000_63_00.6.analog-stereo --port analog-output-speaker
```

Le fichier utilisateur est `~/.local/state/kara/micro-test/monitor-profiles.json`. Il contient des réglages uniquement. La calibration et les mesures ci-dessous restent disponibles après réinitialisation.

### Lire les relevés

- **Ignorés téléphone** compte les blocs que le navigateur a écartés. **Séquences manquantes** compte les trous effectivement observés à la réception, souvent les mêmes blocs. Un rejet en fin de session sans nouveau paquet reçu peut rester visible côté téléphone uniquement.
- **Écartés Arch** compte les blocs valides retirés de la file avant écriture. Les doublons, messages mal formés et retards relatifs sont visibles en JSON.
- **Périmés après un trou réseau** distingue les trames reçues mais laissées hors du retour. Pour les profils à file bornée, si la réception prend au moins 250 ms de plus que l’intervalle entre deux envois successifs, le serveur écarte le retard accumulé jusqu’à retrouver le budget de file du profil. Il compare des durées locales observées autour du trou. Après 2 s de reprise sans rattrapage, la session s’arrête et peut être réactivée.
- **Attentes d’alimentation** compte les périodes où la file est vide et où le lecteur a attendu plus de 25 ms en Stable ou 15 ms à 10 ms, après la première écriture. La durée cumulée compte l’excédent au-delà du seuil. Un tampon aval peut absorber ces périodes. Dans `pw-top`, les incréments de `ERR` signalent des xruns ou erreurs du graphe ou du nœud. Rapporter ces deux compteurs séparément.
- **Sous-alimentations Pulse** compte les notifications du lecteur expérimental émises par son flux Pulse. Le nombre peut inclure plusieurs notifications pour une seule pause. Stable ne fournit pas ce compteur. Le JSON rapporte aussi les attributs de tampon annoncés à l’établissement de la connexion. Par exemple, `tlength=2944` correspond à une cible de 15,33 ms de PCM stéréo à 48 kHz sur le test analogique. Il s’agit d’un attribut de tampon, distinct de son niveau instantané, de la période du graphe et du délai acoustique.
- **Comparaison des sessions** garde en mémoire le dernier relevé par sortie, profil et état de la protection jusqu’au rechargement. Le JSON indique les paramètres techniques, la version du retour et les compteurs. La sortie « par défaut » doit être identifiée lors de l’essai. Les résumés ne contiennent ni PCM, ni identifiant de périphérique, ni jeton.
- **Calibration complète** continue à utiliser ses propres blocs de 20 ms, ses sondes et son analyse. Son récapitulatif est consultable séparément et permet de surveiller la répétabilité de la chaîne de restitution. Sa demande de sortie et sa campagne sont indépendantes du profil de retour sélectionné. [Contrat et limites de la calibration](audio-calibration.md).

Pour une séance de calibration pilotée depuis Arch, démarrer le serveur Kara avec `KARA_LOCAL_DIAGNOSTICS=1`, puis recharger la page du téléphone. Le serveur garde en mémoire le dernier relevé chiffré de la session. `GET /monitor-diagnostics` exige le jeton habituel et une connexion émise depuis l’adresse locale d’écoute du serveur. Le relevé inclut les réglages de piste retournés par Chrome lorsqu’ils sont disponibles. Le champ `latency` de la piste et le délai de base de l’AudioContext sont des valeurs déclarées par Chrome. La mesure de l’ADC au Worklet nécessite une référence indépendante.

**Tampon Pulse court**, dans les détails techniques du retour, demande 15 ms au lieu de 20 ms pour Équilibré et Faible latence. La période de traitement demandée reste de 5 ms. Sur l’EDENWOOD en SBC stéréo à 48 kHz, les deux demandes ont donné des cibles Pulse de 2 464 et 2 944 octets, soit 12,83 et 15,33 ms. Deux relectures d’une cadence irrégulière ont chacune donné deux sous-alimentations après amorçage aux deux réglages. Le chemin WebSocket réel a ouvert le lecteur sur l’EDENWOOD aux deux réglages avec du PCM silencieux et zéro rejet de file à cadence régulière. Des demandes de 8 et 10 ms ont toutes deux donné une cible de 3 072 octets, soit 16 ms, avec deux sous-alimentations après amorçage dans chaque relecture irrégulière. Le réglage court reste optionnel. Les 2,50 ms observées caractérisent la cible du tampon.

Les détails techniques permettent de tester séparément l’annulation d’écho et la réduction de bruit de Chrome. Les deux restent demandées par défaut. Sur le S21 FE mesuré, Chrome a signalé une piste mono à 48 kHz et un gain automatique désactivé. La latence déclarée est passée de 10 à 40 ms lors de l’essai sans réduction de bruit, avec l’annulation Chrome active. L’écoute sur l’EDENWOOD est restée trop faible et les copies gênantes avec ce réglage. Le lecteur Pulse porte un nom d’application propre au retour Kara pour isoler la restauration du volume et de l’état muet.

Sur l’EDENWOOD, le diagnostic de retour vocal en Équilibré a retenu trois intervalles du graphe jusqu’au monitor, entre 158,88 et 158,94 ms. En Faible latence, à un autre niveau et avec le téléphone rapproché, un seul des trois essais a donné un intervalle exploitable de 126,25 ms, avec une plage de 122,25 à 130,25 ms. Les deux autres ont été rejetés à cause d’alignements concurrents. La configuration Faible latence avec retour à 75 %, voix à 30 % et enceinte temporairement à 65 % a donné un premier retour assez fort. Le confort de chant est resté insuffisant à l’écoute. Le profil et le placement ayant changé entre ces diagnostics, le gain propre au profil demeure indéterminé.

Sur l’association ArchASP–EDENWOOD, BlueZ expose un endpoint distant SBC. PipeWire propose les variantes A2DP SBC et SBC-XQ. Le S21 FE envoie le micro par Wi-Fi. Le paramètre `bluez5.decode-buffer.latency` concerne le décodage par PipeWire sur un nœud source Bluetooth. Les réglages BAP concernent LE Audio. Le tampon interne de l’enceinte A2DP reste inaccessible à Kara. Le délai déclaré par le nœud PipeWire est de 17,85 ms sur le relevé SBC, tandis que la médiane de recapture mesurée dans la première série est de 134,52 ms après le monitor. [Propriétés Bluetooth de PipeWire](https://pipewire.pages.freedesktop.org/pipewire/page_man_pipewire-props_7.html#bluetooth-properties) · [Terminaux et transports BlueZ](https://bluez.readthedocs.io/en/latest/media-api/).

Dans la comparaison courte à 50 % sur la même enceinte, le S21 FE était coupé. Le graphe était à 48 kHz, avec le même pilote et un quantum de 1 024 frames pour les deux réglages. SBC a donné 140,27, 143,15 et 145,44 ms du monitor au témoin. SBC-XQ a donné 190,19 ms sur le chirp, seul essai retenu sur trois. Le chirp équivalent en SBC valait 143,15 ms et les deux chirps ont produit le même niveau acoustique RMS à 0,01 dB près. Les configurations du transport BlueZ différaient, avec le même identifiant de codec SBC et la même valeur déclarée de 3 ms. Cet essai court n’a pas démontré de gain pour SBC-XQ. SBC et le volume de 35 % ont été rétablis.

### Retour Bluetooth en boucle réelle

Les trois TAC de contrôle, avec le retour coupé, ont donné 92–100 ms d’activité acoustique détectée. Entre 240 et 360 ms après l’attaque, le témoin était 27,7 à 29,5 dB sous la voix directe. Le monitor restait silencieux. Ce contrôle décrit la pièce et le bruit ambiant à ce placement.

En SBC, à 50 % sur l’EDENWOOD, Faible latence avec tampon standard, retour Kara à 65 %, voix à 55 % et protection Kara active, le passage avec les deux traitements Chrome a été rejeté par V5.1. Deux TAC du passage avec réduction de bruit seule avaient une première arrivée isolable au monitor à 122,38 et 140,38 ms. Une copie appariée apparaissait ensuite à 261,81 et 255,25 ms après le premier retour, avec un niveau RMS inférieur de 24,7 et 24,4 dB respectivement, sur des fenêtres de même longueur. L’appariement de ces deux événements sur le monitor ne valide pas la latence acoustique totale et le verdict de campagne V5.1 reste ambigu. L’utilisateur a identifié le retard du premier retour comme gêne dominante dans les deux passages.

Les essais de parole, note tenue et chant se sont terminés avec les deux réglages Chrome. Les positions et curseurs étaient identiques. Les maximums RMS de 4 ms au monitor étaient néanmoins 6 à 7 dB plus bas au second passage, où le niveau du témoin était aussi plus faible. Cette différence limite la comparaison de fidélité vocale à niveau égal. Avec les deux traitements, l’utilisateur a décrit une voix naturelle et un retard gênant.

Avec accompagnement Strawberry, les deux traitements Chrome actifs et le retour Kara à 65 %, l’utilisateur a jugé la voix trop faible face à la musique. Le niveau de Strawberry est resté identique. Passer le retour Kara à 75 % a porté la crête du flux de retour isolé de −23,88 à −20,36 dBFS pendant les deux écoutes de 15 s. La voix restait trop faible selon l’utilisateur. L’enceinte était à 50 % et le niveau voix à 55 % dans les deux cas. Le passage préparé avec l’enceinte à 25 % a été annulé avant activation. Le premier retour et la stabilité de boucle aux niveaux d’enceinte de 80 à 100 % restent à qualifier séparément.

Le S21 FE utilisait le Wi-Fi à 2,4 GHz et le Vivobook le même réseau en 5 GHz. Le RTT WebSocket était d’environ 19–21 ms en médiane sur les TAC, avec des p95 de 112–149 ms. Un ping du téléphone au Vivobook, retour coupé, a atteint 156,5 ms, avec un minimum de 6,9 ms et aucune perte sur 30 paquets. Le traitement côté serveur est sous 0,2 ms au p95. Les retards réseau sont variables et l’essai ne démontre pas un effet causal de la bande Wi-Fi. La différence de cible Pulse de 2,5 ms est à distinguer de ces pointes et du délai après le monitor Bluetooth.

Pour la comparaison AUX, brancher le Vivobook à l’entrée jack de la même enceinte en gardant l’enceinte, le S21 FE et le témoin aux mêmes positions. Le port ALSA doit indiquer la sortie jack. Conserver le micro Wi-Fi, son profil et ses traitements. Commencer par une sonde brève à faible niveau sur le lecteur, puis ajuster uniquement le gain du flux de test pour appairer le RMS acoustique au témoin à ±1,5 dB. Mesurer ensuite le trajet jusqu’au monitor et celui du monitor au témoin dans les deux configurations. L’identité ALSA par port préserve le réglage existant des haut-parleurs du Vivobook.

## Validation logicielle

Depuis la racine du dépôt, avec Python, NumPy, SciPy, aiohttp et Node.js.

```bash
node --check bin/_kara_micro_worklet.js
node --check bin/_kara_audio_calibration.js
node tests/test-kara-audio-monitor.js
node tests/test-kara-calibration-capture.js
node tests/test-kara-calibration-engine.js
(for test in tests/test-kara-*.py; do python3 "$test" || exit; done)
git diff --check
```

Les suites du retour exercent le Worklet et le client, le serveur aiohttp et son lecteur avec des périodes irrégulières, des paquets manquants ou mal formés, des doublons, et un blocage du consommateur pendant huit arrivées. Elles vérifient la taille des blocs aux deux fréquences, l’ordre des blocs conservés, la pression WebSocket, les capacités, l’arrêt et le retour Stable. Les tests de calibration couvrent séparément les références, le filtrage des commandes et du PCM, l’authentification, l’exclusivité, les bornes et le nettoyage.

Les essais sur ArchASP avec PipeWire 1.6.11 ont envoyé par le WebSocket local de production du PCM silencieux vers les vrais lecteurs et les deux sorties. Aucune séquence manquante, aucun rejet de file ni aucune attente prolongée côté source à cadence régulière. Le nœud Kara en Stable a accumulé des erreurs de graphe sur l’analogique, jusqu’à 22 sur le dernier relevé, et sur l’EDENWOOD, avec 38 au premier relevé actif. Les deux variantes sont restées à zéro sur les relevés `pw-top`, avec une notification Pulse au démarrage sur chaque sortie. À l’analogique, le DSP moyen mesuré était d’environ 0,14–0,16 ms et l’écriture stdin de 0,03 ms. Avec deux pauses imposées de 30 et 70 ms en Équilibré, le diagnostic analogique a retrouvé dix séquences manquantes, deux attentes prolongées et quatre notifications Pulse sur la session. Les erreurs de graphe et les sous-alimentations Pulse décrivent donc deux choses distinctes.

Le test de volume a contrôlé le seul nœud Kara et vérifié que l’index, la sortie, le volume et le mute du flux Strawberry sont restés identiques. Ces essais ne couvrent ni la liaison Wi-Fi du téléphone ni la mesure acoustique.

## Comparaison sur ArchASP et S21 FE

Garder le même réglage de protection contre les répétitions lors d’une comparaison de latence. [Mode de protection et essai dédié](audio-echo-control.md).

1. Lancer `python3 -u bin/_kara_micro_test.py` dans le terminal privé déjà configuré pour HTTPS. Sur le S21 FE, ouvrir le lien privé dans Chrome et garder la page au premier plan. Placer le téléphone et une source de sons brefs, par exemple une paire de claves ou deux objets en bois, à une distance fixe de la sortie. Commencer à faible volume. Choisir **Haut-parleurs ou prise casque analogique du PC** et vérifier la sortie utilisée. Garder Strawberry en pause durant les mesures et conserver les mêmes gains pour tous les profils d’une même sortie.
2. Retour coupé, lancer **Calibration complète**. Reporter séparément les médianes, MAD et nombres acceptés et retenus des deux méthodes. Garder la géométrie fixe et le volume des haut-parleurs modéré. En cas de saturation, ajuster le volume avant de recommencer la série et le conserver ensuite.
3. Ouvrir `pw-top` dans un autre terminal. Pour chaque profil dans l’ordre **Stable → Équilibré → Faible latence → Stable**, activer le retour et laisser passer cinq secondes. Noter alors les compteurs de départ. Dans les mêmes conditions, faire au moins dix sons brefs espacés d’environ deux secondes, puis chanter pendant la même durée, par exemple une minute. Pour les sons brefs, relever l’écart direct → retour avec le micro témoin ci-dessous. Pour le chant, arrêter le micro témoin et noter à l’écoute les coupures, craquements, variations de délai et le Larsen. Avant de couper le retour, relire les compteurs et relever les incréments de `ERR` du flux Kara et du pilote de sortie. Les valeurs de `QUANT` et `RATE` se lisent sur la ligne du pilote pendant que le flux tourne. En cas de Larsen, couper immédiatement. **Revenir à Stable** rend la référence disponible au prochain démarrage.
4. Retour coupé, refaire la campagne de calibration complète sur la même sortie. Comparer les statistiques par méthode à celles du début pour détecter une variation de la chaîne de restitution ou des conditions. Comparer entre profils la médiane et la MAD des écarts direct → retour dans le flux vivant. Garder une variante uniquement si le gain se répète au regard de la dispersion et de l’écart entre les deux passes Stable, avec une restitution continue pendant le chant. En cas d’écart ambigu ou de stabilité dégradée, garder Stable sur cette sortie.
5. Choisir manuellement **EDENWOOD** comme sortie active du PC, vérifier A2DP/SBC dans `pactl list sinks`, choisir **EDENWOOD / sortie active du PC** dans la page et répéter les étapes 2 à 4. Faire les comparaisons de profils à géométrie fixe au sein de chaque sortie. Une enceinte A2DP peut rester sensiblement plus lente, même si le transport vocal et la file locale ont été raccourcis.

### Mesurer le retour vivant sans sauvegarder de son

Utiliser le micro intégré du PC comme témoin s’il entend à la fois le son direct et le retour. Vérifier que la source choisie est un micro physique et que le niveau reste sous la saturation. Le pipeline ci-dessous requiert FFmpeg et affiche uniquement les instants de franchissement d’un seuil de crête, résolus par blocs de 5 ms. Le PCM transite en mémoire. Lancer le pipeline uniquement pour les sons brefs, puis l’arrêter avec Ctrl+C **avant** le test chanté.

```bash
pw-cat --record --raw --format s16 --rate 48000 --channels 1 \
  --latency 100ms --target "$(pactl get-default-source)" - |
ffmpeg -hide_banner -nostats -loglevel error -f s16le -ar 48000 -ac 1 -i pipe:0 \
  -af 'asetnsamples=n=240:p=0,astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:file=-' \
  -f null /dev/null |
awk -F '[ :=]+' '
  /pts_time:/ {t=$6}
  /Peak_level=/ && $2 != "-inf" && $2+0 > -35 {
    if (!seen || t-last > .025) {printf "%.3f s (%s dBFS)\n", t, $2; fflush(); seen=1}
    last=t
  }'
```

Vérifier d’abord avec le retour coupé que chaque son produit un seul événement et relever les réflexions éventuelles. Ajuster si besoin le placement ou le seuil `-35` dBFS, puis figer ce réglage par sortie. Retour actif, retenir seulement les paires direct puis retour identifiables et noter les ambiguïtés ou les clics sans retour. Les écarts se calculent en soustrayant les deux instants du même micro témoin. Les distances différentes jusqu’à ce micro ajoutent un décalage fixe à géométrie constante. Le regroupement de crêtes à 25 ms et la résolution de 5 ms limitent la méthode. Si le niveau ou l’acoustique empêche de séparer les deux arrivées, les valeurs restent indéterminées et l’écoute renseigne seulement la continuité et le délai perçu. Le pas de 5 ms du pipeline a été vérifié sur deux transitoires synthétiques espacés de 170 ms.
