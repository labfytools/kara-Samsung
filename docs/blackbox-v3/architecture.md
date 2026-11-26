# Architecture BlackBox V3

## État de départ et audit OMEGA

L’enregistreur `_kara_blackbox.py` est possédé par Parole. Le mobile et l’interface GTK pilotent cette instance. Un thread sérialise une file bornée à 512 entrées, écrit le journal append-only et segmente le PCM. Le module `_kara_room_audio.py` reçoit le téléphone, alimente le retour et transmet des datagrammes locaux au processus de score. `_kara_acoustic_capture.py` ouvre les taps PipeWire consentis pour le tour. `_kara_game.py` et `_kara_voice.py` publient les fenêtres et décisions du score. `_kara_tv.py` et le client mobile déposent leurs événements accessibles. `bin/kara-blackbox` exploite le même journal pour l’inventaire, les contrôles, l’extraction et le rejeu. [Documentation V1](../kara-v1.md#blackbox) · [Documentation V2](../kara-v2.md) · [OMEGA I](../omega/rapport.md) · [OMEGA II](../omega-ii/rapport.md)

| Donnée V1 | Posée à la source |
| --- | --- |
| Identifiants | Dossier de session aléatoire daté, numéro d’événement croissant dans la session, tour 64 bits, fenêtres Turbo numérotées dans leur processus. Les indices de graphe restent propres à chaque pilote. |
| Journal et intégrité | JSONL pour les événements et incidents, JSON pour le manifeste et la synthèse, Markdown pour le rapport. Empreintes SHA-256 et tailles à la clôture. En reprise, fermeture de RIFF et conservation du préfixe valide du journal. |
| PCM | Wi-Fi reçu après admission dans le pipeline, datagrammes 16 kHz à destination du score et retour en PCM16. Monitor stéréo et témoin mono en float32 LE 48 kHz. Stems référencés par empreinte. |
| Temps | Réception et publication monotones côté serveur, positions de worklet, temps du navigateur, repères du lecteur, position/quantum/pilote/xrun des taps PipeWire. UTC pour l’inventaire. |
| Événements | Commandes et phases, mixage et volumes, préparation, réseau audio, changements ou arrêts navigateur, fenêtre et décision Turbo, score, affichage Tizen logiciel, marqueurs, ressources système. |
| Stockage | 128 Mio de budget léger, 16 Gio avec audio, segments de 16 Mio de PCM et réserve libre de 32 Mio. Racine XDG privée, liens du dépôt ignorés par Git. |

L’inventaire local retrouve 12 sessions closes occupant environ 640 Mio sur disque. OMEGA II a reproduit 34 décisions de score. Dans les quatre sessions contenant monitor et témoin, le tour et au moins une ouverture des taps coïncident. Leurs pilotes PipeWire diffèrent dans chaque ouverture commune. Le monitor est cadencé à 256 échantillons et le témoin à 1 024. Aucune clé `(tour, ouverture, pilote, frame)` commune, aucun horodatage de cycle strictement identique. Les enveloppes entre premiers et derniers horodatages se recoupent de 26,8 à 359,0 s selon la session. Cette mesure d’enveloppe peut inclure des trous. Les quatre monitors signalent `pcm_missing_or_invalid`, état du helper natif lorsqu’un tampon attendu devient indisponible ou invalide. Le journal historique ne précise pas lequel ni la cause de ce défaut. [Qualification OMEGA II](../omega-ii/qualification-audio.md#kara-écho-et-préservation-de-la-voix)

Une paire de taps séparés protège l’horloge de la sortie contre une réattribution du graphe. Un temps monotone d’hôte donne un repère logiciel pour ces deux taps. Aucun reçu de calibration ADC/DAC, modèle de retard Bluetooth, horloge matérielle partagée ou erreur de recalage n’établit l’équivalence de leurs échantillons historiques. Le lecteur V1 préserve les données et le banc AEC exige toujours une même grille de graphe pour son mode strict.

## Format V3

Kara conserve le dossier de session et le journal append-only. Le schéma V3 ajoute un identifiant de cycle `SESSION/TOUR`, un identifiant de flux `SESSION/TOUR/PISTE`, une identité de salon et de partie émise par Parole et une empreinte de configuration. Les tours gardent leur identité émise par Kara. Le joueur est représenté par un pseudonyme HMAC propre à la session, avec clé volatile. Les événements produits hors d’un tour peuvent avoir un cycle nul. Les empreintes de la préparation, de l’enregistrement et des références musicales proviennent des versions identifiées par le pipeline.

Un WAV s’écrit sous suffixe `.part`, est synchronisé avec son en-tête définitif, puis renommé et publié. La fin de session publie le manifeste après les index et rapporte les pertes. Lors d’une interruption, la reprise répare les brouillons RIFF lisibles et conserve les événements dont la plage PCM est encore présente. Elle préserve les identifiants et les événements suivants, et compte séparément les références PCM irrécupérables. Les captures V1 restent lisibles avec leurs métadonnées d’origine.

Le manifeste distingue l’état du journal de l’état de couverture des pistes. Les références musicales locales peuvent être réutilisées par empreinte. Le monitor PipeWire représente le mix numérique accessible au noeud de sortie et peut inclure d’autres lecteurs. La grille interne, la latence des périphériques et le son dans la pièce ont des points de mesure distincts. Le micro témoin est désélectionné par défaut et requiert une sélection explicite.
