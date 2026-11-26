# Validation BlackBox V3

## Compatibilité et contrôles automatisés

| Contrôle | Résultat |
| --- | --- |
| Sessions historiques | 12 sessions examinées en lecture seule, 34 décisions de score reproduites à l’identique. |
| `test-kara-blackbox-v3.py` | Identité des cycles, sélection du témoin, pseudonymisation, envoi après fin de cycle, récupération de blocs, échange asymétrique, recouvrement et export. Un appel Turbo transféré en mémoire retrouve son SHA-256 et son PCM dans le WAV extrait. |
| `test-kara-v1.py` | 15 cas passent, dont consentement mobile, historique, reprise, quota et voies d’extraction. |
| `test-kara-pipewire-latency.py` | 12 cas passent lors du premier lot. Le test du monitor passif et du témoin sur graphe virtuel a aussi été relancé après l’ajout des réouvertures bornées. |
| `test-kara-audio-monitor.py` et `test-kara-audio-monitor.js` | 23 cas Python et le scénario JS passent, avec trames, séquences, retour et reconnexion. |
| `test-kara-turn-mix.py` | 4 cas passent dans le venv de score. Le probe mesure le PCM après les faders et l’essai compare les octets à ceux de la sortie GStreamer. |
| `test-kara-preparation.py` | 37 cas passent dans le venv de score. |
| `test-kara-aec-blackbox.py` | Le témoin de grille commune passe, avec une estimation synthétique de 40 ms. Le témoin à pilotes distincts est refusé. |
| Tizen et code natif | Scénario JavaScript Tizen réussi. Syntaxe C17 stricte du wrapper DTW validée contre les en-têtes du moteur installé. |
| BlackBox Studio | Les deux cas passent dans Sway headless sous bus D-Bus isolé. Le test charge une capture synthétique, affiche sa chronologie et vérifie ses empreintes. |

Le premier lancement de `test-kara-turn-mix.py` avec le Python système n’avait pas `pywhispercpp`. L’exécution a ensuite utilisé le venv du moteur. Le premier passage après ajout d’un ticket dans la fixture omettait trois propriétés de route. Les propriétés ont été ajoutées et les quatre cas sont passés.

## Déterminisme du modèle

Avec whisper.cpp 1.8.2 et le modèle local de type GGML 1, un premier contrôle d’égalité complète échoue sur deux inférences CPU à quatre threads du même chirp synthétique de 1,2 s. Une répétition de trois appels sur ce signal a duré 19,83 s, 19,73 s et 19,70 s. La transcription, les repères du segment, l’identifiant et le temps du seul token ainsi que sa probabilité sont restés identiques. L’écart maximal de probabilité interne de non-parole est de 0,00002 par rapport au premier appel. La reproductibilité de l’entrée par SHA-256 et la reproductibilité de chaque champ du modèle sont deux contrôles distincts. Ce signal sans parole ne donne ni WER ni validation sur du chant.
