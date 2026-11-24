# AEC avec référence de sortie distante

## Décision

Le prototype choisi traite le signal **sur Arch**. Il exécute deux bibliothèques AEC réelles et compare leur aptitude au retour vocal. Aucun candidat n’a préservé suffisamment le chant en boucle fermée. Kara conserve donc ses trois profils, son retour sans AEC distant et l’atténuation −9 dB comme option indépendante. Le moteur de calibration de 12 essais et son chemin d’écoute seule restent en place.

Le prototype est hors ligne. Il utilise du PCM généré, ne démarre aucune restitution et ne communique avec aucun service. La compilation locale de l’adaptateur WebRTC utilise un répertoire temporaire. Les rapports contiennent seulement des mesures numériques. [Prototype](../bin/_kara_aec_prototype.py).

## Référence accessible sur Arch

| Signal | Contenu et limite |
| --- | --- |
| PCM écrit vers `pw-cat` ou `pacat` | Voix Kara après son traitement. Le mixage, le niveau de sortie appliqué par PipeWire, les autres applications et les délais en aval sont encore inconnus. |
| Moniteur de la sortie sélectionnée | Mixage PCM livré par le graphe à cette sortie. Le signal inclut les autres flux effectivement routés vers le même périphérique, dont Strawberry. Le format stéréo garde les différences de chemin gauche/droite. |
| Signal acoustique repris par le S21 FE | Signal après le périphérique, le haut-parleur, la propagation, les réflexions et la capture Android. Le moniteur numérique précède ces transformations, ainsi que l’encodage, le tampon et la restitution de l’enceinte Bluetooth. |

ArchASP sous PipeWire 1.6.11 énumère un moniteur de la sortie analogique dans `pactl --format=json list sinks`. Un lecteur de capture seul, explicitement attaché à ce moniteur via `parec`, a fourni 261 120 octets en stéréo 48 kHz int16 sur environ 1,4 s. Le premier lot a été lu après 65 ms et l’intervalle médian entre lectures était de 21,3 ms, pour des demandes de 20 ms de tampon et 10 ms de traitement. Ces temps caractérisent les lectures du programme et son démarrage. Ils ne donnent ni l’horodatage d’un échantillon ni le délai acoustique. Aucun son de test n’a été joué. L’EDENWOOD n’était pas connecté lors de cet audit.

Pour refaire cette vérification sur un poste au repos, choisir explicitement le moniteur correspondant. La sortie de la commande compte les octets, puis la capture se ferme à expiration.

```bash
pactl --format=json list sinks
timeout 1.4s parec --device='alsa_output.pci-0000_63_00.6.analog-stereo.monitor' \
  --raw --format=s16le --rate=48000 --channels=2 --latency-msec=20 --process-time-msec=10 |
wc -c
```

Dans le contrôle synthétique à délai fixe, fournir seulement la voix de restitution tout en laissant l’accompagnement contaminer le micro a réduit l’annulation d’écho de 23,8 à 19,8 dB pour WebRTC et de 21,3 à 10,7 dB pour SpeexDSP. Le mixage complet est donc la référence requise dès que d’autres sons atteignent le même haut-parleur. Une sortie différente qui reste audible dans la pièce aurait besoin de sa propre référence.

## Architectures comparées

| Critère | A · Traitement sur Arch | B · Traitement dans Chrome Android |
| --- | --- | --- |
| Accès au rendu | Moniteur stéréo de la sortie disponible localement et vérifié. WebRTC Audio Processing 1.3 et SpeexDSP 1.2.1 déjà installés. | Il faudrait fournir le mixage au moteur du navigateur, par exemple via WASM. Le réglage natif `echoCancellation` de `getUserMedia()` ne fournit pas d’API pour injecter ce PCM comme référence de restitution. |
| Transport et horloges | Le WebSocket actuel transporte seulement le micro. Il faut mettre en relation les échantillons du Worklet et ceux du moniteur. | La même mise en relation est nécessaire, avec en plus le transit du mixage vers le téléphone. À 48 kHz en PCM int16, la voie descendante représente 0,768 Mbit/s en mono ou 1,536 Mbit/s en stéréo hors encadrement. Recevoir la référence avant son écho pourrait éviter une attente supplémentaire. Cela dépend de la gigue et du délai de l’enceinte. |
| CPU et voix | Mesurés ci-dessous pour les deux vrais moteurs. Le budget DSP est faible. La préservation du chant et l’alignement restent les facteurs limitants. | Aucun binaire AEC WASM ni mesure CPU, voix ou latence sur le S21 FE pour cette variante. Passer au navigateur ne résoudrait pas la suppression excessive mesurée quand la même voix est à la fois proche et dans la référence. |
| Bluetooth et boucle | Le moniteur précède la partie radio et le tampon de l’enceinte. Un saut de délai impose un nouvel alignement. | Même partie acoustique non observée. Jouer la référence sur le téléphone pour solliciter l’AEC natif créerait un second chemin audible et compliquerait la mesure. |

Le choix A s’appuie sur le moniteur vérifié, le coût local borné et l’absence d’une seconde voie PCM sur le Wi-Fi. La variante B reste une architecture possible à étudier avec un moteur explicite et des horodatages exploitables. Les observations actuelles ne permettent pas de chiffrer sa latence ni sa charge Android. [API de capture et AEC du navigateur](https://www.w3.org/TR/mediacapture-streams/#def-constraint-echoCancellation) · [WebRTC Audio Processing](https://webrtc.googlesource.com/src/+/refs/heads/main/api/audio/audio_processing.h).

### Alignement à valider avant un retour en direct

Kara connaît `endFrame` dans le Worklet, mais son paquet de retour actuel transporte la séquence et l’heure d’envoi JavaScript. L’heure de lecture stdout de `parec` a une limite analogue côté Arch. Les apparier directement confondrait horloges, transport et disponibilité des tampons. La calibration acoustique existante mesure un autre trajet et ne fournit pas à elle seule ce décalage.

Une suite minimale côté serveur utiliserait une référence stéréo explicitement liée à la sortie, datée selon la position des buffers et l’horloge du graphe PipeWire. Elle conserverait les indices de capture du Worklet jusqu’au serveur. Une correspondance affine entre horloges fournirait un premier décalage et une correction progressive de dérive. Une mesure acoustique indépendante permettrait ensuite de vérifier le délai résiduel, en particulier après un saut Bluetooth. La session devrait invalider sa référence en cas de trou, changement de sortie ou discontinuité et redémarrer l’état adaptatif sans réutiliser les anciens échantillons. Un tampon d’attente éventuel devrait avoir une borne et être mesuré avant toute activation. Les champs de temps de PipeWire décrivent le graphe et sa file, avec une portée distincte de la capture acoustique. [Horloge et latence de flux PipeWire](https://docs.pipewire.org/structpw__time.html).

## Banc AEC et résultats

Le banc traite du PCM int16 mono à 48 kHz par trames de 480 échantillons. La référence peut être mono ou stéréo. WebRTC exécute AEC3 avec ses mécanismes internes de suppression, sans activer l’AGC, le réducteur de bruit ni le passe-haut séparé. SpeexDSP utilise `speex_echo_cancellation` en trames appariées, sans préprocesseur, avec une queue adaptative de 200 ms, puis 120 ms en contrôle. Aucun algorithme d’annulation artisanal n’est utilisé. [Documentation SpeexDSP](https://www.speex.org/docs/manual/speex-manual/node7.html#SECTION00740000000000000000).

Le micro synthétique combine une voix utile indépendante, un rendu avec accompagnement, un délai et plusieurs réflexions. Le rendu virtuel est décalé grossièrement pour laisser 25 ou 30 ms de retard au filtre. Le cas d’alignement idéal connaît l’horloge et le délai simulés. Il fournit une borne favorable et ne représente pas un estimateur intégré. Les cas couvrent une dérive de 180 ppm, un délai de 360 ms avec saut de 27 ms et variation lente, des retours stéréo distincts, des syllabes faibles et leurs fins. Le cas fermé renvoie au micro le signal réellement produit par le moteur après la compression de Kara, avec un retard de 155 ms. Cette boucle met en concurrence la voix directe et ses propres copies, indépendamment de l’accompagnement.

Les critères d’intégration choisis demandent au moins 10 dB d’annulation pendant les fenêtres d’écho seul, un gain de voix entre −1,5 et +1,5 dB, un SI-SDR d’au moins 15 dB et des trames traitées dans les 10 ms pendant les essais. Le gain est estimé par projection sur la voix connue. Le SI-SDR mesure l’erreur résiduelle relativement à cette voix, après recalage du délai fixe du moteur. Il est invariant à une réduction générale de volume et pénalise aussi les déphasages. Il s’agit d’un contrôle de fidélité de forme d’onde, distinct d’une note d’écoute. Le contrôle −9 dB vérifie qu’une atténuation générale ne peut pas passer le seuil.

| Scénario | WebRTC AEC3 | SpeexDSP, 200 ms |
| --- | --- | --- |
| Retard fixe de 130 ms | Annulation de 23,8 dB. Voix faible −7,8 dB. Chant −3,4 dB, SI-SDR 5,3 dB. | Annulation de 21,3 dB. Voix faible −1,1 dB. Chant −1,0 dB, SI-SDR 6,4 dB. |
| Dérive de 180 ppm, puis horloge connue | Annulation 23,2 → 23,9 dB. Chant SI-SDR 3,6 → 8,6 dB. | Annulation 6,3 → 21,3 dB. Chant SI-SDR 4,4 → 6,4 dB. |
| Délai synthétique de 360 ms avec saut | Annulation 54,2 dB avant le saut et 49,3 dB après. Voix faible −66,5 dB au moment du changement. | Annulation 0,8 dB avant et 0,3 dB après. Même avec alignement idéal, 7,2 dB avant et 6,1 dB après. |
| Boucle du propre retour vocal à 155 ms | Énergie des rebonds après la phrase −52,4 dB. Chant utile −9,6 dB, SI-SDR −2,7 dB. | Énergie des rebonds après la phrase −15,2 dB. Chant utile −1,8 dB, SI-SDR 2,4 dB. |

En boucle fermée, le SI-SDR du chant avant correction était de 3,0 dB. En contrôle sans référence active, le SI-SDR vaut 13,9 dB pour WebRTC et 6,4 dB pour SpeexDSP. Réduire la queue de SpeexDSP à 120 ms améliore l’annulation à délai fixe à 23,9 dB, sans améliorer la qualité du chant en boucle. La dérive et la sélectivité en double parole causent donc des problèmes distincts. L’absence de voix réelle, de saturation des haut-parleurs et de traitement Android dans ce modèle limite la portée du résultat.

Les mesures p95 du CPU par trame vont de 127 à 158 µs pour WebRTC et de 126 à 217 µs pour SpeexDSP. Les cas tabulés traitent chacun 1 200 trames et aucune n’a dépassé 10 ms dans ce relevé. Le retard du noyau déterminé par corrélation avec une capture large bande seule est de **430 échantillons, soit 8,96 ms**, pour WebRTC. SpeexDSP donne un pic de corrélation à zéro échantillon, avec la différence de forme d’onde rapportée ci-dessus. Les profils existants livrent déjà 10 ou 20 ms de PCM. Cet essai ne mesure aucun tampon d’alignement en direct, ni la cadence ou la latence acoustique sur Android.

### Reproduction

```bash
OPENBLAS_NUM_THREADS=1 python3 bin/_kara_aec_prototype.py
OPENBLAS_NUM_THREADS=1 python3 bin/_kara_aec_prototype.py --engine speex --speex-tail-ms 120
OPENBLAS_NUM_THREADS=1 python3 tests/test-kara-aec-prototype.py
node tests/test-kara-audio-monitor.js
node tests/test-kara-calibration-capture.js
node tests/test-kara-calibration-engine.js
(for test in tests/test-kara-*.py; do python3 "$test" || exit; done)
git diff --check
```

Sur ArchASP, le banc utilise les bibliothèques installées, Python avec NumPy/SciPy et, pour WebRTC, un compilateur C++17 avec `pkg-config`. Le programme choisit le moteur avec `--engine`. Les chiffres de temps dépendent de la machine. Les tests AEC vérifient la réduction à délai fixe, la continuité des trames, les tailles en entrée de l’ABI, le mode stéréo et la différence entre annulation et atténuation générale.

## Repères de la séance EDENWOOD

Avec l’EDENWOOD PARTY 250 V2 en A2DP SBC, trois sondes de 100 ms ont donné 131,77, 134,52 et 136,94 ms du monitor à la recapture sur le Vivobook. L’intervalle couvre la restitution, la propagation et la chaîne du témoin. Le S21 FE était coupé pendant cette mesure. En écoute seule sur le S21 FE, les trois sondes ont traversé le graphe et des paquets continus sont arrivés au serveur. À 65 % sur l’enceinte, les pics de corrélation sur le téléphone avaient des concurrents trop proches pour calibrer une référence d’annulation.

Trois syllabes synthétiques ont relevé les corrélations principales de 0,17–0,21 à 0,38–0,52. L’appariement individuel a échoué sur les trois syllabes. Une seconde série a fourni 828 blocs continus. L’alignement conjoint contraint par l’espacement des syllabes a distingué les faux pics mobiles sur le banc synthétique. Sur l’EDENWOOD, le rapport entre le meilleur alignement et son concurrent était de 1,010, sous le seuil de 1,25. À niveau et stimuli identiques, avec l’annulation d’écho et la réduction de bruit déclarées inactives par Chrome, 828 blocs sont arrivés sans interruption et le rapport est resté sous le seuil à 1,093. La prédiction sur des syllabes tenues à l’écart de l’ajustement reste à démontrer. Le PCM du téléphone a été traité en mémoire et le volume de l’enceinte rétabli à 35 %.

Le pilotage local d’Android par ADB USB a permis d’ouvrir la page Kara, d’activer l’écoute seule puis de couper le microphone. Le PCM du téléphone a continué à passer par WebSocket sur le LAN. En SBC à 35 %, deux séries comparant les références du monitor et du témoin ont été arrêtées par le contrôle local de qualité de la référence. Le signal acoustique mesuré valait 1,56 à 1,81 fois le bruit de fond RMS selon la syllabe, sous le rapport minimal de 3. À 50 %, le signal est monté à 2,67–2,91 fois le fond RMS. Le prévol à signal nul a aussi mesuré un bruit local variable, avec une médiane RMS de 0,00020 et un 90e percentile de 0,00039 dans la bande de 500 Hz à 6,5 kHz. L’alignement du téléphone avec la référence acoustique reste indéterminé. Le retour normal, les réglages Chrome et le niveau de 35 % ont été rétablis.

En rapprochant le Vivobook à environ 30 cm de l’enceinte et en gardant le S21 FE à sa position de chant, le témoin a reçu les syllabes à 5,0–10,4 fois le bruit RMS, en SBC à 35 %. Les deux arrivées locales retenues étaient à 112–114 ms après le monitor. La fenêtre de référence commençait à 120 ms et coupait le début des syllabes. La fenêtre corrigée couvre de 100 à 400 ms. À ce placement, l’écart entre le meilleur alignement des syllabes d’origine et son concurrent est passé de 1,237 à 1,340 après correction. Sur une autre série, les prédictions de temps fondées sur deux syllabes divergeaient jusqu’à environ 80 trames pour la syllabe écartée. Un filtre plus long a retrouvé des réflexions de 18 et 27 ms sur un cas contrôlé, avec 18,3–38,9 dB de prédiction en validation croisée. Il a rejeté un saut de délai simulé. Sur les syllabes réelles, le meilleur résultat du modèle long n’a atteint que 4,45 dB et deux prédictions ont aggravé le résidu.

Une série de trois signaux générés moins périodiques a ensuite porté la référence locale à 16,4–17,9 fois le bruit RMS. Le téléphone a fourni 814 blocs consécutifs. Dans les trois validations croisées, les deux signaux d’ajustement prédisaient le même alignement temporel pour le troisième, sans correction sur la grille de vérification de ±3 trames. Le décalage a évolué de deux trames sur 2,90 s entre la grille du graphe et le PCM du téléphone. Cette stabilité est propre à la série et ne démontre pas une horloge physique commune. La prédiction sur les signaux écartés est restée à 3,14–3,43 dB pour le filtre court et à 3,73–4,13 dB avec une réponse relative jusqu’à 42,67 ms après l’alignement. La référence du monitor ne fournissait pas d’alignement cohérent sur cette série. Le seuil de 10 dB est resté inchangé. Le transfert entre la référence et le S21 FE reste le blocage pour un essai de chant. Ces captures ont été traitées en mémoire, en écoute seule avec les traitements Chrome désactivés temporairement.

### Origine des transformations

Quatre configurations Chrome ont été comparées à 35 % sur l’EDENWOOD en SBC, avec le Vivobook près de la grille et le S21 FE au même emplacement. Chaque passage exploitable comprenait six répétitions du même signal de 630 ms, trois par niveau numérique à un écart de 6,02 dB. Le signal comprend un repère apériodique, une note synthétique et un intervalle à deux fréquences de 733 et 1 171 Hz. Le monitor a reproduit les six marqueurs, avec les mêmes niveaux pour les quatre configurations. La différence acoustique entre les deux niveaux vaut 5,58–5,87 dB au témoin selon le passage. Pour un même niveau, le résidu médian entre répétitions du témoin, après ajustement d’un gain scalaire et d’un décalage borné, vaut environ 14–15 dB. Cette mesure sur un signal répété ne constitue pas une validation sur un signal nouveau.

| Annulation Chrome | Réduction de bruit Chrome | Source Android observée | Alignements S21 FE | Rapport meilleur pic / concurrent |
| --- | --- | --- | --- | --- |
| Désactivée | Désactivée | `MIC` | 6/6 | 2,54–3,17 |
| Désactivée | Active | `MIC` | 6/6 | 1,91–2,82 |
| Active | Désactivée | `VOICE_COMMUNICATION` | 0/6 | 1,02–1,05 |
| Active | Active | `VOICE_COMMUNICATION` | 0/6 | 1,02–1,08 |

Le S21 FE a déclaré 48 kHz, un canal et le gain automatique désactivé dans les quatre passages. Chacun a livré 1 031 ou 1 032 blocs de 10 ms consécutifs. AudioFlinger a affiché `Enable Aec: 1` et `Acoustic Echo Canceler` sur la session avec annulation seule. Le changement de voie Android déclenché par l’annulation Chrome suffit à dégrader les repères pour ce protocole. La réduction de bruit seule a conservé leur cadence, avec une dérive apparente d’une trame par intervalle de 54 720 trames, identique au passage sans traitements. La latence déclarée par Chrome valait 10 ms avec la réduction de bruit et 40 ms sans elle, indépendamment de l’annulation. Le délai physique de capture n’a pas été mesuré par ces champs. [Sémantique des réglages de piste](https://w3c.github.io/mediacapture-main/getusermedia.html).

L’écart de niveau au S21 FE en voie `MIC` était de 6,72 dB. Sans traitements, ajuster un gain scalaire lors de la comparaison entre niveaux du même signal fait passer la prédiction médiane de 9,91 à 11,51 dB. La cohérence médiane par bande avec le témoin varie de 0,57 à 0,86. La compensation de gain explique une partie des différences sur des répétitions, tandis que la généralisation aux trois signaux différents reste sous 10 dB. Sur un contrôle synthétique à phase centrée, la dérive apparente relevée de 18,3 ppm laisse 28,8 dB de prédiction. Une erreur fixe de deux trames fait tomber ce même contrôle à 9,5 dB. La dérive lente mesurée seule n’explique donc pas le résidu, tandis que la phase reste déterminante. L’overlay Android du S21 FE annonce `config_supportAudioSourceUnprocessed=false`. Android expose aussi `VOICE_PERFORMANCE` pour la capture à faible délai. Son comportement sur ce téléphone reste à démontrer. Le contournement de `VOICE_COMMUNICATION` est déjà possible dans Chrome pour les mesures. [Sources de capture Android](https://developer.android.com/reference/android/media/MediaRecorder.AudioSource) · [Résolution de la propriété Android](https://android.googlesource.com/platform/frameworks/base/%2B/59831c0d941156cd3fb405c543c4f17016314544/media/java/android/media/AudioManager.java).

L’ajustement des deux tons laisse un résidu acoustique important au témoin local, d’environ 8 à 9 dB sous les fondamentales dans la plupart des passages, contre plus de 66 dB au monitor. Des composantes apparaissent aussi aux fréquences testées d’intermodulation. Leur amplitude relative varie et ne croît pas systématiquement au niveau fort. Ce seul relevé ne permet pas d’attribuer une non-linéarité à l’enceinte. Un aller-retour hors ligne avec libsbc 2.2 et la configuration A2DP négociée a donné 55–57 dB de prédiction de forme d’onde au bitpool 58, et au moins 27,7 dB aux bitpools testés de 16 à 40. La simulation introduit 73 trames de décalage à 48 kHz. Elle couvre la quantification SBC du signal d’essai. Elle ne couvre ni le transport radio réel, ni le rééchantillonnage, ni le traitement interne de l’enceinte, la pièce ou le micro témoin. Un essai sur l’entrée AUX à niveau acoustique apparié séparerait la liaison Bluetooth du chemin commun restant. [Implémentation SBC de PipeWire 1.6.11](https://github.com/PipeWire/pipewire/blob/1.6.11/spa/plugins/bluez5/a2dp-codec-sbc.c).

Pour la stabilisation, un contrôle synthétique a construit deux captures identiques à partir d’une note directe en crescendo et d’une boucle résonante de 300 ms dont le gain croît de 3,05 dB par tour. Un détecteur fondé uniquement sur la croissance de cette bande déclencherait de la même façon sur les deux cas. Le filtre coupe-bande du contrôle a retiré 36,4 dB à la note directe. Une marge fixe stabilise la boucle simulée au prix d’une baisse équivalente du premier retour. Le contrôle illustre la limite de ces deux approches pour le chant et ne mesure pas un seuil de Larsen sur l’EDENWOOD.

Dans une boucle **simulée** de 300 ms, WebRTC AEC3 a réduit la traîne de 47,4 dB mais atténué le chant doux de 6,5 dB et le chant normal de 8,0 dB. SpeexDSP à 120 et 200 ms a réduit la traîne de 8,1 et 7,1 dB avec une dégradation du chant. L’expansion de niveau a échoué sur les passages doux. Une soustraction limitée aux copies cohérentes a réduit les fins de phrase de 19,5 à 22,2 dB dans les trois cas simulés où la réponse acoustique était connue, mais le chant en recouvrement est resté sous le seuil de fidélité de 15 dB. Ces candidats restent au banc.

## Essai court sur le S21 FE et retour arrière

1. Utiliser les haut-parleurs intégrés, profil **Stable**, avec Strawberry en pause. Garder téléphone, placement, délai du test et volume physique constants. Commencer à faible niveau. Pour établir le témoin, prononcer une phrase suivie de silence, puis une phrase douce et le même passage chanté. Couper entre les passes.
2. Faire **A · Sans AEC distant, protection désactivée**, puis **B · Protection −9 dB**. Noter séparément le niveau du premier retour, les rebonds qui suivent, les fins de mots, les artefacts et le délai ressenti. Pour une comparaison à niveau égal, choisir un premier retour commun et prudent en baissant les modes les plus forts. Garder ce réglage pour chaque passe. Le micro témoin du [protocole de latence](audio-latency-optimization.md) peut mesurer des transitoires séparés en mémoire, puis doit être coupé pour les passages chantés.
3. **C · AEC distant** devient testable seulement avec un candidat validé sur le banc et une référence analogique alignée. Rejouer le même protocole, avec les mêmes placements et premier retour. Retenir l’AEC uniquement si les rebonds diminuent à niveau comparable et que les syllabes, le chant et la continuité tiennent. Mesurer le delta acoustique depuis le témoin Stable. Tester **Équilibré** après cette validation puis faire une campagne Bluetooth séparée. En cas de boucle ou de montée du son, utiliser **Couper** immédiatement.

La version livrée expose A et B. Le prototype se lance par une commande séparée. Pour revenir du mode B au témoin A, appuyer sur **Couper**, décocher **Protection contre les répétitions** puis relancer le micro. C nécessitera un essai manuel avec une référence correctement alignée et un niveau de retour représentatif.

Pour disposer entre-temps d’un retour audible avec le S21 FE, une sortie PC filaire vers un casque fermé limite physiquement la réinjection sans imposer une baisse générale du retour vocal. Pour conserver la diffusion dans la pièce, la piste technique prioritaire est une chaîne de référence et de capture synchrone sur Arch, par exemple un micro filaire et une sortie de la même interface audio, puis un AEC pleine bande validé en double parole à niveau apparié. Le téléphone Wi-Fi avec haut-parleurs ouverts reste le cas à résoudre.
