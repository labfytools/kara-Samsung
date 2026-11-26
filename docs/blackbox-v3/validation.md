# Validation BlackBox V3

## Compatibilité et contrôles automatisés

| Contrôle | Résultat |
| --- | --- |
| Sessions historiques | 12 sessions examinées en lecture seule, 34 décisions de score reproduites à l’identique. |
| `test-kara-blackbox-v3.py` | 9 cas passent. Identité des cycles, sélection du témoin, pseudonymisation, envoi après fin de cycle, récupération, admission indépendante du disque, échange asymétrique, recouvrement, export, marqueurs, annotations, simulation et rejeu du score de tour. Un appel Turbo transféré en mémoire retrouve son SHA-256 et son PCM dans le WAV extrait. |
| `test-kara-v1.py` | 15 cas passent, dont consentement mobile, historique, reprise, quota et voies d’extraction. |
| `test-kara-pipewire-latency.py` | 12 cas passent sur le code final, dans un graphe virtuel. |
| `test-kara-audio-monitor.py` et `test-kara-audio-monitor.js` | 23 cas Python et le scénario JS passent, avec trames, séquences, retour et reconnexion. |
| `test-kara-turn-mix.py` | 4 cas passent dans le venv de score. Le probe mesure le PCM après les faders et l’essai compare les octets à ceux de la sortie GStreamer. |
| `test-kara-preparation.py` | 37 cas passent dans le venv de score. |
| `test-kara-aec-blackbox.py` | 2 cas passent. Le témoin de grille commune et le témoin mobile/monitor V3 retrouvent le décalage synthétique de 40 ms. Le banc de grille stricte refuse les pilotes distincts. Le reçu de calibration est accepté pour l’empreinte correspondante, refusé si la configuration a changé. |
| Tizen et code natif | Scénario JavaScript Tizen réussi. Syntaxe C17 stricte du wrapper DTW validée contre les en-têtes du moteur installé. |
| BlackBox Studio | Les deux cas passent dans Sway headless sous bus D-Bus isolé. Le test charge une capture synthétique, affiche sa chronologie et vérifie ses empreintes. |
| Non-régression audio et USB | 6 cas AEC, 20 cas de calibration serveur, 3 cas de protection et 34 cas de retour voix passent. Quatre fichiers JavaScript d’audio et de Tizen passent. Le test USB avec ADB simulé passe sous Sway headless. |

Le premier lancement de `test-kara-turn-mix.py` avec le Python système n’avait pas `pywhispercpp`. L’exécution a ensuite utilisé le venv du moteur. Le premier passage après ajout d’un ticket dans la fixture omettait trois propriétés de route. Les propriétés ont été ajoutées et les quatre cas sont passés.

Le premier appel de syntaxe du helper PipeWire redéfinissait `_GNU_SOURCE`, fourni par le source. Le contrôle strict a réussi après retrait de l’option redondante. La vérification systemd et `git diff --check` ont réussi. Côté paquet et score, le récapitulatif final historique est identique sur un tour. Les trois autres tours recensés manquent de synthèse finale ou de décisions pour ce contrôle.

## Déterminisme du modèle

Avec whisper.cpp 1.8.2 et le modèle local de type GGML 1, un premier contrôle d’égalité complète échoue sur deux inférences CPU à quatre threads du même chirp synthétique de 1,2 s. Une répétition de trois appels sur ce signal a duré 19,83 s, 19,73 s et 19,70 s. La transcription, les repères du segment, l’identifiant et le temps du seul token ainsi que sa probabilité sont restés identiques. L’écart maximal de probabilité interne de non-parole est de 0,00002 par rapport au premier appel. La reproductibilité de l’entrée par SHA-256 et la reproductibilité de chaque champ du modèle sont deux contrôles distincts. Ce signal sans parole ne donne ni WER ni validation sur du chant.

## Charge et stockage du banc synthétique

Le banc boucle pendant 6 s à 50 pas/s, avec huit soumissions PCM par pas pour cinq pistes. Quatre soumissions appartiennent au monitor. Le chronométrage du producteur englobe la construction des blocs de fixture et leur soumission. Le CPU du processus inclut la finalisation. Le maximum RSS indiqué par le processus Python est resté à 323 396 Kio sur les trois modes. Le témoin, Turbo, le Bluetooth et les clients physiques sont hors de cette mesure.

| Mode | Médiane par pas | P95 par pas | CPU cumulé | Octets en cours | Événements perdus |
| --- | ---: | ---: | ---: | ---: | ---: |
| OFF | 102,03 µs | 128,17 µs | 0,041 s | 0 | 0 |
| Diagnostic léger | 105,51 µs | 143,12 µs | 0,068 s | 2 392 | 0 |
| Deep Analysis | 257,56 µs | 351,38 µs | 0,291 s | 8 479 754 | 0 |

Le débit soutenu du lot complet synthétique est de 5,09 Go/h, avant le rapport et hors témoin et fenêtres Turbo. Le chemin de production emploie du PCM avec des écritures bornées et une réserve libre. La commande `space` indique l’espace global et `prune` propose les sessions candidates au nettoyage manuel. L’inventaire de départ compte 669 947 359 octets sur 12 captures. Les copies PCM identiques doivent être évitées. Les entrées du worker et du décodeur sont enregistrées séparément, car elles représentent des étapes différentes.

Sur deux témoins mono PCM16 48 kHz de 12 s, FLAC ramène le signal tonal de 1 152 044 à 203 898 octets et porte le bruit de 1 152 044 à 1 162 151 octets. L’encodage consomme environ 0,063 s CPU. Les deux décodages restituent chaque octet de PCM16. Le gain dépend du signal. Le monitor et les références flottantes gardent leur WAV float32. Aucune compression différée n’est activée.
