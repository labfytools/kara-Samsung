# Import d’albums dans Paroles Strawberry

Le tableau de bord et la maintenance sont documentés dans [Parole V3](parole-v3.md).

## Déplacement du dépôt sur ArchASP

Le commit `2201869` a déplacé `kara` dans le dépôt dédié. Les appels de tri et de recherche de pochette utilisaient déjà le répertoire de `kara`, mais `musique-tri` et `musique-pochettes` sont restés dans `~/.dotfiles/bin/.local/bin`, accessible sur ArchASP par `~/.local/bin`. Ces deux exécutables n’avaient pas d’historique dans Git. Les copies datées du 22 novembre dans `~/.local/state/kara-tri-paroles-*` corroborent l’origine du script de tri. Les scripts locaux ont été repris dans `bin/`, avec des adaptations ciblées pour les chemins et la reprise.

| Emplacement audité | Usage actuel |
| --- | --- |
| `bin/musique-tri` et `bin/musique-pochettes` | Exécutables intégrés au dépôt. Le tri vérifie les tags avec FFprobe, organise par artiste et album, et rapporte les destinations. La recherche de pochette privilégie une pochette existante, puis une image intégrée ou vérifiée dans les catalogues. |
| `~/.local/bin/paroles-musique` | Le bouton de recherche de paroles de la piste utilise désormais le script du même dépôt que la fenêtre. |
| `~/.local/share/kara/catalogue/bin/python` | Environnement externe recherché dans `$XDG_DATA_HOME/kara`, puis dans `.runtime/kara-data`. Le Python courant est utilisé si aucun environnement local n’est trouvé. `KARA_CATALOGUE_PYTHON` permet d’indiquer un interpréteur par chemin absolu. |
| `/home/Partage/Music` | Sur ArchASP, `~/Music` pointe déjà vers ce dossier. La racine est déduite de `KARA_MUSIC_LIBRARY` si renseigné, du dossier musique XDG s’il est activé, puis de `~/Music`. Elle est résolue avant les vérifications et les écritures. Le sous-dossier `A_trier` doit exister. |
| `~/.local/share/applications/fr.labfy.ParolesStrawberry.desktop` | `bin/installer-parole` régénère le raccourci dans `$XDG_DATA_HOME/applications` à partir de l’emplacement réel du dépôt. Le service systemd utilisateur observé sur ArchASP est une unité transitoire de lancement Sway. |
| `~/Music/Artiste/Album` | Pistes classées, `cover.jpg` et fichiers `.lrc` adjacents aux pistes correspondantes. Strawberry conserve sa base dans `$XDG_DATA_HOME/strawberry/strawberry`, consultée en lecture seule par Parole. |
| `$XDG_STATE_HOME/kara` | Sauvegardes LRC et journaux d’import par exécution. Le code TV présent sur une installation antérieure reste lisible depuis son ancien emplacement. |

Les liens de compatibilité présents sur ArchASP et les cibles de runtime auditées étaient valides. Le raccourci et les commandes d’import n’ont pas besoin de ces liens pour trouver les sources du dépôt. Le matériel TV, le runtime Tizen, l’environnement Demucs, les modèles et le binaire DTW restent des dépendances des fonctions qui les utilisent. L’utilitaire de test TV prend désormais la page Tizen dans `tizen/app`.

La capture de l’ancien lot « Résurrection » identifie la playlist `OLAK5uy_kk--W5ImwXmRjXIEGJm0G1-lySInLTvBc`. Le texte visible correspond à `YouTube said: The playlist does not exist.` Le code historique coupait le diagnostic à 220 caractères et ne le persistait pas. Le 24 novembre 2026, une requête de métadonnées de la même playlist a renvoyé les 16 pistes. Un lien de test inexistant a confirmé le classement d’un HTTP 404 de l’extracteur en playlist indisponible. Les 46 pistes des cinq dossiers de réception historiques ont été laissées sur place.

## Dépendances et lancement

La fenêtre demande Python, GTK4 et PyGObject. Les imports utilisent `yt-dlp`, FFmpeg et FFprobe, un profil Firefox utilisable par `yt-dlp` et Pillow pour traiter les pochettes. La recherche par artiste et les liens YouTube Music de type `browse` utilisent un environnement Python avec `ytmusicapi` et `requests`. Installer cet environnement dans `$XDG_DATA_HOME/kara/catalogue` ou renseigner `KARA_CATALOGUE_PYTHON`. Strawberry fournit la lecture MPRIS et le catalogue de la collection locale.

Lancer `bin/paroles-strawberry` directement depuis le dépôt ou par chemin absolu depuis un autre répertoire. Après un déplacement futur, exécuter `bin/installer-parole` depuis le nouveau dépôt pour régénérer le raccourci. Les chemins de bibliothèque et de l’interpréteur du catalogue peuvent être transmis par l’environnement de la session graphique.

## Téléchargement, traitement et reprise

La recherche d’albums renvoie les identifiants du catalogue YouTube Music. `kara` en déduit la playlist et la résout auprès de `yt-dlp` sur YouTube. Le transfert de chaque piste passe ensuite par l’identifiant vidéo exact renvoyé par cette playlist. La fiche du catalogue et la playlist peuvent renvoyer des versions de piste différentes. Pour « Résurrection », les 16 identifiants étaient différents au moment de l’audit. L’import conserve donc la sélection de la playlist. Une playlist inaccessible donne lieu à un diagnostic par album.

### File partagée

Parole et la salle mobile utilisent la même file par bibliothèque. Parole enregistre les lots et les demandes mobiles avant de confirmer leur réception. Dans le parcours V2, la confirmation des droits sur une édition enregistre immédiatement le lot. Les demandes groupées des interfaces antérieures gardent leur validation organisateur. Il n’y a pas de quota en nombre de lots ou d’albums en attente. La recherche de catalogue et les aperçus mobiles gardent leurs propres limites de charge.

Le réglage **Lots simultanés** accepte de 1 à 8 lots et vaut 4 à la première utilisation. **Mettre la file en pause** suspend les nouveaux départs. Les lots actifs peuvent se terminer. Diminuer la concurrence laisse également les lots actifs se terminer. Le panneau d’import et la salle indiquent les lots actifs ou en attente, les albums terminés, les pistes traitées, les erreurs et l’avancement. La progression globale répartit le travail par album. Les erreurs restent visibles séparément.

Chaque lot travaille sur un album à la fois. Les lots partagent trois places de téléchargement avec conversion, deux places de préparation de catalogue et deux places de traitement. Chaque lot soumet au plus deux téléchargements à la fois. Le téléchargeur utilise un fragment par piste et limite le débit de chaque transfert à 512 Kio/s. La recherche de pochettes et de paroles s’effectue hors du verrou global. Le tri, la publication et la vérification de la version du fichier au moment de l’écriture utilisent ce verrou. La temporisation par fournisseur est partagée entre les processus. L’import conserve les mécanismes d’accès existants aux sources.

La file exige 64 Mio libres dans l’espace d’état. L’admission d’un import audio conserve 2 Gio libres dans la bibliothèque, avec 1,5 Gio de marge supplémentaire pour les transferts et leurs fichiers temporaires. Le moteur surveille l’espace pendant les transferts et limite la taille annoncée d’une piste à 256 Mio. Les lots en attente restent enregistrés lorsque la bibliothèque manque d’espace.

### Reprise et diagnostics

L’état est enregistré dans une base SQLite privée sous `$XDG_STATE_HOME/kara/import-queue`, dans un sous-dossier propre à la bibliothèque. Fermer Parole suspend les départs. Les processus actifs gardent leurs réservations et peuvent terminer leur travail. Au redémarrage, les lots interrompus redeviennent éligibles à la reprise, selon la pause et les limites enregistrées. Les résultats conservés déclenchent l’actualisation de la bibliothèque lorsque Parole retrouve la file.

Les transferts utilisent des dossiers privés et stables sous `A_trier`. Les brouillons restent dans `.download`. Le tri vérifie les tags et enregistre les destinations avant de publier l’audio. Après une interruption, le moteur vérifie la vidéo source et la position des pistes publiées, réutilise les fichiers complets déjà reçus et reprend les pistes manquantes. Il reprend séparément l’enrichissement lorsque l’audio est complet. Une pochette ou un LRC existant est conservé. L’écriture des paroles intégrées vérifie les métadonnées et l’identité des flux audio et image avant publication.

Dans Parole, **Détails** ouvre le diagnostic sélectionnable et donne accès à **Ouvrir le journal complet**. **Relancer les échecs** reprend le même lot en conservant ses albums terminés. Les journaux privés restent sous `$XDG_STATE_HOME/kara/imports` (`~/.local/state/kara/imports` par défaut). Un lien enregistré dans un ancien journal peut être repris avec `kara --reprendre JOURNAL -- LIEN`. Pour plusieurs liens, utiliser `kara --reprendre JOURNAL --suivi-json --lot LIEN1 LIEN2`. Un journal historique commence au lancement d’un lot. Les demandes rejetées avant lancement et la file en mémoire d’une ancienne fenêtre n’y figurent pas. Laisser une ancienne session vider sa file avant de la fermer et de rouvrir Parole.

**Historique** ouvre les lots clos après confirmation du catalogue. Les détails d’un lot partiel présentent les albums en échec ou en attente. Une erreur d’indexation conserve la demande dans la file et ses reçus permettent de relancer la vérification.

### Dossiers et archives ZIP

Dans **Importer un album → Fichiers locaux**, choisir un dossier contenant des fichiers audio déjà téléchargés, ou une archive ZIP. Ce parcours accepte les sous-dossiers de disques et demande des tags de titre, d’album et d’artiste. Les sources doivent rester accessibles pendant l’import et sa reprise. En ligne de commande, `bin/kara -- file:///chemin/edition.zip` utilise le même moteur de traitement.

La file copie les pistes dans le dossier de réception, vérifie qu’elles se décodent, puis les classe sans réencodage. Les sources restent intactes. Un LRC UTF-8 utilisable, nommé comme son fichier audio, et un `cover.jpg` non ambigu sont publiés avant la recherche en ligne si la bibliothèque n’en a pas déjà. L’enrichissement et l’actualisation Strawberry suivent le parcours partagé. Le résultat du lot conserve la provenance locale, le membre d’archive ou chemin relatif et la somme SHA-256 du fichier source.

Les reçus vérifient le contenu audio publié avant de sauter une piste lors d’une reprise depuis la même sélection. Une erreur sur une piste laisse les autres disponibles. Les liens symboliques, noms ZIP qui sortent de l’archive, entrées chiffrées et répertoires ZIP trop volumineux sont écartés. Les limites de lecture sont de 4 096 entrées et 2 Gio par piste. La réserve disque de l’import s’applique aux copies. Les défauts de la sélection locale restent à vérifier localement.

## Diagnostic des échecs persistants

La tentative initiale appartient au moteur d’import. Après l’enregistrement de son résultat et l’actualisation du catalogue, Parole peut planifier jusqu’à deux relances classiques pour un échec temporaire. Ces relances attendent 60 secondes puis 240 secondes au minimum, ou le délai signalé par le fournisseur s’il est plus long. Plusieurs échecs comparables peuvent différer leurs relances automatiques de 30 minutes. Les autres lots gardent les règles de la file. Les compteurs et échéances survivent à l’arrêt de Parole. La commande manuelle reste disponible dans l’import.

Un échec persistant peut ensuite rejoindre le quota Codex commun si la catégorie **Échecs d’import** est activée. Une signature conserve la catégorie, l’étape, le fournisseur et les versions utiles. Les défauts dus à une version ou à une réponse d’API peuvent partager un diagnostic. Une playlist ou une édition inaccessible conserve sa propre identité. Parole transmet des faits typés et expurgés, dont le code de retour s’il est disponible. Le journal privé complet, les chemins locaux et les paramètres d’authentification restent locaux. Les limitations de requêtes, le manque d’espace, les permissions et les interruptions volontaires donnent lieu à une attente ou à la vérification locale.

Un résultat Codex peut suggérer une autre adresse de catalogue. La relance automatique n’a lieu qu’une fois, après confirmation indépendante de la réponse du catalogue et de l’identité de l’édition déjà autorisée. La file conserve le même lot, la sélection et les reçus des pistes existantes. Une édition différente demande une validation. Les diagnostics de code peuvent donner lieu à une ébauche et à des tests simulés dans l’historique Codex. Leur intégration au programme et leur exécution demandent une revue explicite. Si Codex est indisponible, si aucune correction n’est confirmée ou si la tentative corrigée échoue, le lot reste **À vérifier**.

## Validation

```bash
python3 tests/test-parole-import.py -v
python3 tests/test-parole-import-recovery.py -v
PAROLE_TEST_GTK=1 python3 tests/test-parole-import.py -v
```

La suite crée une bibliothèque temporaire avec un son de synthèse. Elle utilise `yt-dlp` et FFmpeg sur une source HTTP locale et contrôle les réponses du catalogue, des pochettes et des paroles. Elle couvre 100 lots totalisant 500 albums, quatre lots actifs, le verrou de tri, la reprise après interruption, les doublons et le manque d’espace. Les tests GTK facultatifs utilisent une session GTK et D-Bus. Ils exercent le journal, la relance sélective, la file commune avec la salle mobile et l’actualisation de la bibliothèque.

Les essais locaux vérifient l’identité PCM de pistes FLAC, le maintien des sources, la conservation octet pour octet du LRC et de la pochette fournis, le refus d’un membre ZIP hors chemin et la reprise d’une seule piste dans un lot partiellement publié.
