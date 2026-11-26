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

Le réglage **Lots simultanés** accepte de 1 à 8 lots et vaut 4 à la première utilisation. **Mettre la file en pause** bloque les départs et les admissions automatiques, interrompt les téléchargements et suspend les recherches de paroles, de LRC et de pochettes des imports. Pendant la fermeture des moteurs et la sauvegarde des lots, Parole et la salle affichent **Suspension en cours…**. **Reprendre la file** devient disponible après libération des réservations de tous les moteurs concernés. La console note la demande, l’arrêt des moteurs et la reprise. La partie Kara, la synchronisation USB manuelle, la maintenance générale et la préparation nocturne suivent leurs commandes habituelles.

Diminuer la concurrence laisse les lots actifs se terminer. Le panneau d’import et la salle indiquent les lots actifs, suspendus ou en attente, les albums terminés, les pistes traitées, les erreurs et l’avancement. La progression globale répartit le travail par album.

La liste d’import occupe l’espace disponible entre les statistiques et le bouton **Historique**. Agrandir la fenêtre affiche davantage de lots. Les résultats de recherche gardent leur propre zone de défilement.

Chaque lot géré prépare une fenêtre glissante de 24 albums au plus et partage ses travailleurs de transfert par fournisseur. La fenêtre laisse de la place aux albums en résolution ou en publication pendant les transferts. Toutes les pistes prêtes passent par la même admission de la bibliothèque, avec priorité aux demandes manuelles éligibles et ordre d’arrivée à priorité égale. Deux créneaux restent dédiés à la préparation du catalogue et deux au traitement des métadonnées. L’enrichissement des paroles, des LRC et des pochettes passe par la maintenance persistante.

Dans un lot mixte, la fenêtre de préparation prend des albums de chaque source à tour de rôle. Lors d’une reprise, pour une source donnée, les albums encore en attente passent avant les albums interrompus puis les tentatives déjà en échec. Le lot conserve ses cibles, les reçus et l’ordre d’origine dans chacun de ces groupes. La recherche **Toutes les sources** propose **Importer les résultats libres**. Cette action utilise Internet Archive et Jamendo selon la configuration, groupe les éditions dont l’équivalence est confirmée et vérifie les droits au moment du transfert. Un lien explicite garde sa source. Pour un plan multisource déjà vérifié, **Vitesse maximale** ajoute au délai mesuré l’attente estimée depuis les demandes prêtes et la cadence locale. Une reprise rapproche d’abord les reçus de la source interrompue.

**Téléchargements simultanés** règle le plafond par bibliothèque, de 1 à 20. La valeur initiale est 10. Après une réduction, les paliers de 5, 10, 15 et 20 se testent selon le nombre de pistes prêtes, les créneaux occupés et les transferts réussis. Le téléchargement d’un gros album peut ainsi qualifier une remontée avant sa publication. Si le palier de référence a publié des pistes, le test compare aussi cette cadence. Une hausse sous-utilisée, sans gain de débit d’au moins 5 % ou qui réduit la cadence de plus de 5 % ramène au palier précédent. Les incidents d’E/S réseau confirmés et la pression sur les ressources réduisent l’admission suivante. Les temporisations et quotas d’un fournisseur s’appliquent à sa propre source. Les créneaux déjà occupés se libèrent progressivement.

L’admission globale est partagée par Parole et le CLI. Le profil local par défaut de YouTube utilise trois créneaux. Pour une installation dont l’accès automatisé et la concurrence autorisée sont documentés, `KARA_YOUTUBE_AUDIO_SLOTS` fixe le plafond de ce profil entre 1 et 20, dans la limite globale. Les conditions de YouTube encadrent l’accès automatisé et les téléchargements. La limite locale de 512 Kio/s est supprimée. Un fragment à la fois maintient une seule acquisition de média par créneau. Les délais de connexion, les reprises et la temporisation suite aux erreurs réseau restent en place. [Conditions de YouTube](https://www.youtube.com/t/terms?gl=FR&hl=fr) · [Options de téléchargement yt-dlp](https://github.com/yt-dlp/yt-dlp#download-options)

Internet Archive et Jamendo conservent leurs intervalles entre requêtes, quotas, contrôles de licence et temporisations HTTP. Une source en temporisation laisse les pistes éligibles des autres sources utiliser la file globale. Pour YouTube, yt-dlp conserve une réservation du fournisseur pendant la résolution de la piste. Son ordonnanceur prend un créneau global juste avant le téléchargement. Il libère les deux réservations avant le post-traitement FFmpeg, qui utilise la file de traitement commune. La file borne aussi le nombre de processus yt-dlp en attente sur l’ensemble des lots. Les brouillons gardent les mêmes noms et reprennent avec le même yt-dlp. Le compteur de réception suit les corps audio du premier octet à leur fin, avec une fenêtre d’activité récente de trois secondes. Le débit suit les octets reçus sur cinq secondes et la cadence suit les pistes reçues et publiées sur une minute. [Accès automatisé Internet Archive](https://archive.org/developers/bots.html) · [Autorisations de téléchargement Jamendo](https://developer.jamendo.com/v3.0/tracks/file) · [Étapes des postprocesseurs yt-dlp](https://github.com/yt-dlp/yt-dlp#usage-and-options)

| Source | Profil local par défaut | Règles du fournisseur |
| --- | --- | --- |
| YouTube Music | 3 opérations réseau, résolution comprise, et 0,5 s entre les départs yt-dlp. Un fragment par piste. | Accès automatisé et téléchargement soumis aux permissions prévues par les conditions de YouTube. La valeur 3 est un choix local. |
| Internet Archive | Jusqu’à 20 transferts dans la limite globale, au moins 1,2 s entre deux départs et 200 requêtes/jour par défaut. | Identifier le logiciel, espacer les demandes, honorer HTTP 429 et `Retry-After`, vérifier la licence et l’accès au fichier d’origine. Leur guide illustre quatre téléchargements concurrents avec une seconde de délai. |
| Jamendo | Jusqu’à 20 transferts dans la limite globale, au moins 1,2 s entre deux départs et 200 requêtes/jour par défaut. | Client d’application requis, autorisation de téléchargement par piste et par album. Le code API 6 signale une limite réelle et suspend les nouveaux départs. |

La fenêtre indique le plafond global appliqué, le volume de résolutions, les pistes prêtes et les pistes en attente de FFmpeg. Les raisons précisent la source, son profil ou quota local, un refus de disponibilité et le délai observé d’une limitation fournisseur. Une limitation plus courte arrivée pendant une temporisation existante ne réduit pas le délai en vigueur. [Conditions de YouTube](https://www.youtube.com/t/terms?gl=FR&hl=fr) · [Guide Internet Archive](https://archive.org/developers/bots.html) · [Authentification Jamendo](https://developer.jamendo.com/v3.0/authentication) · [Codes Jamendo](https://developer.jamendo.com/v3.0/response-codes)

La file exige 64 Mio libres dans l’espace d’état. L’admission conserve 2 Gio libres dans la bibliothèque et prévoit une marge par transfert pour la piste et ses fichiers temporaires. Le moteur surveille l’espace pendant les transferts et limite la taille annoncée d’une piste à 256 Mio. Les lots en attente restent enregistrés lorsque la bibliothèque manque d’espace.

### Qualification des créneaux audio

L’audit de `06bb68d` identifie trois choix locaux historiques. Le CLI utilisait deux travailleurs par album, le profil local de YouTube accordait trois créneaux et yt-dlp recevait un plafond de 512 Kio/s. Avec yt-dlp 2026.11.23, quatre passages successifs sur le même original CC0 de 7,87 Mio ont pris 19,94 et 19,38 s avec ce plafond, puis 5,19 et 5,04 s pour les deux passages intercalés sans plafond local. La durée inclut la connexion et le lancement du programme. Les quatre fichiers ont été vérifiés avec la somme du manifeste. [Fiche Internet Archive](https://archive.org/details/ILOOSE)

La qualification du 25 novembre 2026 a importé et publié 26 originaux CC0 d’Internet Archive (272,38 Mio) dans une bibliothèque isolée, avec un seul lot par passage. Les départs ont été espacés de 1,22 s en médiane. Le pic mesure les corps audio en cours de réception. [ILOOSE](https://archive.org/details/ILOOSE) · [Jj Lens](https://archive.org/details/JjLensAkvavitSessionsDansihLanguageTributesToGgAllin) · [LAM002](https://archive.org/details/LAM002) · [Hindle Wakes](https://archive.org/details/hindlewakes_1209_librivox)

| Plafond global | Pic en réception | Durée | Débit de bout en bout | Pistes publiées/min |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 5 | 108,00 s | 2,52 Mio/s | 14,44 |
| 10 | 4 | 149,70 s | 1,82 Mio/s | 10,42 |
| 15 | 4 | 115,44 s | 2,36 Mio/s | 13,51 |
| 20 | 7 | 57,41 s | 4,74 Mio/s | 27,17 |

Les plafonds de 10 à 20 sont restés au-dessus des pics observés. Ces passages donnent la cadence d’importation de cet échantillon, avec la variabilité du réseau. Pour mesurer la capacité de réception, un autre essai sur 21 originaux (150,92 Mio) a ralenti localement la lecture de chaque flux à 48 Kio/s pendant les 55 premières secondes, avec les mêmes règles d’accès au fournisseur. Le moteur a observé 5, 10, 15 et 20 réceptions simultanées à 10,30 s, 16,61 s, 22,67 s et 28,70 s. Les 21 pistes ont été publiées en 107,82 s. Cette durée inclut le ralentissement de l’essai.

Le même essai a été répété après le découpage de yt-dlp et la répartition des albums par source. Le pic Internet Archive a de nouveau atteint vingt réceptions. Les 21 originaux ont été intégrés en 67,53 s, soit 18,66 pistes/min jusqu’à publication. La lecture locale est restée ralentie pendant les 55 premières secondes. L’essai de transport couvre les créneaux et la publication sur des fichiers réels avec les limites d’accès habituelles.

`python3 -B tests/test-parole-downloads.py -v` passe par la file persistante, l’admission des pistes, les réponses HTTP avec plages, la validation d’originaux FLAC et le tri. Le serveur local définit une cadence et des quotas propres à la fixture. Les mesures couvrent la durée jusqu’à la publication des 36 pistes sur disque. La maintenance des paroles reste en attente au moment où l’audio est disponible.

| Plafond global | Pic observé | Durée | Débit de bout en bout | Pistes publiées/min |
| ---: | ---: | ---: | ---: | ---: |
| 3 | 3 | 14,24 s | 0,263 Mio/s | 151,7 |
| 5 | 5 | 9,73 s | 0,384 Mio/s | 221,9 |
| 10 | 10 | 7,68 s | 0,487 Mio/s | 281,3 |
| 15 | 15 | 7,58 s | 0,494 Mio/s | 285,0 |
| 20 | 20 | 7,58 s | 0,494 Mio/s | 285,0 |

Dans cette exécution sur ArchASP, le gain au-delà de 10 reste sous le seuil de 5 % utilisé pour conserver un palier supérieur. Le banc vérifie aussi la pause à vingt, la reprise des plages validées, un lot de 231 éditions, la priorité manuelle, une réponse HTTP 429 et les réductions d’admission sous pression mémoire ou disque.

Le 25 novembre 2026, six originaux d’Internet Archive sous CC0, soit 29,2 Mio, ont été publiés dans deux bibliothèques d’essai indépendantes avec les intervalles et quotas habituels. La révision `bb1fd97` a pris 26,27 s pour les six pistes, soit 1,11 Mio/s et 13,7 pistes/min. Le moteur à file globale a pris 11,65 s, soit 2,50 Mio/s et 30,9 pistes/min. Il s’agit d’un passage par version sur le réseau réel, mesuré jusqu’à la publication sur disque. [Fiche de la collection audio](https://archive.org/details/ILOOSE) · [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)

### Reprise et diagnostics

L’état est enregistré dans une base SQLite privée sous `$XDG_STATE_HOME/kara/import-queue`, dans un sous-dossier propre à la bibliothèque. La pause y reste enregistrée après fermeture de Parole ou redémarrage de `kara.service`. Les lots suspendus gardent leur place et leur identifiant. Fermer le propriétaire de la file entre deux commandes arrête les nouveaux départs. Les moteurs qui survivent à leur propriétaire gardent leurs réservations jusqu’à la fin de leur opération. Les résultats conservés déclenchent l’actualisation de la bibliothèque lorsque Parole retrouve la file.

Les transferts utilisent des dossiers privés et stables sous `A_trier`. YouTube garde ses téléchargements partiels dans `.download`. Internet Archive peut reprendre une plage déjà reçue si le serveur la fournit et si le fichier final correspond à la somme publiée par le fournisseur. Un flux dépourvu de preuve de continuité reprend au début de la piste. Le tri vérifie les tags et enregistre les destinations avant de publier l’audio. À la reprise, le moteur saute les albums terminés, vérifie les reçus des pistes publiées et réutilise les fichiers complets déjà reçus. Les enrichissements reprennent séparément. Une pochette ou un LRC existant est conservé. L’écriture des paroles intégrées vérifie les métadonnées et l’identité des flux audio et image avant publication.

Les résultats des transferts sont traités dans leur ordre d’achèvement. Une piste convertie ou un original validé peut être trié pendant que les créneaux de réception servent les suivants. Le tri prépare ses copies et remux hors du verrou de bibliothèque, puis revérifie la version, la destination et le classement manuel au moment de publier. Les crédits de collaboration dépourvus d’artiste d’album explicite peuvent encore avoir besoin des autres titres pour établir leur destination. Le reçu et la notification V2 partent dès la publication de chacun de ces fichiers. Strawberry découvre les changements par sa surveillance des dossiers.

Les pochettes et les recherches de références des imports ont chacune un travailleur sous le bail de maintenance et utilisent la file SQLite existante. Une recherche de paroles lente peut se poursuivre pendant la récupération d’une pochette. Pour une même piste, le texte publié peut servir à vérifier la correspondance du LRC et les deux recherches réutilisent la réponse fournisseur lorsqu’elle contient les deux formats. Le texte devient disponible avant l’éventuelle requête de minutage. Les notifications de fichiers enrichis partent dès leur écriture. Un LRC fourni avec une sélection locale peut accompagner sa piste, et l’affectation d’une pochette locale tient compte des destinations du dossier source. La pause des imports attend toujours la libération des moteurs et le règlement de leurs tâches.

Dans Parole, **Détails** ouvre le diagnostic sélectionnable et donne accès à **Ouvrir le journal complet**. **Relancer les échecs** reprend le même lot en conservant ses albums terminés. Les journaux privés restent sous `$XDG_STATE_HOME/kara/imports` (`~/.local/state/kara/imports` par défaut). Un lien enregistré dans un ancien journal peut être repris avec `kara --reprendre JOURNAL -- LIEN`. Pour plusieurs liens, utiliser `kara --reprendre JOURNAL --suivi-json --lot LIEN1 LIEN2`. Un journal historique commence au lancement d’un lot. Les demandes rejetées avant lancement et la file en mémoire d’une ancienne fenêtre n’y figurent pas. Laisser une ancienne session vider sa file avant de la fermer et de rouvrir Parole.

**Historique** ouvre les lots terminés, y compris les échecs et les résultats partiels. Sélectionner plusieurs entrées puis utiliser **Supprimer la sélection**, effacer uniquement les importations réussies, ou confirmer **Effacer tout l’historique**. Le bouton **Retirer** de chaque lot terminé encore visible applique le même retrait. La liste, l’historique et les compteurs se mettent à jour ensemble dans Parole. Le retrait persiste aussi sur le mobile et écarte le lot des relances automatiques. L’audio, les reçus de publication, les identités, les résultats de reprise et les validations de catalogue déjà en attente sont conservés.

La conservation est indéfinie par défaut. Le menu **Nettoyage automatique** propose 7, 30 ou 90 jours après la clôture. Pour un ancien lot dépourvu de date de clôture enregistrée, le délai court à compter de la migration. Les lots actifs, en attente et suspendus restent dans la file.

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

Pour YouTube, le moteur reconnaît une limitation même lorsque yt-dlp préfixe le message par « Video unavailable ». Une limitation de session suspend les nouvelles résolutions et ouvertures de transferts pendant au moins une heure. HTTP 429 suspend les départs pendant au moins une minute. Un délai `Retry-After` plus long exprimé en secondes prévaut. Une série de six réponses ambiguës sur des identifiants vidéo distincts en deux minutes déclenche une suspension de 30 minutes. Une extraction suivie d’un transfert réussi réinitialise le comptage des réponses ambiguës. Les réponses qui nomment une restriction propre à une vidéo, telle qu’une vidéo privée, restent individuelles. [Diagnostics de l’extracteur YouTube de yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#this-content-isnt-available-try-again-later)

La suspension du fournisseur est partagée par les moteurs et survit au redémarrage. Les corps audio déjà ouverts et les fichiers reçus peuvent finir leur traitement. Les demandes différées par une limitation confirmée restent **À vérifier** avec leurs reçus. Si la suspension a été déclenchée par des réponses ambiguës, la catégorie **Échecs d’import** peut lancer un diagnostic Codex mutualisé par fournisseur et version. Les autres sources continuent à utiliser la file commune.

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
