# Import d’albums dans Paroles Strawberry

Le tableau de bord et la maintenance sont documentés dans [Parole V3](parole-v3.md).

## Publication audio et enrichissement

Un résultat « Audio complet » confirme que les pistes de l’édition sont disponibles. Les imports de la file enregistrent séparément les recherches de paroles et de pochettes dans les tâches persistantes de Parole. Le reçu audio permet de rejouer l’admission après une interruption. Les tâches ont des identifiants stables dérivés des chemins publiés. Les sorties audio partielles suivent la même procédure pour les pistes déjà classées.

Kara intègre les pistes publiées par petits lots pendant l’import. La vérification globale avec la collection Strawberry intervient à la fin du lot. Les recherches repassent en maintenance classique quand le traitement initial laisse des éléments manquants. Les parties Kara interrompent cet enrichissement. Les imports en attente ont priorité sur la préparation lourde, qui retrouve ses tâches après la libération des ressources.

Chaque lot d’enrichissement des paroles utilise une seule lecture initiale des métadonnées par piste. Le moteur privilégie les paroles déjà disponibles, partage la réponse LRCLIB entre le texte et le LRC et vérifie le fichier avant chaque publication. Il conserve les réponses vérifiées sept jours et les absences confirmées vingt-quatre heures dans l’index local. La clé inclut la durée, l’édition et les crédits. Un changement de configuration fournisseur rend les anciennes entrées inaccessibles. Les échecs et les limitations temporaires gardent leur statut propre.

Le propriétaire de maintenance admet quatre recherches de pistes à la fois par défaut et ramène ce plafond à deux en cas de limitation HTTP ou de pression sur les ressources. Les créneaux HTTP, les quotas et les temporisations des fournisseurs restent mutualisés. La sélection exploite les mesures récentes du registre quand suffisamment de réponses ont été observées.

Les profils séparent texte et LRC. Ils utilisent les tags de langue et de genre disponibles, sur cinq observations ou plus. Les pistes sans ces tags utilisent les mesures générales. Des réponses HTTP répétées déclenchent une temporisation croissante. Le délai demandé par le fournisseur est toujours respecté.

Les journaux d’import horodatent les mesures de démarrage, de transfert, de traitement et d’attente. Les durées de sous-processus sont inclusives. Les temps de créneau, d’attente du verrou et de premier octet décrivent des sous-étapes et ne s’additionnent pas au total. La console montre les transferts et recherches actifs et les mesures du dernier lot de paroles. Le débit direct représente les transferts qui ont signalé des octets récemment.

Le trieur lit les tags avant le verrou et vérifie la version de l’entrée sous le verrou. Les pochettes sont décodées et redimensionnées avant la publication. L’actualisation de petites listes de pistes interroge Strawberry par URL et en lecture seule.

Sur ArchASP, un essai isolé avec deux albums synthétiques et quatre transferts HTTP locaux a comparé le parcours géré à la révision `657c291`. Le premier résultat d’album est arrivé en 4,66 s puis 2,23 s. Le dernier est arrivé en 9,07 s puis 4,18 s. L’ancien signal attendait l’enrichissement. Le nouveau confirme la disponibilité audio.

| Recherches de pistes admises | Durée de l’essai | P95 par piste | Requêtes | Textes publiés | LRC publiés |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 11,49 s | 5,25 s | 18 | 16 | 16 |
| 8 | 11,49 s | 9,81 s | 25 | 15 | 16 |
| 16 | 11,34 s | 11,34 s | 33 | 14 | 16 |

Le second essai utilise seize pistes de synthèse, des réponses contrôlées et les vrais créneaux du moteur. Quatre recherches restent le réglage par défaut. Les deux plafonds plus élevés ont presque les mêmes durées globales, davantage de requêtes et des textes différés.

Le parcours complet a été rejoué sur la même partition avec les deux albums, la file de maintenance et les pochettes. L’ancienne version finissait après 9,59 s. Avec l’enrichissement asynchrone, le signal audio du dernier album arrive à 4,40 s et les quatre tâches d’enrichissement finissent après 6,96 s. Les quatre textes et quatre LRC utilisent quatre requêtes communes. Un essai intermédiaire conservant l’attente de réveil entre tâches prenait 11,11 s au total.

## Classement d’une discographie

Lors de la sélection d’un artiste YouTube Music, Parole conserve l’identité de l’artiste et les identifiants d’album obtenus depuis sa discographie. La file garde cette provenance après redémarrage. L’import vérifie l’identifiant d’album pendant la résolution et utilise le nom sélectionné pour le dossier. Le crédit d’album du catalogue et les crédits complets des pistes sont conservés indépendamment.

Une demande de discographie créée depuis la salle conserve la même référence pendant l’attente d’approbation, même si le service redémarre.

La comparaison des graphies ignore certaines différences de casse, accents et ponctuation dans le cadre d’une association déjà validée pour un album. Elle garde les collaborateurs dans la comparaison. Les offres voisines proposées par une recherche textuelle nécessitent leurs propres preuves.

Le moteur de regroupement prévisualise chaque dossier d’édition et son nombre de pistes. Il déplace le dossier entier sous un artiste existant. En cas de collision, il attribue à l’édition déplacée un nom de dossier distinct. Le reçu persistant permet l’annulation ou la reprise après interruption. La file et les références canoniques suivent les chemins déplacés. Les journaux de publication historiques restent lisibles et leurs anciens chemins sont résolus depuis le reçu. Les tags et les fichiers associés sont conservés. Les préparations Kara compatibles sont réassociées par vérification de l’audio et des références.

Dans Parole, le menu contextuel des artistes propose **Regrouper les artistes**. Sélectionner les dossiers, choisir le nom à conserver et ouvrir l’aperçu des fichiers avant de confirmer. **Annuler le dernier regroupement** restaure les dossiers et la règle correspondante après confirmation. L’action est disponible hors lecture et hors import actif.

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

Dans **Importer un album → Fichiers locaux**, choisir un fichier audio, un dossier contenant des fichiers déjà téléchargés ou une archive ZIP. Ce parcours accepte les sous-dossiers de disques et demande des tags de titre et d’artiste. La sélection de plusieurs pistes demande aussi le tag d’album. Les sources doivent rester accessibles pendant l’import et sa reprise. En ligne de commande, `bin/kara -- file:///chemin/edition.zip` utilise le même moteur de traitement.

La file copie les pistes dans le dossier de réception, vérifie qu’elles se décodent, puis les classe sans réencodage. Les sources restent intactes. Un LRC UTF-8 utilisable, nommé comme son fichier audio, et un `cover.jpg` non ambigu sont publiés avant la recherche en ligne si la bibliothèque n’en a pas déjà. L’enrichissement et l’actualisation Strawberry suivent le parcours partagé. Le résultat du lot conserve la provenance locale, le membre d’archive ou chemin relatif et la somme SHA-256 du fichier source.

Les reçus vérifient le contenu audio publié avant de sauter une piste lors d’une reprise depuis la même sélection. Une erreur sur une piste laisse les autres disponibles. Les liens symboliques, noms ZIP qui sortent de l’archive, entrées chiffrées et répertoires ZIP trop volumineux sont écartés. Les limites de lecture sont de 4 096 entrées et 2 Gio par piste. La réserve disque de l’import s’applique aux copies. Les défauts de la sélection locale restent à vérifier localement.

### Bandcamp

Dans le sélecteur de sources, **Bandcamp** propose le site officiel et son aide au téléchargement. Récupérer l’album gratuit ou déjà acheté avec les droits applicables, puis sélectionner le dossier ou le ZIP dans Parole. La page publique de l’édition peut être renseignée pour conserver sa référence dans le résultat. Il s’agit d’une provenance déclarée. Parole ne déclenche aucun achat. L’API publiée documente les comptes, ventes et commandes des partenaires. Le parcours auditeur utilise les fichiers remis directement par Bandcamp. [Documentation de l’API](https://bandcamp.com/developer) · [Récupération des achats](https://get.bandcamp.help/en/articles/15263192-can-i-re-download-my-purchases)

**Toutes les sources** donne accès aux recherches YouTube Music, Internet Archive et Jamendo quand celui-ci est configuré, ainsi qu’au choix de fichiers locaux. Sélectionner un fournisseur pour n’interroger que celui-ci ou afficher son parcours officiel. Les résultats restent identifiés par leur source. Les formats pris en charge par l’import local, dont le FLAC, restent inchangés pendant la copie.

### Qobuz

Sélectionner **Qobuz** après la récupération d’un album depuis **Mes achats**. Le téléchargement depuis le navigateur peut fournir un ZIP FLAC que Parole ouvre directement. Un dossier de fichiers FLAC déjà téléchargés fonctionne aussi. Qobuz propose plusieurs résolutions sans DRM pour les achats. La sélection de la résolution et la récupération des achats se font sur le site officiel. La page publique de l’album peut être enregistrée avec l’import. [Télécharger depuis le navigateur](https://help.qobuz.com/en/articles/369151-tutorial-how-to-download-without-qobuz-downloader) · [Formats des achats](https://help.qobuz.com/en/articles/10167-what-are-the-different-audio-formats-available-for-download)

### SoundCloud

Récupérer le fichier seulement si SoundCloud affiche **Télécharger le fichier** pour la piste. Après le téléchargement, sélectionner **SoundCloud** puis **Fichiers locaux → Choisir un fichier audio**. La page publique de la piste ou de l’ensemble d’origine peut être conservée avec l’import. Une piste dotée d’un titre et d’un artiste peut être classée dans **Sans album** lorsque son fichier n’indique aucun album. Les conditions de l’API de SoundCloud interdisent la conservation permanente des fichiers par une application. [Téléchargement officiel](https://help.soundcloud.com/hc/en-us/articles/115003448787-Downloading-tracks) · [Conditions de l’API](https://developers.soundcloud.com/docs/api/terms-of-use)

### Free Music Archive

Télécharger depuis la page officielle si elle propose le fichier. Dans Parole, sélectionner **Free Music Archive**, renseigner la page publique et choisir la licence affichée, puis importer le fichier. Le parcours accepte CC0 1.0, CC BY et CC BY-SA en versions 3.0 et 4.0. Il conserve le lien et la licence déclarée pour retrouver les conditions et les crédits. Pour un dossier, la page et la licence renseignées doivent s’appliquer à toutes ses pistes. Free Music Archive a fermé son API et réserve l’automatisation de son formulaire de recherche aux intégrations autorisées. [Guide des licences](https://freemusicarchive.org/License_Guide) · [Informations développeurs](https://freemusicarchive.org/app-developers)

### Sites d’artistes et de labels

Sélectionner **Sites d’artistes et de labels** et indiquer la page HTTPS publique où le site officiel propose le téléchargement. Le bouton ouvre cette page dans le navigateur. Choisir ensuite le fichier reçu, le dossier ou le ZIP. Parole conserve le lien, la licence renseignée si elle figure dans le sélecteur et les crédits des fichiers. Le parcours n’interroge pas automatiquement des sites arbitraires.

### Internet Archive

Rechercher par artiste ou titre, ou coller une fiche publique `https://archive.org/details/identifiant`. Parole confirme le type audio, la licence explicite et l’inventaire via l’API de la plateforme. Les éditions publiques avec CC0, CC BY ou CC BY-SA compatibles affichent le nombre de pistes originales, leurs formats et l’accès gratuit. Les résultats qui exigent une authentification, une licence différente ou un fichier marqué privé sont écartés.

Chaque piste originale admissible est transférée avec les limites de la file commune. Le contrôle SHA-1 publié par la plateforme protège contre un transfert tronqué ou altéré. Les fichiers dérivés encodés par la plateforme ne sont pas téléchargés. La file conserve la licence, la fiche publique, l’identifiant du fichier et la somme SHA-256 calculée à la réception. Une piste déjà publiée est vérifiée et réutilisée lors d’une reprise. [API de lecture des fiches](https://archive.org/developers/md-read.html) · [Règles d’accès automatisé](https://archive.org/developers/bots.html)

### Jamendo

L’adaptateur utilise l’API officielle. Renseigner `KARA_JAMENDO_CLIENT_ID` dans l’environnement qui lance Parole avec l’identifiant d’une application Jamendo autorisée. Une fiche d’album est proposée seulement si `zip_allowed` et le champ `audiodownload_allowed` de chaque piste sont vrais et que les licences sont compatibles. Le lien d’une piste demande sa propre autorisation de téléchargement. Les liens d’écoute restent inutilisés. Le moteur demande le FLAC à l’API fichier et vérifie le codec avant le tri. La file conserve l’identifiant Jamendo, la licence, la fiche et le SHA-256 des fichiers reçus. [Authentification de l’application](https://developer.jamendo.com/v3.0/authentication) · [Autorisations d’album](https://developer.jamendo.com/v3.0/albums/tracks) · [Téléchargements des pistes](https://developer.jamendo.com/v3.0/tracks/file)

Sans identifiant d’application, sélectionner **Jamendo** pour ouvrir le site officiel, récupérer un téléchargement autorisé et importer les fichiers avec leur page et leur licence. L’identifiant public que la documentation propose pour les tests de lecture a renvoyé le code 11 « application suspendue » sur les trois points d’entrée interrogés. Aucun téléchargement réel par l’API n’a pu être qualifié avec cet identifiant.

### Découverte de téléchargements avec Codex

Dans **Paramètres → Sources**, renseigner un artiste, un titre ou un album et lancer la recherche explicite. Elle utilise le quota Codex existant. Les résultats de catégorie `import` entrent dans le registre en attente, avec la documentation et les conditions publiées. Une source proposant seulement un téléchargement officiel peut être utilisée par le parcours local. Quand une interface autorisée nécessite du code, **Ébaucher l’adaptateur** prépare du texte et des tests simulés dans l’espace isolé de Codex. La proposition reste disponible pour revue. Toute intégration exécutable demande une validation explicite.

## Diagnostic des échecs persistants

La tentative initiale appartient au moteur d’import. Après l’enregistrement de son résultat et l’actualisation du catalogue, Parole peut planifier jusqu’à deux relances classiques pour un échec temporaire. Ces relances attendent 60 secondes puis 240 secondes au minimum, ou le délai signalé par le fournisseur s’il est plus long. Plusieurs échecs comparables peuvent différer leurs relances automatiques de 30 minutes. Les autres lots gardent les règles de la file. Les compteurs et échéances survivent à l’arrêt de Parole. La commande manuelle reste disponible dans l’import.

Un échec persistant peut ensuite rejoindre le quota Codex commun si la catégorie **Échecs d’import** est activée. Une signature conserve la catégorie, l’étape, le fournisseur et les versions utiles. Les défauts dus à une version ou à une réponse d’API peuvent partager un diagnostic. Une playlist ou une édition inaccessible conserve sa propre identité. Parole transmet des faits typés et expurgés, dont le code de retour s’il est disponible. Le journal privé complet, les chemins locaux et les paramètres d’authentification restent locaux. Les limitations de requêtes, le manque d’espace, les permissions et les interruptions volontaires donnent lieu à une attente ou à la vérification locale.

Un résultat Codex peut suggérer une autre adresse de catalogue. La relance automatique n’a lieu qu’une fois, après confirmation indépendante de la réponse du catalogue et de l’identité de l’édition déjà autorisée. La file conserve le même lot, la sélection et les reçus des pistes existantes. Une édition différente demande une validation. Les diagnostics de code peuvent donner lieu à une ébauche et à des tests simulés dans l’historique Codex. Leur intégration au programme et leur exécution demandent une revue explicite. Si Codex est indisponible, si aucune correction n’est confirmée ou si la tentative corrigée échoue, le lot reste **À vérifier**.

### Mesures des fournisseurs

Les détails de **Paramètres → Sources** présentent les délais de recherche et de démarrage, le débit observé, la durée d’import, la réussite de récupération et le nombre exact d’échecs. La console indique le débit moyen mesuré lorsqu’il existe. Les moyennes donnent au moins 25 % de poids à la dernière mesure et vieillissent l’historique sur deux semaines. Le débit commence après le premier bloc d’octets et demande au moins 64 Kio supplémentaires observés. Les étapes d’enrichissement qui échouent après récupération audio ne comptent pas comme des échecs du fournisseur. Un fichier récupéré auparavant sur un site puis importé localement renseigne la durée locale. Les temps du téléchargement externe restent inconnus.

Les réponses HTTP 429 et 5xx qui déclenchent une attente sont mémorisées avec leur échéance. L’expiration réadmet la source. La réinitialisation des statistiques efface les observations et conserve les temporisations en cours.

### Comparer les éditions

Les comparaisons réduisent les variations Unicode, de casse, d’accents, d’apostrophes, de ponctuation et de présentation des collaborations. Les tags originaux restent disponibles. Les mentions Deluxe, Live, Remix, Acoustic, Instrumental et Remastered participent à l’identité. L’application compare séparément artiste, groupe de publications, édition et enregistrements, avec les identifiants MusicBrainz, ISRC et codes de publication présents. Elle confronte également les positions, les nombres de pistes et de disques et les durées.

Une correspondance confirmée demande une preuve d’édition complétée par les enregistrements ou les minutages comparables. Une piste locale manquante peut laisser l’édition confirmée si les autres positions portent les preuves nécessaires. Les rapprochements insuffisants sont présentés comme **Édition à vérifier**. Le journal indique la raison de la comparaison.

### Sélection du fournisseur pour une édition équivalente

Dans **Toutes les sources**, une édition complète confirmée et actuellement proposée par plusieurs fournisseurs automatiques apparaît sur une seule ligne. Le lot enregistre cette édition avant de rejoindre la file. Le moteur revérifie les autorisations et l’inventaire du fournisseur choisi avant de commencer la récupération. Les sources automatiques comparables sont actuellement Internet Archive et Jamendo. Les téléchargements officiels manuels et les résultats insuffisamment identifiés gardent leur parcours propre.

Dans **Paramètres → Général**, **Vitesse maximale** estime le temps de récupération d’après les mesures disponibles et le nombre de pistes encore manquantes. **Qualité audio maximale** classe d’abord les formats disponibles et la présence d’un fichier original. **Fournisseur préféré** utilise la source choisie si elle peut servir cette édition. Un fournisseur neuf peut être essayé sur un import demandé, une fois le groupe d’équivalence établi. Le moteur ne commande aucun achat et ne lance pas un second téléchargement pour établir un classement.

Le motif, le débit observé et le délai estimé, quand ils existent, figurent dans la console. Après un échec persistant, une indisponibilité signalée ou une piste inaccessible, une autre offre confirmée peut reprendre les positions manquantes. La reprise conserve les fichiers intégrés dans le dossier de la première édition et les tags d’origine de chaque piste. Une interruption conserve le fournisseur actif et ses reçus pour vérifier ses publications avant de basculer.

### Identités et provenances locales

L’index de maintenance contient les identités locales de l’édition et de l’enregistrement ainsi que les références de fournisseur. Il conserve les identifiants d’œuvre, de groupe de publications et de catalogue lorsqu’ils sont disponibles. **Détails → Provenance** présente les tags d’origine utiles à l’identification et les preuves, séparément des tags fournis par les catalogues. Un renommage établi par l’inventaire conserve l’identité et déplace la référence vers le chemin actuel.

Les rapprochements de recherches portent un niveau de confiance et des preuves consultables via **Comparer**. Pour approuver deux graphies d’un artiste sur une édition, utiliser **Paramètres → Général → Alias d’artiste pour une édition**. L’alias est révocable et s’applique au titre d’album renseigné. Les regroupements validés précédemment restent disponibles. Le formulaire n’écrit aucun tag ni fichier musical.

Le tri peut réutiliser un fichier déjà publié si son original est identique octet pour octet, ou si l’identité PCM concorde dans une édition et à une position établies. Le fichier en bibliothèque est revérifié. Les crédits, fichiers associés et préparations existants sont conservés. Une édition distincte peut contenir le même enregistrement. La correspondance du PCM seule ne fusionne donc pas ses éditions.

Avant un import multisource, les références déjà persistées peuvent couvrir certaines positions, même après un import initial depuis un fournisseur sélectionné seul. L’édition et l’enregistrement doivent être confirmés et le fichier local revérifié. Si les preuves pointent vers plusieurs dossiers d’édition, seuls les reçus du lot en cours peuvent servir à la reprise.

Si `fpcalc` est disponible sur le PATH ou via le chemin absolu `KARA_FPCALC`, le registre conserve une empreinte Chromaprint sur les 120 premières secondes des nouveaux fichiers reçus. Les rapprochements utilisent seulement des pistes dont les métadonnées et durées sont compatibles. Une empreinte concordante apporte un indice sur l’enregistrement et apparaît dans la provenance. Une fusion d’édition ou une réutilisation de préparation Kara exige la preuve audio et temporelle déjà applicable. [Chromaprint et fpcalc](https://github.com/acoustid/chromaprint)

## Validation

```bash
python3 tests/test-parole-import.py -v
python3 tests/test-parole-import-recovery.py -v
PAROLE_TEST_GTK=1 python3 tests/test-parole-import.py -v
```

La suite crée une bibliothèque temporaire avec un son de synthèse. Elle utilise `yt-dlp` et FFmpeg sur une source HTTP locale et contrôle les réponses du catalogue, des pochettes et des paroles. Elle couvre 100 lots totalisant 500 albums, quatre lots actifs, le verrou de tri, la reprise après interruption, les doublons et le manque d’espace. Les tests GTK facultatifs utilisent une session GTK et D-Bus. Ils exercent le journal, la relance sélective, la file commune avec la salle mobile et l’actualisation de la bibliothèque.

Les essais locaux vérifient l’identité PCM de pistes FLAC, le maintien des sources, la conservation octet pour octet du LRC et de la pochette fournis, le refus d’un membre ZIP hors chemin et la reprise d’une seule piste dans un lot partiellement publié.

Le parcours Bandcamp est testé avec un FLAC de synthèse et une référence publique représentative. Le test vérifie la provenance et la désactivation du fournisseur. Il ne se connecte pas à un compte Bandcamp.

Le parcours Qobuz est testé avec un ZIP FLAC de synthèse et une référence d’édition au format Qobuz. Le test contrôle la conservation du PCM et de cette référence sans accéder à un compte ni à une commande.

Le parcours SoundCloud est testé avec une piste de synthèse sans tag d’album. Seul le fichier sélectionné est classé, avec une provenance déclarée et le même PCM. Le test ne se connecte pas à SoundCloud.

Le parcours Free Music Archive est testé avec un FLAC de synthèse. Le test vérifie la conservation de la provenance et de la licence CC BY et le refus d’une licence manquante ou CC BY-ND.

Le parcours des sites d’artistes a été vérifié en bibliothèque isolée avec le MP3 officiel de *This Too Shall Pass* par Scott Buckley, sous licence CC BY 4.0. Le fichier audio a été classé avec un PCM identique, son format et sa provenance. L’enrichissement en ligne était simulé et l’absence de pochette a laissé le lot partiel. Le test automatisé couvre aussi un lot qui se termine et le refus des références privées. [Fichier et crédits officiels](https://www.scottbuckley.com.au/library/this-too-shall-pass/)

Internet Archive a renvoyé la fiche *Glow Wave* de La Machine de la Mort, sous licence CC BY 4.0. Les dix MP3 originaux totalisant 36 248 422 octets ont été téléchargés et les dix fichiers publiés sont identiques octet pour octet aux fichiers reçus. La relance n’a transféré aucune piste. L’enrichissement simulé a laissé la pochette à compléter. Les tests automatisés couvrent une reprise partielle, un fichier privé et la distinction entre fichier original et dérivé. [Fiche et crédits officiels](https://archive.org/details/glow-wave)

Les essais Jamendo simulent les réponses officielles et le transport avec des FLAC de synthèse. Ils couvrent l’absence d’identifiant, les deux permissions de téléchargement, le refus de paramètres ambigus, la recherche, l’identité PCM et la reprise limitée à la piste en échec.

La découverte Codex est testée avec une réponse de recherche simulée. Le fournisseur découvert reste en attente et la demande d’ébauche demeure un travail séparé.

Les tests de mesures contrôlent la pondération récente, la persistance des comptes, la séparation entre démarrage et débit et l’expiration d’une limitation. Le parcours Internet Archive de synthèse vérifie l’enregistrement d’une recherche, d’un échec de récupération et de sa reprise réussie.

Une mesure réelle isolée sur l’introduction de *Electric Funeral* a confirmé la somme de contrôle des 1 325 388 octets reçus. Sur ce transfert unique, le premier bloc est arrivé en 1,80 s et le débit après ce bloc était de 1,51 Mio/s. [Fiche et crédits officiels](https://archive.org/details/electric-funeral)

Les tests d’identité ont confirmé les cas concordants avec variations de présentation et un album incomplet. Les 16 variantes construites pour représenter des éditions, artistes, positions ou enregistrements différents ont produit zéro confirmation. Il s’agit d’un contrôle sur des cas construits.

Les stratégies et le basculement sont validés avec deux catalogues simulés. Après deux échecs sur la seconde piste Internet Archive, Jamendo a fourni cette seule position dans le même dossier. La première piste est restée identique et une relance n’a rien retéléchargé. La comparaison en ligne entre deux fournisseurs reste à qualifier avec un identifiant Jamendo autorisé et une édition commune confirmée.

Le test de provenance importe le même FLAC par deux parcours, avec un renommage entre les deux. Il conserve un seul fichier, son LRC manuel et deux références de fournisseur. La variante Deluxe de même PCM conserve une édition séparée.

Un second test intègre une première piste via un import Internet Archive direct. Le nouveau lot multisource reconnaît cette position et transfère uniquement la seconde via Jamendo.

Avec fpcalc 1.6.0, l’empreinte de *This Too Shall Pass* a obtenu une similarité de 100 % en FLAC, 98,25 % en Opus à 64 kbit/s et 99,58 % en MP3 à 128 kbit/s après réencodage local. La piste indépendante *Afterglow Avenue* a obtenu 51,70 %. L’import isolé de la version Opus a retrouvé la similarité de 98,25 % et conservé les deux encodages. Les résultats couvrent une source et une piste de comparaison. [Fichier et crédits Scott Buckley](https://www.scottbuckley.com.au/library/this-too-shall-pass/) · [Fiche et crédits de Glow Wave](https://archive.org/details/glow-wave)
