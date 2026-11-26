# Indexation Parole V2

## Réconciliation de la bibliothèque

L’index Parole reste dans `maintenance.sqlite3`, en WAL. Strawberry fournit des données de collection en lecture seule. Le tri indexé d’OMEGA I et les preuves de provenance d’OMEGA II sont conservés. [OMEGA I](omega/rapport.md) · [OMEGA II](omega-ii/rapport.md)

La réconciliation compare les versions du fichier audio, des fichiers associés et du cache Kara. Un fichier associé est relu lorsque sa version change. La lecture bornée vérifie la version avant et après. Le cache de lecture distingue le dernier repère LRC non vide et la présence de texte. La durée du morceau reste un critère de validation du repère. L’index conserve ses pistes inchangées sans écriture et vérifie les disparitions sur un parcours complet. L’identité du répertoire racine et le périphérique des fichiers historiques protègent contre un point de montage indisponible. Les erreurs de parcours suspendent la suppression.

La console `INDEX` indique le résultat et la durée de chaque réconciliation. Les mesures de l’indexeur ventilent le parcours, les versions de fichiers, Strawberry, les métadonnées, FFprobe, les fichiers associés, le LRC, le cache de préparation, les écritures et l’attente avant transaction. Le chronométrage des métadonnées inclut FFprobe quand il est appelé. La transaction inclut le temps d’attente pour son ouverture. Les empreintes acoustiques relèvent des traitements d’import et de préparation existants.

## Premier relevé apparié

La bibliothèque est celle de production, consultée en lecture seule. Les écritures ont utilisé des sauvegardes SQLite privées de 13 880 pistes présentes. Les deux passages ont utilisé le même cache Kara et la collection Strawberry de l’hôte. Les chiffres sont deux passages individuels et leur écart inclut les effets du cache système et de la charge concurrente.

| Mesure | Référence `a895850` | Réconciliation V2 |
| --- | ---: | ---: |
| Inventaire | 4,90 s | 2,01 s |
| Pistes inchangées | 13 880 | 13 880 |
| Appels à FFprobe | 0 | 0 |
| Appels de lecture des fichiers associés, y compris les absents | 27 760 | 0 |
| Appels au parseur LRC | 27 760 | 0 |
| Contrôles du cache Kara | 13 880 | 13 880 |
| Réécritures de pistes | 13 880 | 0 |
| Transactions d’inventaire | 56 | 1 |

La première transaction V2 enregistre l’identité du stockage. Les passages suivants à inventaire égal et sans erreur n’ouvrent aucune transaction d’écriture. Une planification intégrale forcée a pris 0,57 s sur la référence et 0,54 s sur V2. V2 omet cette phase après une réconciliation stable si la politique de maintenance est déjà appliquée.

## Livraison persistante

Le résultat détaillé du tri fournit les tags déjà vérifiés sur la version publiée, avec son instant de publication. L’import enregistre les chemins et leurs preuves dans la base Parole. L’admission des enrichissements couvre aussi les chemins qui viennent d’un reçu de reprise et ceux qui n’ont pas ce détail. Un chemin est unique dans la file, et les notifications combinent les types de changements. La fenêtre démarre à 250 ms, s’étend au maximum jusqu’à 1,5 s lors d’une rafale et s’ouvre dès que 64 chemins attendent.

L’indexeur vérifie le périphérique, l’inode, la taille et les temps à la nanoseconde avant de réutiliser les tags. Il publie les pistes, planifie leur maintenance et accuse réception dans la même transaction WAL. Un événement plus récent conserve son numéro de livraison. Une lecture échouée ou une mutation concurrente diffère la livraison. Le chemin de secours reste la réconciliation. Lorsqu’une écriture de paroles prouve que tous les flux sont inchangés, l’identité PCM existante reste valable pour la nouvelle version. Les références Kara conservent leurs propres vérifications.

Le thread d’indexation appartient au bail de maintenance du service. Il traite des lots bornés pendant que les opérations réseau suivent leur propre cours. Le catalogue de la salle fusionne les chemins publiés. Un album importé ne demande plus d’inventaire global de Parole. Strawberry garde son mécanisme de surveillance et sa base en lecture seule.

## Changements externes

Un descripteur inotify surveille les répertoires de la bibliothèque, sous la limite de 16 384 dossiers et d’un huitième du quota utilisateur exposé par le noyau. La réception est bornée par tour. Un nouveau dossier est parcouru localement, et un déplacement est rapproché par son cookie. Un débordement, une perte de notifications ou un manque de descripteurs est indiqué dans la console INDEX et déclenche une réconciliation. Une disparition éphémère du stockage conserve les lignes historiques. [Sémantique du noyau Linux](https://man7.org/linux/man-pages/man7/inotify.7.html)

Pendant une partie Kara, l’indexeur diffère ses tours. Pendant la lecture Strawberry, les événements avec preuve et les mises à jour des fichiers associés restent éligibles. Les conciliations et les nouveaux appels FFprobe attendent la fin de la lecture. Le fil d’indexation et son processus FFprobe éventuel réduisent leur priorité CPU sous Linux.

Le code amont de Strawberry regroupe les dossiers modifiés, attend deux secondes après la dernière notification, puis effectue une lecture ciblée de ces dossiers. En cas de débordement il répare les watches et prévoit un inventaire. Parole laisse cette coordination à Strawberry et n’écrit jamais dans sa base. [CollectionWatcher upstream](https://github.com/strawberrymusicplayer/strawberry/blob/master/src/collection/collectionwatcher.cpp)

## Vue Parole et statistiques

La vue virtuelle demande une page, les artistes, les branches ouvertes et les agrégats sur le lecteur de fond existant. Chaque réponse est liée à la recherche et aux filtres qui l’ont demandée. La vue garde les lignes stables, la sélection et les branches ouvertes. La console charge aussi la synthèse des enrichissements en fond. Le navigateur de lecture reçoit les chemins publiés et conserve son filtre quand ils arrivent.

Les compteurs de pistes, paroles, LRC, octets et préparations restent dans `library_stats`. Des déclencheurs appliquent les deltas aux artistes et aux éditions dès l’écriture de la piste. Les indices partiels couvrent les erreurs média et les brouillons. Parole vérifie les agrégats au premier passage complet, puis une fois par jour lorsqu’une réconciliation est possible. Un écart est signalé dans INDEX.

La cadence consomme 64 événements par transaction, jusqu’à 128 quand au moins 256 chemins attendent et que les ressources le permettent. La pression mémoire, l’activité CPU et disque récente et les préparations en cours déclenchent le mode léger. La queue d’événements reste durable pendant ce report. Les tâches d’import, de préparation et de preuve PCM gardent leurs mécanismes de reprise et leurs limites.

## Banc et limites de mesure

Le [banc exécutable](../tests/benchmark-parole-index.py) fait travailler le véritable inventaire sur des FLAC synthétiques de 0,5 s et une collection Strawberry simulée. Les gros inventaires tirent leurs tags de cette collection. Les publications qui simulent l’import fournissent une preuve versionnée comme le trieur de production. Les timings d’admission groupée partent du moment où l’audio est disponible et omettent le téléchargement, le tri et le temps de regroupement. Les séries solo 1, 10 et 100 modélisent autant de fins de lots historiques séparées. Une fin de lot historique pouvait contenir plusieurs albums.

| Cas | Référence | V2 | Fichiers vérifiés, référence → V2 | FFprobe, référence → V2 |
| --- | ---: | ---: | ---: | ---: |
| 1 admission de 10 pistes | 0,84 s | 0,039 s | 2 510 → 10 | 10 → 0 |
| 10 admissions de 10 pistes | 11,71 s | 0,36 s | 26 100 → 100 | 100 → 0 |
| 100 admissions de 10 pistes | 127,30 s | 3,70 s | 361 000 → 1 000 | 1 000 → 0 |
| 1 000 pistes en un lot | 45,71 s | 0,73 s | 4 610 → 1 000 | 1 000 → 0 |
| 1 000 LRC associés à des pistes existantes | 0,68 s | 0,74 s | 4 610 → 1 000 | 0 → 0 |
| 1 000 fichiers retagués en externe | 44,75 s | 47,52 s | 4 610 → 1 000 | 1 000 → 1 000 |
| Déplacement d’un album de 10 pistes | 0,97 s | 0,042 s | 4 610 → 10 | 10 → 0 |
| 50 000 pistes inchangées | 6,25 s | 3,64 s | 50 000 → 50 000 | 0 → 0 |
| 100 000 pistes inchangées | 14,67 s | 8,79 s | 100 000 → 100 000 | 0 → 0 |
| Construction à 100 000 pistes | 37,32 s | 32,74 s | 100 000 → 100 000 | 0 → 0 |

Les deux tailles passent `quick_check` et conservent `songs_display` pour la première page. À 100 000 pistes inchangées, le moteur passe de 392 commits d’inventaire à zéro. Le temps CPU de ce passage est de 8,65 s puis 8,58 s. La différence de temps mural inclut les écritures et la charge de fond. La passe de 100 admissions consomme 26,34 s de CPU Python et 49,81 s de CPU enfant dans la référence, contre 1,39 s et zéro en V2. Elle effectue 1 602 commits d’inventaire dans la référence et 100 commits de traitement dans V2, hors admission de ses reçus. Les compteurs `/proc/self/io` passent d’environ 1,43 Go d’écritures à 146 Mo sur ce témoin. Les maxima RSS du banc s’échelonnent entre 33 et 323 Mio selon le lanceur et la phase. Ces maxima processus ne mesurent pas une allocation propre à chaque cas.

Sur 1 024 pistes munies de reçus, les lots de 1, 16, 32, 64 et 128 prennent respectivement 20,95 s, 1,87 s, 1,09 s, 0,67 s et 0,50 s. Ils utilisent 1 024, 64, 32, 16 et 8 transactions de traitement. Sur 24 notifications espacées de 25 ms, le p95 de disponibilité SQLite est de 0,342 s avec la fenêtre de 250 ms, 0,592 s à 500 ms, 1,079 s à 1 s et 2,079 s à 2 s. Les quatre fenêtres utilisent 14 à 15 transactions sur ce flux. Le choix garde 250 ms comme délai usuel et élargit les lots surtout quand la file est dense.

Pour SQLite, le plan ciblé utilise l’index unique de chemin, les déplacements utilisent `songs_inode`, et l’échéance des événements utilise `index_events_due`. Les lectures côté interface utilisent des instantanés WAL brefs. Les lots d’écriture préparent les changements avant d’ouvrir le verrou `BEGIN IMMEDIATE`. La copie du banc conserve `synchronous=2` et l’auto-checkpoint de 1 000 pages. Un checkpoint passif sur la copie stabilisée a renvoyé `(0,0,0)`. Le banc ne modifie ni les paramètres de durabilité ni la base Strawberry.

### Métadonnées

Sur 50 FLAC avec FFprobe, 1, 2, 4, 8 et 16 travailleurs donnent respectivement 15,0, 27,3, 19,0, 11,4 et 16,4 pistes/s. Le coût CPU des enfants monte de 2,55 s avec un travailleur à 3,46 s avec seize. La charge varie pendant ces mesures. Parole garde un seul appel FFprobe simultané dans l’indexeur pour les cas sans preuve exploitable.

TagLib C 2.3.2 lit les cinq formats testés dans le banc Opus, FLAC, MP3, Ogg Vorbis et M4A. Il redonne les crédits du témoin et fournit une durée à la seconde entière par l’entrée C testée, contre la durée fractionnaire utilisée pour qualifier le dernier repère LRC. Ses valeurs sont des listes et ses noms canoniques diffèrent selon le format. Dix lectures TagLib prennent quelques millisecondes par format, contre environ 0,4 à 0,5 s avec dix lancements FFprobe. Mutagen annonce la prise en charge des cinq formats et le Python du service ne l’installe pas. La production réutilise les tags du trieur ou de Strawberry et conserve FFprobe comme secours. Le retagage externe massif reste limité par ce secours. [TagLib](https://taglib.org/) · [Mutagen](https://mutagen.readthedocs.io/en/latest/)

Une empreinte acoustique utilisée pour qualifier un fournisseur ou une édition reste à la charge de la vérification d’import existante. L’indexation n’ajoute aucun calcul d’empreinte et une preuve de copie des flux peut préserver l’identité PCM au retagage. Les résultats du banc portent sur l’inventaire et la livraison. Ils ne mesurent ni le débit d’un fournisseur distant, ni la latence de l’application Strawberry, ni une partie Kara physique.

Sous Sway headless et D-Bus isolés, une publication sur fixture est devenue visible dans la liste GTK **0,441 s** après admission. La mise à jour des modèles GTK a pris **0,82 ms** pour ce passage. Le test a également vérifié la conservation de l’objet de ligne lors d’une mise à jour de provenance, la sélection, le regroupement d’artistes et les parcours d’import et de synchronisation USB simulée.

### Transferts en parallèle

Le mode `overlap` du banc écrit et synchronise quatre réceptions HTTP locales de 4 Mio chacune, avec et sans l’indexation de 60 publications. Dans la référence, le débit régulé de ce serveur local est de 19,95 Mio/s seul et 20,35 Mio/s avec indexation. En V2, il est de 20,22 et 20,34 Mio/s. Le témoin ne montre pas de baisse mesurable. La fin des transferts et de l’indexation passe de 3,95 à 0,79 s, avec 60 appels FFprobe évités. Un passage par condition et un débit régulé en boucle locale ne permettent pas de conclure sur le rendement d’un fournisseur réseau.

## Déploiement et vérification

Le 26 novembre 2026, un premier redémarrage a réutilisé le raccourci pointant vers l’instantané isolé BlackBox. La mise en service V2 a conservé cet instantané, puis pointé le raccourci vers un checkout détaché. Les fichiers de BlackBox du premier instantané sont identiques à ceux de la version livrée. Un rejeu intermédiaire a révélé que la lecture Strawberry ciblée omettait le `rowid` inclus dans la signature de réconciliation. Le contrôle de non-régression exige maintenant un rejeu sans écriture après une réconciliation.

Le service a redémarré sur `d42b60f`, PID **2348303**, à 18 h 01 CET. Le fil `parole-index` est actif et D-Bus appartient à ce PID. Parole a été rouverte et ramenée au premier plan sous Sway. Le service est resté actif sans redémarrage automatique ni erreur de niveau `err` au contrôle. La salle était en attente, la capture BlackBox et l’USB étaient inactifs et Strawberry était en pause. La file d’import a été suspendue coopérativement avant le redémarrage et remise à son état initial `running`.

Les sauvegardes SQLite privées de maintenance, d’imports et de Strawberry sont dans `.runtime/parole-index-v2/deployment-final/avant-deploiement/`. Les trois copies ont passé `quick_check`. Le rapprochement avant-après conserve les 13 880 pistes présentes, les 13 201 chemins historiques, les 81 associations manuelles, les 30 lots, les sept manifestes de préparation, les douze manifestes de stems, les deux éléments de reprise et les réglages de nuit.

Le service a consommé un rejeu de 26 chemins issus d’un reçu d’import réel en **0,319 s**. Les 26 étaient inchangés. L’intégrité et la cohérence complète des agrégats ont été vérifiées après le rejeu, et la file d’événements était vide. Les 26 chemins existaient aussi dans la collection Strawberry, configurée avec `monitor=true`. Ce contrôle vérifie la reprise et la concordance de deux catalogues établis. Aucun lot actif ou prêt à partir n’était disponible pour mesurer la publication distante ou l’arrivée d’un nouveau titre dans Strawberry.

## Tests exécutés

- Onze cas de livraison, interruption du processus en écriture, rejeu, fichier corrompu, mutation concurrente, déplacement, statistiques et inotify.
- 36 cas Parole V3 sous Sway headless et D-Bus isolés, dont publication jusqu’à la liste GTK et conservation de la sélection.
- 51 cas d’import couverts par le passage normal et le passage des quatre cas GTK. Huit cas de reprise et treize cas de téléchargement sur serveur local.
- Deux cas de regroupement d’artistes, 21 cas nocturnes et cinq cas de ressources.
- 37 cas de préparation, 15 Kara V1, six d’audition, neuf BlackBox V3 et une synchronisation USB simulée.
- `git diff --check`, intégrité SQLite en fixture et en production, propriétaire D-Bus, activation du fil d’indexation, reprise de la file et présence de la fenêtre.
