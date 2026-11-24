# Import d’albums dans Paroles Strawberry

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

Les téléchargements et les brouillons de post-traitement restent dans un lot privé sous `A_trier`. Seules les pistes publiées par `yt-dlp` sont présentées au tri. Le tri attend le verrou de la bibliothèque, contrôle les tags, et identifie une piste déjà présente dans un même album par sa source vidéo et sa position. Les pistes déjà classées restent dans la collection. Les fichiers résiduels d’un transfert ou d’un tri interrompu sont conservés dans le dossier de réception indiqué dans les détails.

L’enrichissement cible les pistes classées pour chaque album. Une pochette ou un fichier LRC existant est conservé. L’écriture de paroles intégrées vérifie les métadonnées et l’identité des flux avant de publier le fichier mis à jour. Un service qui répond sans correspondance vérifiable est distinct d’un service en erreur.

Chaque lot donne un bilan par album. Dans Parole, **Détails** ouvre le texte sélectionnable des erreurs et donne accès à **Ouvrir le journal complet**. **Relancer les échecs** resoumet les albums échoués ou partiels. La reprise vérifie les pistes déjà classées et peut refaire uniquement la recherche de pochettes et de paroles si la partie audio avait abouti. Les journaux sont privés et conservés sous `$XDG_STATE_HOME/kara/imports` (`~/.local/state/kara/imports` par défaut). La commande `kara --reprendre JOURNAL -- LIEN` permet aussi de reprendre un lien issu du journal après avoir fermé la fenêtre. La reprise en lot utilise `--suivi-json --lot LIEN1 LIEN2`.

## Validation

```bash
python3 tests/test-parole-import.py -v
PAROLE_TEST_GTK=1 python3 tests/test-parole-import.py -v
```

La suite crée sa propre bibliothèque et un son de synthèse. Elle exerce `yt-dlp` et FFmpeg sur une source HTTP locale, les scripts de tri et de paroles, et la publication d’une pochette. Les réponses externes du catalogue, des pochettes et des paroles sont contrôlées. Le test GTK facultatif exige une session graphique et D-Bus. Il vérifie le détail intégral d’un échec de playlist, le journal et la relance sélective.
