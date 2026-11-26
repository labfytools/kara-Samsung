# Rapport BlackBox V3

Branche locale `feat/audio-latency-optimization`, référence de départ `52d6943`. Les essais utilisent des fixtures synthétiques, un graphe PipeWire virtuel ou les captures historiques ouvertes en lecture seule. Les essais physiques de la prochaine partie appartiennent à l’utilisateur.

## Résultats

- BlackBox conserve un seul propriétaire, un seul journal par session et le lecteur V1. V3 rattache le PCM, les horloges, les fenêtres Turbo, les décisions et les événements clients aux cycles Kara. Les blocs WAV sont publiés atomiquement et la reprise préserve les plages encore lisibles. Les captures vocales nécessitent une activation et un accord individuel par connexion. [Architecture](architecture.md)
- Le PCM reçu avant filtrage réseau, le signal vers le worker, le mix Kara après faders, le retour numérique, le monitor PipeWire et, sur sélection, le témoin acoustique peuvent être collectés. Le décodeur Turbo préparé fournit les entrées PCM exactes des appels admissibles avec SHA-256, paramètres, segments et tokens. Les stems sont référencés par empreinte. La matrice de disponibilité figure dans le manifeste. [Points de mesure](architecture.md#points-de-capture-et-rapprochement-des-horloges)
- Les 12 captures historiques restent lisibles. Les 34 décisions historiques sont reproduites. Un score final est reproduit et trois tours n’ont pas assez de données pour le calcul final. Les quatre anciennes paires monitor/témoin partagent des tours et des plages d’horloge d’hôte mais utilisent des pilotes PipeWire distincts, avec quantums 256 et 1 024. Les coupures du monitor sont attestées et leur cause exacte reste indéterminée. [Audit](architecture.md#état-de-départ-et-audit-omega)
- Les tests synthétiques contrôlent à la fois une paire continue, une paire disjointe et une paire avec piste manquante. Le banc V3 retrouve le décalage de 40 ms injecté entre microphone et sortie sur un cas à deux domaines d’horloge. L’échange réseau asymétrique simulé borne l’offset à un intervalle de 14 ms. La borne physique reste inconnue. [Validation](validation.md)
- Le rejeu par événement, intervalle, fenêtre Turbo ou tour fonctionne hors ligne. La qualification AEC compare le signal inchangé, AEC3 et Speex sur la même fenêtre logicielle. Le Studio ouvre le prévol, la chronologie, les marqueurs, l’intégrité et les résultats dans une fenêtre distincte. Les annotations versionnées et les perturbations reproductibles sont accessibles en CLI. [Rejeu](replay.md)

## Performances et stockage

Le banc de soumission à cinq pistes écrit 8 479 754 octets en 6 s, soit 5,09 Go/h hors témoin et Turbo. Le p95 du pas de soumission passe de 128,17 µs en mode arrêté à 351,38 µs en Deep Analysis. Le CPU cumulé passe de 0,041 à 0,291 s et la perte de diagnostic reste nulle sur ce banc. Les temps de latence du retour, les xruns et l’inférence pendant une partie réelle restent à mesurer. Le budget par session est de 128 Mio pour le diagnostic léger ou 16 Gio avec voix, avec une réserve minimale de 32 Mio. L’espace global est consultable et le nettoyage est manuel. L’expérience FLAC restitue le PCM16 exactement, avec un débit et un gain dépendants du contenu. [Mesures et conditions](validation.md#charge-et-stockage-du-banc-synthétique)

## Limites opérationnelles

Le navigateur et le système mobile peuvent traiter le signal avant que Kara le reçoive. La chaîne actuelle ne fournit pas de piste indépendante `MIC_RAW` avant traitement mobile ni de sortie AEC séparée `MIC_AEC`. Les métriques acoustiques objectives exigent un témoin propre et une phase où le joueur ou l’écho peuvent être isolés. Le banc AEC n’évalue pas la préservation vocale sur une capture dépourvue de vérité terrain. Le comparatif PipeWire Echo Cancel et les stages CTC ou phonétiques restent à intégrer au rejeu modulaire.

Le prévol indique l’état logiciel et les vérifications à faire sur un premier tour. L’aperçu GTK suit le journal et les rapprochements AEC utilisent les horloges des sources. L’écoute A/B se fait sur demande par export des WAV privés. Les annotations d’intervalle se font en CLI. Le protocole de calibration reste explicite et le rejeu vérifie le reçu face à la configuration de sortie enregistrée. Les délais physiques du Bluetooth, du retour, de la télévision et de l’ADC/DAC requièrent des mesures matérielles. [Première partie laboratoire](protocole-physique.md)

La stabilité d’un rejeu exact ne garantit pas l’égalité de toutes les probabilités de Turbo. Sur la répétition CPU du chirp synthétique, la probabilité de non-parole varie de 0,00002. Le segment, le token et les temps observés restent identiques. Aucun WER sur du chant réel n’a été mesuré.

## Déploiement

Le code `a895850` est validé sur la branche locale. Le premier script de sauvegarde associait le mauvais utilitaire d’empreinte à un chemin de capture. La correction a été reprise avant toute mutation du service. Le contrôle suivant a détecté une session USB en cours. Au dernier relevé, 788 opérations sur 10 934 et 2 115 474 356 octets sur 23 953 928 273 étaient traités. Le transfert interdit le redémarrage pendant cette opération.

Le service reste actif sous le PID 1961547, qui détient le nom D-Bus et l’inhibiteur de capot. La salle était en attente, capture arrêtée. Le client mobile servi appartient à l’instance précédente et la TV installée conserve sa version. Trois sauvegardes SQLite cohérentes ont été prises à chaud avec permissions `0600`. Elles sont dans `.runtime/blackbox-v3/deployment-20261126-173315/` et ont passé `quick_check`.

La reprise du déploiement attend la fin de la session USB, la fermeture de son dialogue et une salle au repos. Le script local `.runtime/blackbox-v3/deploy.py` contient les vérifications, les sauvegardes fraîches, la pause coopérative des imports, le redémarrage et le contrôle de Parole. Aucun redémarrage différé n’est programmé. Aucun enregistrement réel ni calibration sonore n’a été déclenché pendant le chantier.

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
