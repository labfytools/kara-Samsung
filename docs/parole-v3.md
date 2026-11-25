# Persistance et maintenance de Parole V3

## Possession et persistance

| Données | Emplacement | Récupération |
| --- | --- | --- |
| Index, tâches, sources, exclusions, décisions | `$XDG_STATE_HOME/kara/parole/<empreinte de la racine>/maintenance.sqlite3` | Une instance possède le verrou `owner.lock`. Elle remet en attente les tâches interrompues, dans leurs catégories et avec leurs échéances. |
| Console | `maintenance.jsonl` dans le même dossier | Rotation à 512 Kio, quatre archives, 600 événements dans la console. Le bouton Historique charge les 4 000 derniers événements au maximum. |
| Corrections locales | À côté de l’audio, avec l’ancienne version dans `corrections/<id>` | Le dialogue vérifie les versions avant d’écrire. Restaurer une version se fait dans ce dialogue en recopiant son texte. |
| Imports | File commune dans `$XDG_STATE_HOME/kara/import-queue` | Les reçus de maintenance restent liés au lot existant. Les lots intégrés passent dans l’historique. Les imports partiels et les échecs restent relançables. |
| Préparation | `$XDG_STATE_HOME/kara/prepared` et `$XDG_CACHE_HOME/paroles-strawberry/stems` | Kara lit les mêmes générations que Parole. Les brouillons marqués de traitements interrompus peuvent être nettoyés dans Stockage. |
| Associations manuelles | Index Parole et manifestes d’origine dans `$XDG_STATE_HOME/kara/music-tidy-backups` | Les manifestes terminés rétablissent les associations exactes par album. Les crédits de piste et les sauvegardes restent intacts. |

Avec les valeurs XDG par défaut, l’état se trouve sous `~/.local/state` et le cache sous `~/.cache`. Conserver l’index avec son WAL lors d’une copie à chaud ou utiliser l’API de sauvegarde SQLite. Pour intervenir sur les fichiers hors ligne, attendre la fin de la maintenance, des imports actifs et des parties.

L’inventaire prend les chemins et versions du système de fichiers, puis associe les informations de Strawberry en lecture seule. Les métadonnées des fichiers reconnus sont consultées par lots. Les fichiers nouveaux ou dont la version a changé sont sondés si les métadonnées Strawberry ne correspondent plus. Une interruption de l’inventaire conserve les enregistrements qui n’ont pas encore été visités. Les flux PCM sont identifiés au moment des traitements qui en ont besoin. Une empreinte PCM inclut les canaux, le taux et l’ordre des échantillons. Les doublons certains sont filtrables une fois les deux flux identifiés. Les déplacements conservant l’inode, la taille et la date de modification gardent l’identifiant d’index. Les références Turbo et pistes Demucs peuvent être réassociées sur preuve d’identité. Les caches antérieurs sans preuve PCM gardent leurs règles d’admission historiques.

## Ordre de travail et limites

Le propriétaire GTK existant garde les priorités des joueurs, de l’organisateur et de l’import. La maintenance revendique une tâche SQLite à la fois et répartit les tours de recherche entre paroles, LRC, albums, discographies, pochettes et sources. Les opérations réseau conservent leur échéance et une temporisation progressive. Les tentatives de recherche sur des fournisseurs et les préparations anticipées ont chacune leur compteur journalier. La file d’importation conserve sa concurrence configurable de 1 à 8 lots, 4 par défaut, et ses réservations partagées.

| Réglage initial | Valeur |
| --- | --- |
| Inventaire | Au lancement, puis toutes les 15 min |
| Vérifications éditoriales | Tous les 30 jours après une vérification |
| Opérations réseau classiques | 24 par jour, à 20 s ou plus d’intervalle entre tâches |
| Préparations anticipées | 3 admissions par jour, une à la fois |
| Inactivité Sway | 300 s, sur secteur par défaut |
| Admission lourde | CPU <25 %, GPU ≤15 %, activité disque <25 %, température <78 °C, mémoire disponible ≥4 Gio |
| Surveillance en cours | Événement de reprise Sway, alimentation, activité multimédia, partie ou import prioritaire, pression mémoire, réserve mémoire de 1,5 Gio, température et stockage |
| Stockage | 50 Gio de cache et 10 Gio libres sur chaque partition utile, avec marge temporaire par morceau |

Les interruptions terminent le groupe de processus et les moteurs libèrent leurs ressources. Les fichiers déjà publiés sont réutilisables. Une préparation interrompue pendant Demucs peut reprendre avec des pistes séparées si leur génération a été validée. Une partie et les sélections mobiles conservent leur chemin prioritaire. La suspension globale ne modifie pas la file d’importation ni une partie Kara.

Une recherche de paroles préserve les textes locaux et les corrections. Les écritures de tags utilisent le moteur existant et sa vérification des flux audio et image, avec copie des flux. Le LRC externe vérifié a la priorité. Après une recherche classique récente sans résultat et, si configuré, une attente bornée de Codex, Turbo peut utiliser le texte Strawberry ou sa correction locale. L’alignement n’ajoute aucun repère à une ligne sans appui acoustique. Une génération publie un LRC seulement si tous les débuts de ligne sont observés et si les seuils Kara sont atteints. Les mots incertains restent sans repère de notation. La couverture et l’erreur temporelle face à une référence annotée sont deux mesures distinctes.

L’album est comparé aux positions disque et piste et aux titres de l’édition MusicBrainz. Une correspondance par titre seule est insuffisante. La discographie utilise un MusicBrainz ID inscrit dans le fichier ou corroboré par des éditions locales distinctes. Les ambiguïtés, doublons et positions absentes sont consultables. L’import automatique requiert une édition vérifiée et un lien YouTube Music expressément autorisé dans son dialogue. Les pistes déjà reconnues par la file sont réutilisées. Les exclusions persistent. Les champs de droits ou les liens proposés par Codex ne valent pas autorisation.

## Codex et fournisseurs

Codex est désactivé au départ et utilise l’authentification ChatGPT existante après activation explicite dans Paramètres. Une recherche est limitée à 120 s et à deux départs locaux par jour par défaut. Le quota distant restant n’est pas mesurable dans Parole. Un signal de quota distant suspend les nouveaux départs pendant une journée. La recherche est lancée avec `codex exec`, une sortie JSON structurée, un espace jetable Bubblewrap et les fonctions d’exécution de commandes désactivées. Le seul fichier d’authentification monté est en lecture seule. L’audio, BlackBox, les répertoires personnels et les dépôts sont absents de l’environnement.

Un résultat de recherche est recevable seulement si la CLI rapporte une recherche Web et si la réponse respecte le schéma. Les résultats restent consultables dans Paramètres → Codex. Ils déclenchent la découverte de fournisseurs et des vérifications indépendantes. Aucun texte ou contenu inventé n’est injecté dans la bibliothèque. Une ébauche d’adaptateur peut être sollicitée depuis Sources. Le texte de code, ses tests et ses fixtures sont visibles dans le résultat Codex pour revue. Leur exécution et leur intégration demandent une validation séparée.

Les sources enregistrées sont consultables, désactivables et mesurées pendant les opérations. Une source compatible LRCLIB reçoit une configuration avec `host`, `path`, `terms` et `fixtures`. Chaque fixture indique `title`, `artist`, `album`, `duration`, éventuellement `album_artist`, `synced` et `sha256`. La valeur `sha256` vaut `null` pour un résultat attendu négatif, ou l’empreinte du texte vérifié pour un positif. Il faut au moins deux positifs et un négatif indépendants. Après confirmation que les conditions permettent les requêtes automatisées, Parole vérifie ces cas en arrière-plan. En cas de succès, la source est activée et les échecs de paroles ou de LRC redeviennent progressivement éligibles. Le format accepte uniquement l’adaptateur approuvé et ses paramètres JSON. Les destinations configurables sont publiques, les redirections sont refusées et chaque résolution DNS est épinglée à des adresses autorisées avant l’établissement de TLS.

Le statut Local, Externe ou Généré du LRC apparaît dans le tableau. Les détails indiquent la provenance, la couverture si mesurée et les éventuels échecs. Le journal prend ses messages dans les événements des opérations et écarte les sorties des processus Codex.

## Qualification

Les fixtures se lancent avec les commandes suivantes.

```sh
python3 -B tests/test-parole-v3.py -v
python3 -B tests/test-parole-import.py -v
python3 -B tests/test-kara-preparation.py -v
KARA_SILENT=1 python3 -B tests/test-kara-v1.py -v
node tests/test-kara-tizen.js
```

Pour les tests graphiques, utiliser `PAROLE_TEST_GTK=1` sur un bureau ou un compositeur de test avec bus D-Bus dédié. Les essais du 25 novembre 2026 ont utilisé un Sway headless, une bibliothèque temporaire et le mode silencieux. L’échantillon de 80 pistes réelles a été inventorié en lecture seule. Les deux moteurs locaux ont été exécutés sur une copie privée d’un morceau de 22,754 s déjà muni d’un LRC. Aucun son ni microphone physique n’a été utilisé.

| Mesure observée | Résultat |
| --- | --- |
| GTK dans Sway headless, bibliothèque temporaire | 1,357 s à la première image, 202 Mio RSS et 1,20 % d’un cœur au repos |
| Inventaire réel, 80 fichiers | 0,283 s au premier passage, 0,212 s à chaud, 0 modification au second passage |
| Demucs CPU, échantillon de 22,754 s | 19,575 s, lecture du cache en 0,302 s |
| Turbo Vulkan sur le même échantillon | 22,297 s pour le processus complet, 2,392 s d’analyse reportée, 93,83 % de mots couverts, 1,657 s pour la relecture du cache |
| Pic de charge GPU pendant Turbo | 97 % observés sur le périphérique, charge agrégée |
| Taille de préparation de l’échantillon | Environ 2,57 Mo pour les deux FLAC et le manifeste |
| Cache existant mesuré en lecture seule | Environ 307 Mio sur la machine à la date du relevé |
| Fournisseurs publics | MusicBrainz et LRCLIB ont répondu à une requête publique de qualification |
| Codex CLI 0.172.0 | Authentification ChatGPT et diagnostic réseau isolés réussis. Aucun événement de recherche Web vérifiable pendant les essais de bout en bout. |

La qualification ne fixe pas la précision de 500 ms. Elle demande un corpus dont les débuts de ligne et de mot sont annotés indépendamment. Les temps observés sur un morceau court ne donnent pas la moyenne de la bibliothèque. La TV physique, la découverte effective d’un nouveau fournisseur par Codex et une campagne d’importation automatique sur la collection réelle restent à qualifier.
