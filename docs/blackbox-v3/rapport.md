# Rapport BlackBox V3

Branche locale `feat/audio-latency-optimization`, référence de départ `52d6943`. Les essais utilisent des fixtures synthétiques, un graphe PipeWire virtuel ou les captures historiques ouvertes en lecture seule. Les essais physiques de la prochaine partie appartiennent à l’utilisateur.

## Résultats

- BlackBox conserve un seul propriétaire, un seul journal par session et le lecteur V1. V3 rattache le PCM, les horloges, les fenêtres Turbo, les décisions et les événements clients aux cycles Kara. Les blocs WAV sont publiés atomiquement et la reprise préserve les plages encore lisibles. Les captures vocales nécessitent une activation et un accord individuel par connexion. [Architecture](architecture.md)
- Le PCM reçu avant filtrage réseau, le signal vers le worker, le mix Kara après faders, le retour numérique, le monitor PipeWire et, sur sélection, le témoin acoustique peuvent être collectés. Le décodeur Turbo préparé fournit les entrées PCM exactes des appels admissibles avec SHA-256, paramètres, segments et tokens. Les stems sont référencés par empreinte. La matrice de disponibilité figure dans le manifeste. [Points de mesure](architecture.md#points-de-capture-et-rapprochement-des-horloges)
- Les 12 captures historiques restent lisibles. Les 34 décisions historiques sont reproduites. Un score final est reproduit et trois tours n’ont pas assez de données pour le calcul final. Les quatre anciennes paires monitor/témoin partagent des tours et des plages d’horloge d’hôte mais utilisent des pilotes PipeWire distincts, avec quantums 256 et 1 024. Les coupures du monitor sont attestées et leur cause exacte reste indéterminée. [Audit](architecture.md#état-de-départ-et-audit-omega)
- Les tests synthétiques contrôlent à la fois une paire continue, une paire disjointe et une paire avec piste manquante. Le banc V3 retrouve le décalage de 40 ms injecté entre microphone et sortie sur un cas à deux domaines d’horloge. L’échange réseau asymétrique simulé borne l’offset à un intervalle de 14 ms. La borne physique reste inconnue. [Validation](validation.md)
- Le rejeu par événement, intervalle, fenêtre Turbo ou tour fonctionne hors ligne. La qualification AEC compare le signal inchangé, AEC3, Speex et le moteur SPA WebRTC de PipeWire Echo Cancel sur les mêmes trames. Chaque candidat atteste les deux empreintes de référence et de microphone. Le Studio ouvre le prévol, la chronologie, les marqueurs, l’intégrité et les résultats dans une fenêtre distincte. Les annotations versionnées et les perturbations reproductibles sont accessibles en CLI. [Rejeu](replay.md)
- Le comparatif Turbo et CTC vérifie le SHA-256 du même PCM d’entrée pour chaque moteur. Le test d’intégration CPU a traité 19 200 échantillons synthétiques de 16 kHz sans parole. L’analyse phonétique compare ensuite les mots décodés. Le diagnostic confirme zéro point pour le silence et les paroles incorrectes, l’exclusion des sources musique et artiste, et 100 points pour le témoin positif. Il vérifie aussi la récupération d’une capture synthétique interrompue et l’arrêt au quota. [Validation](validation.md)

## Performances et stockage

Le banc de soumission à cinq pistes écrit 8 479 754 octets en 6 s, soit 5,09 Go/h hors témoin et Turbo. Le p95 du pas de soumission passe de 128,17 µs en mode arrêté à 351,38 µs en Deep Analysis. Le CPU cumulé passe de 0,041 à 0,291 s et la perte de diagnostic reste nulle sur ce banc. Les temps de latence du retour, les xruns et l’inférence pendant une partie réelle restent à mesurer. Le budget par session est de 128 Mio pour le diagnostic léger ou 16 Gio avec voix, avec une réserve minimale de 32 Mio. Avec une provision de 6 Go/h sans témoin et de 6,9 Go/h avec témoin, quatre heures de jeu actif demandent respectivement 24 Go et 27,6 Go au-delà de la réserve. Le plafond couvre environ 2 h 52 ou 2 h 29. Le nettoyage est manuel. L’expérience FLAC restitue le PCM16 exactement, avec un débit et un gain dépendants du contenu. [Mesures et conditions](validation.md#charge-et-stockage-du-banc-synthétique)

## Limites opérationnelles

Le navigateur et le système mobile peuvent traiter le signal avant que Kara le reçoive. La chaîne actuelle ne fournit pas de piste indépendante `MIC_RAW` avant traitement mobile ni de sortie AEC séparée `MIC_AEC`. Les métriques acoustiques objectives exigent un témoin propre et une phase où le joueur ou l’écho peuvent être isolés. Le comparatif SPA traite les trames hors ligne et laisse à la qualification physique le délai et le routage du graphe de sortie. L’analyse phonétique porte sur les transcriptions. Le WER exige une référence humaine indépendante.

Le prévol indique l’état logiciel et les vérifications à faire sur un premier tour. L’aperçu GTK suit le journal et les rapprochements AEC utilisent les horloges des sources. L’écoute A/B se fait sur demande par export des WAV privés. Les annotations d’intervalle se font en CLI. Le protocole de calibration reste explicite et le rejeu vérifie le reçu face à la configuration de sortie enregistrée. Les délais physiques du Bluetooth, du retour, de la télévision et de l’ADC/DAC requièrent des mesures matérielles. [Première partie laboratoire](protocole-physique.md)

La stabilité d’un rejeu exact ne garantit pas l’égalité de toutes les probabilités de Turbo. Sur la répétition CPU du chirp synthétique, la probabilité de non-parole varie de 0,00002. Le segment, le token et les temps observés restent identiques. Aucun WER sur du chant réel n’a été mesuré.

## Déploiement

Le 26 novembre 2026, le déploiement a attendu l’arrêt effectif du transfert USB. La reprise restait disponible à l’opération 932 sur 10 934. Après autorisation, le service a redémarré sur la révision isolée `cda639f`, qui reprend BlackBox V3 et ses correctifs validés. Le plan et le curseur de reprise USB ont conservé leurs empreintes. Le lanceur local pointe sur cette version pour préserver le travail concurrent sur l’index.

Parole a été rouvert et BlackBox Studio a été ouvert par **Paramètres → Maintenance → Ouvrir BlackBox Studio**. Le service était actif sans redémarrage automatique ni erreur de niveau `err` au contrôle. Le même processus détenait la fenêtre GTK, le nom D-Bus et l’inhibiteur de capot. Le serveur a servi le JavaScript mobile exact de la version déployée. La TV installée conserve sa version et la capture est restée arrêtée.

Les empreintes avant et après concordent pour 13 880 pistes indexées par Parole, 13 926 entrées disponibles dans Strawberry, 81 associations, 30 tâches d’import, 7 reçus de préparation, 12 métadonnées de séparation et 12 manifestes BlackBox. La file d’import a retrouvé son état actif initial. Les réglages persistants, dont ceux de la préparation nocturne, ont conservé leurs empreintes. Trois sauvegardes SQLite `0600` vérifiées par `quick_check` et les preuves locales sont dans `.runtime/blackbox-v3/deployment-20261126-175116/`.

## Commits locaux du code

| Commit | Périmètre |
| --- | --- |
| `f5f0f00` | Identifiants de cycles, lecteurs V1/V3, blocs atomiques, micro témoin sur sélection. |
| `aeacba3` | Référence du mix, réception microphone, consentement et correspondances d’horloges. |
| `f50cd2c` | Recouvrement par cycle, prévol, rejeu et export expurgé. |
| `7ee4893` | Entrées exactes et métadonnées de Turbo. |
| `f694c7c` | Comparatif AEC3 et Speex. |
| `621186f` | Marqueurs, Tizen et télémétrie adaptative. |
| `1c15bd3` | Fenêtre GTK BlackBox Studio. |
| `5e7e875` | Modes Calibration et Benchmark, annotations et incidents simulés. |
| `ca2513d` | Rejeu du score final et crédit mot par mot. |
| `7cd8582` | WAV du comparatif AEC et réglages du tour. |
| `7810951` | Ouverture du manifeste hors du verrou d’admission audio. |
| `9d38f71` | Détections de fenêtres sans décision et anomalies de reconnaissance. |
| `a895850` | Empreinte des sorties et contrôle du reçu de calibration. |
| `b4056fb` | Moteur SPA de PipeWire et empreintes des trames du comparatif AEC. |
| `bc3896a` | Comparatif Turbo et CTC sur PCM identique, variantes phonétiques et WER sur référence humaine. |
| `624b9a7` | Diagnostic complet, budget disque, récupération après interruption et contrôles négatifs du score. |
| `5e8575e` | Accès au Studio depuis les paramètres de maintenance. |
