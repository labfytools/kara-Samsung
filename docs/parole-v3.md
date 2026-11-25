# Parole V3

Parole ouvre le tableau de bord de la bibliothèque. L’en-tête donne accès à l’importation, à la connexion TV et aux paramètres. La console affiche les étapes réellement observées. La consultation et la correction des paroles se font depuis les détails d’une chanson. La lecture, la notation, la salle, la télévision, les volumes et BlackBox utilisent le contrôleur Kara existant. Le mode `--service` continue de servir Tizen et les téléphones si la fenêtre Parole est masquée. La maintenance démarre à l’ouverture de la fenêtre Parole.

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
| Opérations réseau classiques | 24 par jour, au moins 20 s entre opérations et jusqu’à 145 s par recherche. Réglages dans Ressources. |
| Préparations anticipées | 3 admissions par jour hors mode nocturne, une à la fois |
| Inactivité Sway | 300 s, sur secteur par défaut |
| Admission lourde | CPU <25 %, GPU ≤15 %, activité disque <25 %, température <78 °C, mémoire disponible ≥4 Gio |
| Surveillance en cours | Événement de reprise Sway, alimentation, activité multimédia, partie ou import prioritaire, pression mémoire, réserve mémoire de 1,5 Gio, température et stockage |
| Stockage | 50 Gio de cache et au moins 20 Gio libres sur chaque partition utile, avec marge temporaire par morceau |

### Préparation nocturne

La section **Paramètres → Préparation nocturne** active un traitement à la fois dans la file existante, sans limite quotidienne. En mode service, le propriétaire de Kara démarre la maintenance même sans fenêtre. Le mode exige une source secteur système confirmée par le noyau. Les périphériques USB alimentés par le portable et le pourcentage de batterie ne servent pas de preuve. La durée d’inactivité initiale est de 420 s, puis le maintien éveillé est limité à 120 min par période d’inactivité. Un redémarrage du service ne recrédite pas cette durée.

Sur ArchASP, `swayidle.service` lance le verrou QuickShell à 300 s et coupe les écrans à 360 s. Sa demande `IdleHint` à 420 s peut être rejetée par logind sous le gestionnaire système utilisateur. Parole observe donc l’inactivité via le compositeur, confirme le verrou et lit l’état de l’écran par IPC Sway. logind gère les actions du capot et sa politique `IdleAction` actuelle vaut `ignore`. Parole emprunte un verrou `idle` uniquement pendant une série de préparations admises. Si le refroidissement capot fermé a été vérifié sur la machine et que l’utilisateur active cette option, le verrou prend aussi `handle-lid-switch`, acquis capot ouvert. Une demande manuelle de veille reste possible. Le verrou `sleep` est en mode `delay` et sert au seul arrêt des moteurs avant la veille. La libération des descripteurs rétablit la politique logind en vigueur, y compris après un plantage.

Les seuils initiaux capot ouvert sont de 78 °C CPU et 75 °C GPU. Le seuil capot fermé ne dépasse jamais 70 °C et requiert une qualification thermique préalable sur le Vivobook. Demucs limite ses fils de calcul à deux. Parole demande un profil Économie temporaire uniquement si le gestionnaire de profils sait le libérer à la déconnexion. TLP 1.11.0 a conservé une demande après disparition du client sur ArchASP. Avec TLP, Parole respecte donc le profil choisi dans QuickShell et applique les mêmes seuils. Au retour d’activité, à la reprise de Kara, au débranchement, à la réouverture du capot ou si une mesure requise devient indisponible, le traitement est interrompu. Les résultats validés restent réutilisables.

Le fichier d’unité livré est [kara.service](../contrib/systemd/user/kara.service). Son activation dans le gestionnaire utilisateur démarre Kara à la connexion graphique sans ouvrir Parole. Sur ArchASP, le service a été activé avec le mode nocturne capot ouvert et écran éteint, après autorisation. Les seuils initiaux ont été conservés. Les réglages système de logind et de swayidle n’ont pas été modifiés.

Le bilan de la dernière période d’inactivité est conservé dans l’index. Il affiche les chansons prêtes et restantes, les LRC corrigés ou générés, les échecs, les interruptions, les reports, le temps cumulé des tentatives clôturées et la variation du cache alloué. La cadence est calculée sur ces tentatives, y compris le temps passé sur les échecs. Un arrêt inattendu marque la tentative en cours comme interrompue et conserve les totaux clôturés. La reprise retire les brouillons marqués et retrouve les caches valides publiés avant l’arrêt.

Les interruptions terminent le groupe de processus et les moteurs libèrent leurs ressources. Les fichiers déjà publiés sont réutilisables. Une préparation interrompue pendant Demucs peut reprendre avec des pistes séparées si leur génération a été validée. Une partie et les sélections mobiles conservent leur chemin prioritaire. La suspension globale ne modifie pas la file d’importation ni une partie Kara.

Une recherche de paroles préserve les textes locaux et les corrections. Les écritures de tags utilisent le moteur existant et sa vérification des flux audio et image, avec copie des flux. Le LRC externe vérifié a la priorité. Après une recherche classique récente sans résultat et, si configuré, une attente bornée de Codex, Turbo peut utiliser le texte Strawberry ou sa correction locale. L’alignement n’ajoute aucun repère à une ligne sans appui acoustique. Une génération publie un LRC seulement si tous les débuts de ligne sont observés et si les seuils Kara sont atteints. Les mots incertains restent sans repère de notation. La couverture et l’erreur temporelle face à une référence annotée sont deux mesures distinctes.

L’album est comparé aux positions disque et piste et aux titres de l’édition MusicBrainz. Une correspondance par titre seule est insuffisante. La discographie utilise un MusicBrainz ID inscrit dans le fichier ou corroboré par des éditions locales distinctes. Les ambiguïtés, doublons et positions absentes sont consultables. Pour un morceau dont les métadonnées sont incomplètes, la maintenance peut joindre une identification MusicBrainz aux détails après recoupement du crédit, de l’édition et de la durée. Les suggestions restent séparées des tags et crédits originaux. Lorsque la récupération classique d’une piste manquante est indisponible, la recherche approfondie reçoit les positions et titres réellement manquants pour l’édition. L’import automatique requiert une édition vérifiée et un lien YouTube Music expressément autorisé dans son dialogue. Les pistes déjà reconnues par la file sont réutilisées. Les exclusions persistent. Les champs de droits ou les liens proposés par Codex ne valent pas autorisation.

## Codex et fournisseurs

Codex est désactivé au départ et utilise l’authentification ChatGPT existante après activation explicite dans Paramètres. Une recherche est limitée à 120 s et à deux départs locaux par jour par défaut. Le quota distant restant n’est pas mesurable dans Parole. Un signal de quota distant suspend les nouveaux départs pendant une journée. La recherche est lancée avec `codex exec`, une sortie JSON structurée, un espace jetable Bubblewrap et les fonctions d’exécution de commandes désactivées. Le seul fichier d’authentification monté est en lecture seule. L’audio, BlackBox, les répertoires personnels et les dépôts sont absents de l’environnement.

Un résultat est recevable si la CLI rapporte une recherche Web terminée avec des résultats et si la réponse respecte le schéma. La CLI autonome 0.172.0 utilise `codex-code-mode-host`, monté séparément en lecture seule. La recherche automatique est désactivée par défaut et la durée maximale est configurable de 15 à 600 s. Les résultats restent consultables dans Paramètres → Codex. Les [échecs d’import persistants](parole-imports.md#diagnostic-des-échecs-persistants) consomment le même quota et utilisent les mêmes règles d’isolation. Aucun texte ou contenu inventé n’est injecté dans la bibliothèque. Une ébauche d’adaptateur peut être sollicitée depuis Sources. Le texte de code, ses tests et ses fixtures sont visibles dans le résultat Codex pour revue. Leur exécution et leur intégration demandent une validation séparée.

Les sources enregistrées sont consultables, désactivables et mesurées pendant les opérations. La désactivation arrête les nouvelles recherches utilisant cette source ainsi que les nouveaux imports automatiques qui en dépendent. Une source compatible LRCLIB reçoit une configuration avec `host`, `path`, `terms` et `fixtures`. Chaque fixture indique `title`, `artist`, `album`, `duration`, éventuellement `album_artist`, `synced` et `sha256`. La valeur `sha256` vaut `null` pour un résultat attendu négatif, ou l’empreinte du texte vérifié pour un positif. Il faut au moins deux positifs et un négatif indépendants. Après confirmation que les conditions permettent les requêtes automatisées, Parole vérifie ces cas en arrière-plan. En cas de succès, la source est activée et les échecs de paroles ou de LRC redeviennent progressivement éligibles. Le format accepte uniquement l’adaptateur approuvé et ses paramètres JSON. Les destinations configurables sont publiques, les redirections sont refusées et chaque résolution DNS est épinglée à des adresses autorisées avant l’établissement de TLS.

Dans **Paramètres → Sources**, les limites du fournisseur sélectionné règlent son quota de requêtes HTTP de maintenance et son intervalle minimal. Le quota initial est de 200 requêtes par jour et le réglage de l’intervalle peut renforcer le minimum du client ou du fournisseur. Un quota de zéro suspend les nouvelles requêtes. Les compteurs par fournisseur et les temporisations de l’hôte s’appliquent aussi à la dernière recherche nocturne. Cette recherche partage le quota global des opérations classiques. Les préparations qui réutilisent un LRC fiable ou un candidat déjà vérifié n’en consomment pas.

Le statut Local, Externe ou Généré du LRC apparaît dans le tableau. Les détails indiquent la provenance, la couverture si mesurée et les éventuels échecs. La console prend ses messages dans les événements des opérations, et ne recopie pas les détails des processus Codex.

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

| Mesure observée | Résultat |
| --- | --- |
| GTK dans Sway headless, bibliothèque temporaire | 1,357 s à la première image, 202 Mio RSS et 1,20 % d’un cœur au repos |
| Inventaire réel, 80 fichiers | 0,283 s au premier passage, 0,212 s à chaud, 0 modification au second passage |
| Demucs CPU, échantillon de 22,754 s | 19,575 s, lecture du cache en 0,302 s |
| Turbo Vulkan sur le même échantillon | 22,297 s pour le processus complet, 2,392 s d’analyse reportée, 93,83 % de mots couverts, 1,657 s pour la relecture du cache |
| Turbo Vulkan avec le même texte sans minutage | 14,30 s, 93,83 % de mots et 80 % de débuts de ligne couverts. Publication refusée. L’original est resté identique. |
| Pic de charge GPU pendant Turbo | 97 % observés sur le périphérique, charge agrégée |
| Taille de préparation de l’échantillon | Environ 2,57 Mo pour les deux FLAC et le manifeste |
| Cache existant mesuré en lecture seule | Environ 307 Mio sur la machine à la date du relevé |
| Fournisseurs publics | MusicBrainz et LRCLIB ont répondu à une requête publique de qualification |
| Codex CLI 0.172.0 | Après montage du compagnon, recherche native avec résultats et réponse conforme au schéma Parole. Documentation publique LRCLIB vérifiée en 15,8 s dans Bubblewrap. |

La qualification ne fixe pas la précision de 500 ms. Elle demande un corpus dont les débuts de ligne et de mot sont annotés indépendamment. Les temps observés sur un morceau court ne donnent pas la moyenne de la bibliothèque. La TV physique, la découverte effective d’un nouveau fournisseur par Codex et une campagne d’importation automatique sur la collection réelle restent à qualifier.

### Essais nocturnes sur ArchASP

Le [banc de performances](parole-performances.md) décrit la campagne élargie, la mesure par étape et le témoin temporel indépendant.

Deux morceaux complets ont été traités sur des copies privées, capot ouvert, avec Demucs CPU à deux fils et Turbo Vulkan. Les mesures utilisent temporairement le profil Économie de TLP. Le profil Performance a fait dépasser 85 °C au CPU pendant l’essai initial. En Économie, les essais terminés ont mesuré au plus 74,9 °C au CPU et 61 °C au GPU. Le décodeur strict a confirmé `Vulkan0`. Lorsque le pilote a été masqué pour le seul processus de test, il a quitté avec le code 4 avant d’annoncer sa disponibilité.

| Morceau entier | Demucs | Turbo avec LRC | Couverture mots / lignes | Repères corrigés | Cache alloué |
| --- | ---: | ---: | ---: | ---: | ---: |
| Rap rapide, 6 min 04 | 386,2 s | 764,3 s | 84,9 % / 82,4 % | 120 | 69,6 Mio |
| Chant, 4 min 36 | 282,5 s | 191,4 s | 95,2 % / 92,9 % | 32 | 51,3 Mio |

Les deux corrections sont validées dans Kara. Aucun repère corrigé ne dépend d’une interpolation seule. Les originaux audio et LRC ont été vérifiés après l’essai. Sur les deux passages validés, la cadence observée est de 4,43 chansons par heure de calcul et le cache total est de 120,8 Mio, soit environ 268 Mio par heure de calcul. Le calcul additionne les mesures Demucs et Turbo des deux parcours validés et exclut les essais supplémentaires et interruptions.

Les deux morceaux ont ensuite été traités à partir de leurs seules paroles. Les durées Turbo étaient de 698,5 s et 192,1 s. La génération a refusé les deux publications, faute d’appui acoustique sur tous les débuts de ligne. Les fixtures vérifient également le cas publiable. Les couvertures mesurent les repères établis par le moteur et ne mesurent pas l’erreur face à une annotation indépendante.

Un essai Turbo s’est arrêté lorsque l’état secteur est devenu indéterminé. La reprise a retrouvé la séparation du morceau de rap rapide en 0,5 s. Sur un troisième morceau, la perte effective du secteur a arrêté Demucs après 280 s, avant publication. Un débranchement physique pendant une transcription Vulkan a aussi arrêté le groupe Turbo avant publication, 32,1 s après le début de la transcription. La réserve a été vérifiée sur les volumes de la bibliothèque et sur une partition de test offrant 7,24 Gio libres, rejetée sans remplissage artificiel.

Un service utilisateur transitoire a démarré la maintenance sans fenêtre sur une bibliothèque isolée. Une terminaison forcée a confirmé la libération des descripteurs logind. Avec les inhibiteurs de test `idle:block` et `sleep:delay` en place, la veille manuelle a été acceptée. Le signal `PreparingForSleep` a entraîné leur libération, et le noyau a confirmé l’entrée et la sortie de `s2idle` après 12,74 s.

Sur le bureau réel, l’option **Maintenir éveillé** de QuickShell bloquait le verrouillage et l’extinction automatiques. Après sa désactivation par l’utilisateur, le signal d’inactivité, le verrouillage QuickShell et l’extinction Sway ont été observés sur secteur, capot ouvert. Le service installé a ensuite démarré sans fenêtre et terminé l’inventaire initial sans erreur.

Un essai sur secteur a confirmé la fermeture et la réouverture du capot. Il a duré 24,3 s capot fermé, avec des maxima de 66 °C au CPU et 56 °C au GPU, sans calcul lourd. Une qualification prolongée sous charge reste nécessaire avant d’activer la préparation capot fermé. La préemption par une partie Kara a été vérifiée sur fixtures avec un vrai sous-processus supervisé.
