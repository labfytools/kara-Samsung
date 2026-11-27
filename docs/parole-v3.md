# Parole V3

Parole ouvre le tableau de bord de la bibliothèque. L’en-tête donne accès à l’importation, à la synchronisation USB, à la connexion TV et aux paramètres. La console affiche les étapes réellement observées. La consultation et la correction des paroles se font depuis les détails d’une chanson. La lecture, la notation, la salle, la télévision, les volumes et BlackBox utilisent le contrôleur Kara existant. Le mode `--service` continue de servir Tizen et les téléphones si la fenêtre Parole est masquée. La maintenance démarre à l’ouverture de la fenêtre Parole.

## Synchronisation USB

Le bouton **Synchroniser** indique l’état du téléphone et ouvre le dialogue Labfy. Parole cherche `scripts/labfy-sync` dans le dépôt voisin `labfy-music-manager`. La variable `LABFY_MUSIC_MANAGER` permet d’indiquer son chemin absolu. Le dossier Android se règle depuis le dialogue ou dans **Paramètres → Général**.

Le branchement du téléphone déclenche seulement la détection USB. Le dialogue présente l’espace libre, les fichiers déjà synchronisés, les ajouts dans les deux sens, les mises à jour, les renommages, les conflits et les suppressions proposées. **Démarrer** envoie au moteur les choix affichés. Les suppressions restent décochées à chaque aperçu. **Suspendre** arrête le transfert et préserve la progression pour **Reprendre**. Après une interruption, **Abandonner le plan** permet de refaire un aperçu en conservant les fichiers déjà publiés. L’indexation incomplète garde sa propre commande **Réparer l’indexation**. La progression affiche les débits instantané et moyen, le volume restant et l’estimation de durée. Le filtre `SYNC` affiche les mesures d’inventaire, le protocole, un échantillon des opérations vérifiées, les erreurs et le bilan de la session.

Labfy copie les fichiers audio, images et LRC en vérifiant leur empreinte SHA-256. Les tags et les paroles intégrées suivent le fichier audio et les corrections locales en `.txt` restent dans leur dossier. Les répertoires masqués sont ignorés. Le moteur écarte des inventaires le dossier `A_trier` et les répertoires techniques XDG de Kara lorsqu’ils se trouvent dans la bibliothèque. Les publications qui ne concernent pas ces dossiers restent disponibles à la synchronisation.

Le moteur contrôle les fichiers du périmètre capturé au début de l’aperçu. Le verrou `.labfy-music.lock` partagé avec les publications d’import, le tri, les enrichissements et les regroupements d’artistes protège les validations et les écritures PC. Les envois et l’indexation Android laissent les publications d’import se poursuivre. Le moteur propage les renommages identifiés avec sa référence commune. Parole lui transmet aussi les déplacements du registre canonique. Un déplacement sans référence USB antérieure exige des chemins non ambigus et un SHA-256 complet identique. Si un ancien chemin Android risque de recréer un fichier sous un artiste regroupé ou de doubler sa copie, Parole signale le chemin à vérifier avant la confirmation.

Le dialogue garde la progression USB et ses débits à l’écran pendant que MediaStore actualise la bibliothèque Android. Les nombres de fichiers reçus et vérifiés sont distincts. Les pistes indexées, celles encore en file et les erreurs apparaissent dans la progression Android. Le journal **SYNC** indique les durées des phases et le chevauchement mesuré. La fin de la copie est visible même lorsque le fournisseur Android traite encore des pistes.

## Possession et persistance

| Données | Emplacement | Récupération |
| --- | --- | --- |
| Index, tâches, sources, exclusions, décisions | `$XDG_STATE_HOME/kara/parole/<empreinte de la racine>/maintenance.sqlite3` | Une instance possède le verrou `owner.lock`. Elle remet en attente les tâches interrompues, dans leurs catégories et avec leurs échéances. |
| Console | `maintenance.jsonl` dans le même dossier | Rotation à 512 Kio, quatre archives, 600 événements dans la console. Le bouton Historique charge les 4 000 derniers événements au maximum. |
| Corrections locales | À côté de l’audio, avec l’ancienne version dans `corrections/<id>` | Le dialogue vérifie les versions avant d’écrire. Restaurer une version se fait dans ce dialogue en recopiant son texte. |
| Imports | File commune dans `$XDG_STATE_HOME/kara/import-queue` | Les reçus de maintenance restent liés au lot existant. Les lots intégrés passent dans l’historique. Les imports partiels et les échecs restent relançables. |
| Associations manuelles | Index Parole et manifestes d’origine dans `$XDG_STATE_HOME/kara/music-tidy-backups` | Les manifestes terminés rétablissent les associations exactes par album. Les crédits de piste et les sauvegardes restent intacts. |

Avec les valeurs XDG par défaut, l’état se trouve sous `~/.local/state` et le cache sous `~/.cache`. Conserver l’index avec son WAL lors d’une copie à chaud ou utiliser l’API de sauvegarde SQLite. Pour intervenir sur les fichiers hors ligne, attendre la fin de la maintenance, des imports actifs et des parties.

L’inventaire prend les chemins et versions du système de fichiers, puis associe les informations de Strawberry en lecture seule. Les métadonnées des fichiers reconnus sont consultées par lots. Les fichiers nouveaux ou dont la version a changé sont sondés si les métadonnées Strawberry ne correspondent plus. Une interruption de l’inventaire conserve les enregistrements qui n’ont pas encore été visités. Les flux PCM sont identifiés au moment des traitements qui en ont besoin. Une empreinte PCM inclut les canaux, le taux et l’ordre des échantillons. Les doublons certains sont filtrables une fois les deux flux identifiés. Les déplacements conservant l’inode, la taille et la date de modification gardent l’identifiant d’index. Les références Turbo et pistes Demucs peuvent être réassociées sur preuve d’identité. Les caches antérieurs sans preuve PCM gardent leurs règles d’admission historiques.

Avant la recherche externe, Parole consulte les références locales des pistes dont les empreintes PCM connues sont identiques. Un texte issu d’un fournisseur vérifié ou une référence acoustiquement vérifiée peut être repris. Une correction manuelle retenue pour ce parcours porte un reçu qui correspond aux versions actuelles de l’audio et du fichier de paroles. Des références divergentes conduisent à poursuivre la recherche. La préparation peut emprunter la référence sans écrire dans la bibliothèque. La maintenance crée uniquement le fichier TXT ou LRC absent, avec la provenance **Bibliothèque · PCM identique**. Elle conserve le conteneur musical et distingue ces publications des résultats obtenus auprès des fournisseurs. Ce parcours s’applique aussi entre deux éditions dont l’identité PCM est établie.

Un fichier audio ou un texte intégré remplacé ne reprend pas la provenance fournisseur de l’ancienne version. Un changement du fichier LRC associé périme aussi sa provenance. Les fichiers restent consultables à leur emplacement habituel.

La même file traite les pistes qui partagent une empreinte PCM indexée et celles dont le texte intégré contient déjà un minutage exploitable. Le parcours local vérifie les versions et la provenance avant de publier un fichier associé absent. Une preuve insuffisante laisse la tâche éligible à la recherche externe et attend une évolution de l’index avant un nouveau contrôle local. Une opération locale et un lot réseau alternent lorsque les deux sont prêts. Les opérations locales restent disponibles lorsque les fournisseurs ont épuisé leur quota.

L’index SQLite `songs_display` suit l’ordre des lignes de la liste. Sur une copie des 13 201 pistes ArchASP, la première page de 80 pistes a pris 0,37 ms en médiane, contre 16,16 ms avant l’ajout. Avec le terme `love`, le temps de sélection de cette page a diminué de 15,25 à 2,49 ms. Une recherche sans résultat requiert toujours de parcourir l’index. Les vingt mesures alternées par requête ont renvoyé les mêmes lignes dans le même ordre. L’index occupe environ 1,23 Mio. Ces mesures couvrent la requête paginée, indépendamment du comptage des résultats et du rendu GTK.

## Ordre de travail et limites

Le propriétaire GTK coordonne les imports, la lecture Strawberry et les recherches de Parole. La maintenance revendique une tâche SQLite à la fois et répartit les tours de recherche entre paroles, LRC, albums, discographies, pochettes et sources. Les opérations réseau conservent leur échéance et une temporisation progressive. Les fournisseurs ont des quotas et une temporisation persistants. La file d’importation conserve sa concurrence configurable de 1 à 8 lots, 4 par défaut, et ses réservations partagées.

| Réglage initial | Valeur |
| --- | --- |
| Inventaire | Au lancement, puis toutes les 15 min |
| Vérifications éditoriales | Tous les 30 jours après une vérification |
| Opérations réseau classiques | Quotas et intervalles par fournisseur. Jusqu’à 145 s par recherche par défaut. |
| Inactivité Sway | 300 s, sur secteur par défaut |
| Admission lourde | CPU <25 %, GPU ≤15 %, activité disque <25 %, température <78 °C, mémoire disponible ≥4 Gio |
| Surveillance en cours | Événement de reprise Sway, alimentation, activité multimédia, partie ou import prioritaire, pression mémoire, réserve mémoire de 1,5 Gio, température et stockage |
| Stockage | 50 Gio de cache et au moins 20 Gio libres sur chaque partition utile, avec marge temporaire par morceau |

### LRC nocturnes

**Paramètres → LRC nocturnes** active la recherche des LRC d’origine manquants dans la file existante. Le [service utilisateur](../contrib/systemd/user/kara.service) la supervise sans fenêtre. Avec la session verrouillée, Parole récupère un fichier à la fois. Les tâches d’album, de métadonnées et de paroles simples sont reprises dans leur parcours de jour.

LRCLIB, NetEase, QQ Music et les adaptateurs compatibles explicitement validés peuvent fournir des LRC. La recherche vérifie la correspondance du morceau puis enregistre le texte minuté d’origine en UTF-8, uniquement si le fichier est absent. Les résultats négatifs ont une expiration. Quand une source atteint son quota, les autres restent utilisables. Les échéances SQLite et les délais renvoyés par les fournisseurs permettent de reprendre sans recommencer la file.

Le tableau indique le nombre de morceaux, les LRC présents, les LRC manquants, les acquisitions de la nuit, l’état des fournisseurs et la prochaine échéance. Le [mode capot fermé](parole-capot-ferme.md) garde les contrôles de secteur, de verrouillage, de température, de mémoire et de stockage.

L’album est comparé aux positions disque et piste et aux titres de l’édition MusicBrainz. Une correspondance par titre seule est insuffisante. La discographie utilise un MusicBrainz ID inscrit dans le fichier ou corroboré par des éditions locales distinctes. Les ambiguïtés, doublons et positions absentes sont consultables. Pour un morceau dont les métadonnées sont incomplètes, la maintenance peut joindre une identification MusicBrainz aux détails après recoupement du crédit, de l’édition et de la durée. Les suggestions restent séparées des tags et crédits originaux. Lorsque la récupération classique d’une piste manquante est indisponible, la recherche approfondie reçoit les positions et titres réellement manquants pour l’édition. L’import automatique requiert une édition vérifiée et un lien YouTube Music expressément autorisé dans son dialogue. Les pistes déjà reconnues par la file sont réutilisées. Les exclusions persistent. Les champs de droits ou les liens proposés par Codex ne valent pas autorisation.

## Codex et fournisseurs

Codex est désactivé au départ et utilise l’authentification ChatGPT existante après activation explicite dans Paramètres. Une recherche est limitée à 120 s et à deux départs locaux par jour par défaut. Le quota distant restant n’est pas mesurable dans Parole. Un signal de quota distant suspend les nouveaux départs pendant une journée. La recherche est lancée avec `codex exec`, une sortie JSON structurée, un espace jetable Bubblewrap et les fonctions d’exécution de commandes désactivées. Le seul fichier d’authentification monté est en lecture seule. L’audio, BlackBox, les répertoires personnels et les dépôts sont absents de l’environnement.

Un résultat est recevable si la CLI rapporte une recherche Web terminée avec des résultats et si la réponse respecte le schéma. La CLI autonome 0.172.0 utilise `codex-code-mode-host`, monté séparément en lecture seule. La recherche automatique est désactivée par défaut et la durée maximale est configurable de 15 à 600 s. Les résultats restent consultables dans Paramètres → Codex. Les [échecs d’import persistants](parole-imports.md#diagnostic-des-échecs-persistants) consomment le même quota et utilisent les mêmes règles d’isolation. Aucun texte ou contenu inventé n’est injecté dans la bibliothèque. Une ébauche d’adaptateur peut être sollicitée depuis Sources. Le texte de code, ses tests et ses fixtures sont visibles dans le résultat Codex pour revue. Leur exécution et leur intégration demandent une validation séparée.

Les sources enregistrées sont consultables, désactivables et mesurées pendant les opérations. La désactivation arrête les nouvelles recherches utilisant cette source ainsi que les nouveaux imports automatiques qui en dépendent. Une source compatible LRCLIB reçoit une configuration avec `host`, `path`, `terms` et `fixtures`. Chaque fixture indique `title`, `artist`, `album`, `duration`, éventuellement `album_artist`, `synced` et `sha256`. La valeur `sha256` vaut `null` pour un résultat attendu négatif, ou l’empreinte du texte vérifié pour un positif. Il faut au moins deux positifs et un négatif indépendants. Après confirmation que les conditions permettent les requêtes automatisées, Parole vérifie ces cas en arrière-plan. En cas de succès, la source est activée et les échecs de paroles ou de LRC redeviennent progressivement éligibles. Le format accepte uniquement l’adaptateur approuvé et ses paramètres JSON. Les destinations configurables sont publiques, les redirections sont refusées et chaque résolution DNS est épinglée à des adresses autorisées avant l’établissement de TLS.

Dans **Paramètres → Sources**, les limites du fournisseur sélectionné règlent son quota de requêtes HTTP de maintenance et son intervalle minimal. Le quota initial est de 200 requêtes par jour et le réglage de l’intervalle peut renforcer le minimum du client ou du fournisseur. Un quota de zéro suspend les nouvelles requêtes. Les recherches classiques et celles précédant Turbo partagent ces limites par fournisseur. Les anciens réglages globaux de quota et d’intervalle des recherches classiques ne sont plus utilisés. LRCLIB traite les requêtes une par une, avec une pause après réponse et une temporisation qui respecte `Retry-After`. En cas d’erreurs répétées, le délai augmente pour le fournisseur concerné. [Documentation LRCLIB](https://lrclib.net/docs) · [Politique MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)


Le statut Local, Externe ou Généré du LRC apparaît dans le tableau. Les détails indiquent la provenance, la couverture si mesurée et les éventuels échecs. La console prend ses messages dans les événements des opérations, et ne recopie pas les détails des processus Codex.

Le 26 novembre 2026, un essai réel a pris 16 copies de titres sans LRC dans la file, dont 8 également sans texte. LRCLIB était le seul fournisseur activé. Les 24 recherches ont pris 35,83 s en 7 lots, pour 48 requêtes HTTP. La vérification a rejeté 3 correspondances ambiguës et 21 recherches se sont terminées sans résultat. Aucune parole ni aucun LRC n’a été publié sur cet échantillon. Les mesures couvrent ce lot court. Le débit par heure de production s’observe dans le compteur après déploiement.

Les détails de chaque fournisseur montrent ses 128 dernières observations au maximum. Le 95e percentile s’affiche à partir de 20 mesures. Un refus LRCLIB pour édition différente signifie qu’un candidat contenait des paroles et avait passé les contrôles de titre, version, crédits et durée. La décision de publication exige toujours une identité suffisamment établie. Une observation regroupe les requêtes d’une recherche et ne contient que sa durée et son résultat.

Les réponses HTTP 401, 403 et 451 des fournisseurs intégrés sont des erreurs de recherche. La temporisation de cinq minutes minimum respecte un `Retry-After` plus long et conserve le statut du fournisseur. Les autres fournisseurs restent éligibles. Les refus se distinguent des résultats absents dans les observations et ne créent aucune entrée négative dans le cache. Le traitement d’un refus d’accès à une piste lors d’un téléchargement reste régi par le contrat d’import.

Une nouvelle interrogation de ces 16 titres a obtenu des candidats par recherche pour 14 pistes. Sur les candidats de la recherche, 88 ont été écartés à cause de l’édition, 21 du titre, 8 des crédits ou de la version et 3 de la durée. Dix candidats avaient une identité compatible, dont six avec un LRC utilisable. Plusieurs candidats peuvent correspondre à une seule piste, y compris des textes divergents. Les recherches essaient désormais le crédit original et une variante typographique du titre si nécessaire. L’acceptation conserve la vérification de l’édition et du minutage. Un LRC valide d’un enregistrement correspondant peut aussi fournir ses paroles brutes.

Le premier passage avec les variantes a effectué 72 requêtes en 55,68 s sur les mêmes copies, sans publication. Aucun des 16 fichiers ne fournissait d’identifiant d’enregistrement MusicBrainz ni d’ISRC exploitable. Deux opérations ont été différées par les limites du fournisseur. Les variantes qui ne diffèrent que par la casse sont désormais écartées avant toute requête.

Pour une recherche classique sans résultat ou ambiguë, Parole peut interroger Unison par ISRC. Il faut une correspondance exacte de l’identifiant, du titre et des crédits. Parole conserve un lien vers le résultat et le crédit du contributeur dans les détails du morceau, avec le quota habituel par fournisseur. Le texte et ses repères restent consultables à la source. Les conditions Jamendo encadrent la conservation hors ligne du contenu. [API Unison](https://docs.betterlyrics.org/unison) · [Licence Unison](https://github.com/better-lyrics/unison/blob/master/LICENSE-DATA.md) · [Conditions Jamendo](https://devportal.jamendo.com/api_terms_of_use)

### Premières mesures après activation

Le 26 novembre, `kara.service` a redémarré à 11 h 10 après les contrôles physiques du capot. La sauvegarde SQLite a passé `quick_check`. Parole a rouvert avec 13 201 pistes et l’inhibiteur de capot armé. La durée maximale effective est de 480 min. Le [relevé capot fermé](parole-capot-ferme.md) décrit la veille manuelle et la restauration après le débranchement.

Entre la sauvegarde avant redémarrage et le relevé de 11 h 23, Parole a publié des paroles LRCLIB et un LRC LyricDown. Le lot représente environ 13 min de production. Les durées et taux ci-dessous portent sur les tentatives de chaque fournisseur et le compteur de publication comptabilise séparément les fichiers ou tags écrits dans la bibliothèque.

| Fournisseur | Tentatives | Réponses exploitables | Erreurs consignées | Durée moyenne |
| --- | ---: | ---: | ---: | ---: |
| LRCLIB | 64 | 5 | 1 | 2,77 s |
| lyrics.ovh | 62 | 0 | 0 | 2,67 s |
| NetEase | 106 | 0 | 1 | 1,93 s |
| QQ Music | 122 | 0 | 0 | 1,18 s |
| Genius | 61 | 0 | 0 | 1,20 s |
| LyricDown | 24 | 1 | 1 | 1,57 s |
| Unison | 0 | 0 | 0 | — |

Les 20 entrées du registre étaient actives, y compris les sources réservées aux pochettes, au catalogue ou aux imports. Unison a répondu sur l’ISRC public de qualification hors production. Les recherches de la première fenêtre de production n’ont déclenché aucune consultation Unison. Le débit sur une heure entière demande une fenêtre de mesure complète en production.

## Qualification

Les fixtures vérifient aussi que la reprise d’activité ou une demande Kara interrompt un sous-processus déjà démarré. Elles vérifient la réserve sur chaque partition, indépendamment de la place totale. Les commandes suivantes exécutent les tests.

```sh
python3 -B tests/test-parole-v3.py -v
python3 -B tests/test-parole-resources.py -v
python3 -B tests/test-parole-night.py -v
python3 -B tests/test-parole-import.py -v
python3 -B tests/test-kara-preparation.py -v
KARA_SILENT=1 python3 -B tests/test-kara-v1.py -v
node tests/test-kara-tizen.js
```

Pour les tests graphiques, utiliser `PAROLE_TEST_GTK=1` sur un bureau ou un compositeur de test avec bus D-Bus dédié. Les essais du 25 novembre 2026 ont utilisé un Sway headless, une bibliothèque temporaire et le mode silencieux. L’échantillon de 80 pistes réelles a été inventorié en lecture seule. Les deux moteurs locaux ont été exécutés sur une copie privée d’un morceau de 22,754 s déjà muni d’un LRC. Aucun son ni microphone physique n’a été utilisé.

`PAROLE_TEST_GTK=1 python3 -B tests/test-parole-sync.py -v` exerce les échanges entre Parole et le moteur Labfy avec le simulateur ADB de Labfy et deux bibliothèques temporaires.

| Mesure observée | Résultat |
| --- | --- |
| GTK dans Sway headless, bibliothèque temporaire | 1,357 s à la première image, 202 Mio RSS et 1,20 % d’un cœur au repos |
| Inventaire réel, 80 fichiers | 0,283 s au premier passage, 0,212 s à chaud, 0 modification au second passage |
| Cache existant mesuré en lecture seule | Environ 307 Mio sur la machine à la date du relevé |
| Fournisseurs publics | MusicBrainz et LRCLIB ont répondu à une requête publique de qualification |
| Codex CLI 0.172.0 | Après montage du compagnon, recherche native avec résultats et réponse conforme au schéma Parole. Documentation publique LRCLIB vérifiée en 15,8 s dans Bubblewrap. |

La qualification ne fixe pas la précision de 500 ms. Elle demande un corpus dont les débuts de ligne et de mot sont annotés indépendamment. Les temps observés sur un morceau court ne donnent pas la moyenne de la bibliothèque. La TV physique, la découverte effective d’un nouveau fournisseur par Codex et une campagne d’importation automatique sur la collection réelle restent à qualifier.
