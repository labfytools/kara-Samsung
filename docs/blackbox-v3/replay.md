# Exploiter une capture BlackBox V3

Le dépôt donne accès au répertoire privé par `blackbox/sessions/`. Les captures sont listées avec leur identifiant persistant. Toutes les commandes ci-dessous opèrent sur les données locales. Le rejeu n’ouvre ni micro, ni sortie audio, ni partie, ni connexion client.

```sh
bin/kara-blackbox list
bin/kara-blackbox space
bin/kara-blackbox inspect latest
bin/kara-blackbox validate latest
bin/kara-blackbox report latest
bin/kara-blackbox export latest --output blackbox/reports/rapport-expurge.json
```

Le manifeste donne les cycles et l’état des flux. Le rapport automatique `analysis.json` contient les plages, le recouvrement des pistes et les preuves d’incident. Il est produit pendant la finalisation puis inscrit dans l’inventaire SHA-256. Le rapport expurgé contient des compteurs, des durées agrégées et des états de qualification. Les identifiants de session, noms, paroles, transcriptions, chemins, empreintes du contenu, horloges brutes et commentaires restent dans la capture privée.

## Rejeu ciblé

```sh
bin/kara-blackbox replay-timeline latest --cycle SESSION/TOUR --speed 10
bin/kara-blackbox replay-timeline latest --after-id 25 --limit 1
bin/kara-blackbox replay-timeline latest --since MONOTONE_NS --until MONOTONE_NS
bin/kara-blackbox replay latest --turn TOUR --limit 10000
bin/kara-blackbox compare latest --turn TOUR --tolerance 0.75 --tolerance 1.25
```

`replay-timeline` restitue les événements dans leur ordre de publication, en JSONL. La cadence est instantanée par défaut. `--speed 1` suit les intervalles d’origine. `--id` sélectionne une décision précise. Le comparatif de score A/B/C part des mêmes sorties de reconnaissance et applique la règle historique puis les tolérances candidates. Le score Kara reste distinct du WER, qui nécessite une transcription humaine du chant.

```sh
bin/kara-blackbox extract-pcm latest --id EVENEMENT --output blackbox/reports/bloc.wav
bin/kara-blackbox extract latest --id FENETRE --output blackbox/reports/fenetre.wav
bin/kara-blackbox replay-whisper latest --id FENETRE --device cpu
```

Le bloc PCM est extrait de son segment par index et contrôles de format. `extract` reconstruit la fenêtre d’entrée du pipeline à partir des datagrammes admis. Par défaut, l’extraction refuse un trou. `--reconstruct-gaps` applique volontairement le remplissage du moteur historique et rapporte le nombre de frames concernés. Le rejeu Whisper reconstruit cette entrée du pipeline et exige au moins 90 % de couverture. Le rejeu du comparateur de score utilise les décisions historiques déjà journalisées, sans relancer Turbo ni Demucs.

## Fenêtres AEC

BlackBox V3 recherche, pour chaque cycle, les runs contigus du microphone et de chaque sortie. Le recouvrement logiciel utilise deux ancres du même renderer comprises dans le run du microphone et intersecte leur plage avec un run de sortie sans interruption. Une fenêtre d’au moins quatre secondes prend l’état `software_overlap`. Les événements d’extrémité et les deux ancres restent accessibles par leurs identifiants. Deux pistes présentes dans un même cycle peuvent avoir un recouvrement nul. La dérive est exprimée par un intervalle issu de deux mesures d’horloge et comprend leurs incertitudes.

Un recouvrement logiciel ne qualifie pas un délai physique. La chaîne ADC, les tampons du navigateur, la présentation DAC, la latence Bluetooth, le retour micro et le trajet dans la pièce exigent les mesures du [protocole physique](protocole-physique.md). Le banc strict `tests/qualify-kara-aec-blackbox.py` demeure utilisable sur un monitor et un témoin qui partagent réellement leur grille PipeWire. Le banc synthétique `tests/test-kara-aec-blackbox.py` contrôle le comportement sur une paire commune et sur deux pilotes distincts.

```sh
bin/kara-blackbox compare-aec latest --cycle SESSION/TOUR --reference monitor
bin/kara-blackbox compare-aec latest --cycle SESSION/TOUR --reference playback --aec-delay-ms 40
```

La valeur de 40 ms illustre un réglage explicite. Utiliser la mesure de la configuration qualifiée. `compare-aec` sélectionne jusqu’à douze secondes dans le recouvrement, rapproche les grilles par interpolation linéaire et s’arrête si un bloc manque. Les comparaisons sur la même entrée couvrent le signal inchangé, WebRTC AEC3 et Speex. Le résultat journalise l’empreinte de l’entrée alignée, le délai candidat, la corrélation avec la référence, le niveau RMS et le temps de trame des moteurs. Ces métriques peuvent varier pendant le chant simultané. Elles ne quantifient ni l’atténuation d’un écho isolé ni la préservation vocale sans témoin indépendant.

## Relancer un appel du décodeur

```sh
bin/kara-blackbox extract-turbo latest --id FENETRE --call 0 --output blackbox/reports/turbo.wav
bin/kara-blackbox replay-turbo latest --id FENETRE --call 0 --device cpu --beam
```

Ces commandes requièrent une entrée Turbo V3 complète. Elles vérifient la taille et le SHA-256 relevé au point d’appel du décodeur. Le rejeu relance le décodeur sur ces octets et compare sa trace aux métadonnées historiques, avec choix du dispositif, de la langue et du décodage beam. Le terminal affiche les segments et les tokens. Une différence de moteur, de modèle, de backend ou de réglage doit être prise en compte dans l’interprétation des divergences.
