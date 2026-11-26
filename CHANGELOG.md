# Journal des changements

## En développement

- Supprimer le plafond et l’intervalle globaux de la maintenance réseau, isoler les temporisations par fournisseur et compter les paroles et LRC publiés par heure.
- Traiter les recherches indépendantes en parallèle et alterner les lots réseau avec une seule préparation Kara.
- Armer l’inhibiteur de capot avant la fermeture sur secteur, confirmer le verrouillage Sway avant l’extinction et porter la durée nocturne par défaut à huit heures.
- Alterner la préparation Kara avec les recherches et vérifications capot fermé, même sans fenêtre Parole, et conserver un bilan par catégorie.

- Mettre en pause les nouvelles requêtes YouTube dès une limitation explicite ou une série de réponses ambiguës, conserver la temporisation après redémarrage et regrouper les diagnostics des demandes à vérifier. Les autres sources restent éligibles.

- Déclencher la fin de mesure yt-dlp uniquement à la fin du corps audio ou à l’entrée en conversion, y compris lorsque yt-dlp exécute des postprocesseurs avant le téléchargement.

- À la reprise d’un lot, préparer en priorité les albums encore en attente et conserver les tentatives interrompues ou en échec pour la suite du même lot.

- Alterner les sources d’un lot dès la préparation des albums, prendre en compte l’attente de créneau dans le choix entre éditions équivalentes et afficher l’origine des limites dans Parole.

- Libérer le créneau global pendant la résolution YouTube et transférer la conversion yt-dlp à la file de traitement, en conservant le profil réseau du fournisseur et les fichiers de reprise.

- Figer les compteurs au début de chaque mesure pour permettre au régulateur de monter après une réduction en production. Consigner les essais sur des originaux CC0 d’Internet Archive.

- Supprimer le plafond local de 512 Kio/s, partager les travailleurs audio entre les parcours CLI et Parole et permettre au régulateur de remonter avant la fin de publication d’un album.

- Distinguer les pistes qui reçoivent des octets des créneaux occupés par une connexion ou une conversion.

- Appliquer l’accent Lavender aux barres d’import avec un thème GTK local qui impose une autre couleur.

- Préserver l’exécution de la maintenance quand une recherche est préparée à partir d’une édition ou d’une piste manquante.

- Régler le plafond global des téléchargements audio entre 1 et 20, avec priorité manuelle, adaptation à la cadence et aux ressources, et statistiques de transfert mesurées.

- Alimenter les créneaux de transfert par piste depuis plusieurs albums du même lot, avec publication audio et enrichissement indépendants.

- Donner à la liste des imports toute la hauteur disponible et garder le bouton Historique au pied de la fenêtre.

- Retirer immédiatement les lots effacés, y compris les échecs, de la file visible et de ses compteurs. Permettre le retrait individuel et conserver les reçus et les résultats de reprise.

- Nettoyer l’historique des imports par sélection, résultat ou durée, avec une visibilité partagée entre Parole et la salle et des compteurs centrés sur la file.
- Suspendre les téléchargements et enrichissements actifs de la file d’import, conserver leurs reçus de reprise et partager l’état confirmé entre Parole et le mobile.
- Publier l’audio des imports gérés avant les recherches de paroles et de pochettes. Confier l’enrichissement à la file de maintenance persistante.
- Mutualiser les recherches de texte et de LRC, limiter les requêtes par fournisseur et conserver les résultats vérifiés et les absences confirmées.
- Afficher la cadence mesurée des imports et de l’enrichissement, instrumenter les attentes et limiter les lectures sous le verrou de publication.
- Conserver l’artiste sélectionné dans les imports de discographie confirmés et distinguer ce classement des crédits audio.
- Autoriser l’enrichissement pendant un transfert audio lorsque Kara est disponible.
- Regrouper les dossiers d’édition de manière réversible, avec reçu de déplacement, associations par album et conservation des pistes et de leurs fichiers associés.
- Ajouter la sélection multiple d’artistes, l’aperçu des fichiers, la confirmation et l’annulation dans Parole.
- Conserver aussi l’artiste de discographie dans les demandes de salle enregistrées avant approbation.
- Apprendre le délai et le taux de correspondance par langue, genre et forme de paroles. Allonger la temporisation lorsque des erreurs HTTP se répètent.
- Réveiller la maintenance à l’admission des albums et enchaîner les tâches d’enrichissement prêtes.
- Rétablir le bouton de synchronisation USB Labfy dans l’en-tête de Parole, avec aperçu, suivi des opérations vérifiées et bilan dans la catégorie `SYNC`.
- Exclure les imports en attente et les répertoires techniques de Kara dès l’inventaire USB.
- Afficher l’espace Android, les compteurs incrémentaux et les mesures USB. Ajouter la suspension avec reprise et l’abandon explicite d’un plan interrompu.
- Transmettre les déplacements d’albums du registre Parole pour éviter les recopies USB lorsqu’une ancienne version Android est identique.

- Documenter dans `AGENTS.md` les périmètres de commit, les contrôles avant chaque commit et la séparation entre développement et agent de recherche Parole.
- Conserver le parcours Kara V2 avec salle mobile, démarrage TV autonome, BlackBox, mixage et file d’import persistante commune au bureau et au mobile. Voir `docs/kara-v2.md` et `docs/parole-imports.md`.
- Aligner les textes sans minutage avec Turbo et publier un LRC seulement après validation acoustique de toutes ses lignes. Réutiliser les préparations et pistes Demucs sur preuve d’identité PCM.
- Indexer la bibliothèque en lecture seule côté Strawberry et traiter les recherches et vérifications par une file de maintenance persistante. Ajouter le registre de fournisseurs et l’appel Codex isolé, soumis à un quota local.
- Afficher la bibliothèque, les statistiques et la console dans la fenêtre Parole. Réutiliser la file d’import et la connexion au téléviseur depuis l’en-tête, avec le thème Catppuccin Mocha.
- Limiter les relances classiques des imports, mutualiser les diagnostics Codex par signature et reprendre les mêmes lots après confirmation indépendante d’une référence de catalogue. Conserver les échecs à vérifier et leur historique.
- Appliquer la désactivation des fournisseurs aux vérifications MusicBrainz et à l’admission d’imports automatiques YouTube Music.
- Vérifier la libération du groupe de processus lors d’une sélection Kara ou d’un retour d’activité. Documenter l’essai Vulkan réel qui refuse une publication sans repères suffisants.
- Vérifier les identifications associées aux métadonnées incomplètes et transmettre à Codex les positions manquantes d’une édition vérifiée.
- Ajouter un point d’accès local aux captures et rapports BlackBox dans `blackbox/`, sans déplacer les données privées ni les versionner.
- Préparer les références Kara en arrière-plan à partir de LRC vérifiés ou de textes sans minutage, en conservant les originaux et en refusant les corrections sans appui acoustique.
- Rétablir la recherche Web native de Codex dans l’environnement isolé et laisser l’automatisation désactivée par défaut.
- Ajouter la préparation nocturne sur secteur, séquentielle et sans plafond quotidien, sous la supervision du service Kara. Porter la réserve de disque à 20 Gio.
- Configurer les délais et quotas des recherches classiques, avec une régulation persistante par fournisseur.
- Afficher dans Parole le bilan persistant de la dernière session nocturne, avec la cadence et le stockage mesurés.
- Exiger un backend Vulkan initialisé pour les analyses nocturnes et conserver le profil système sur les versions de TLP dont les demandes survivent au client.
- Documenter les essais nocturnes ArchASP sur deux morceaux complets et la validation sur secteur, écran éteint.
- Ajouter un banc silencieux de préparation sur dix morceaux complets et un comparateur de repères sur annotations indépendantes.
- Distinguer références temporelles et préparations complètes, et afficher les temps moyens observés et les blocs mutualisés dans le bilan nocturne.
- Continuer la recherche locale des débuts de ligne manquants lorsque la transcription d’un texte sans minutage couvre déjà au moins 95 % des mots.
- Réutiliser la première transcription Vulkan lors d’une nouvelle tentative sur le même audio et dans la même langue, puis rejouer l’alignement et les reprises locales.
- Importer les dossiers et les archives ZIP dans la file Parole, avec provenance, préservation des originaux et reprise des seules pistes manquantes.
- Proposer le téléchargement officiel Bandcamp et conserver sa provenance déclarée lors de l’import local de l’album.
- Accepter les ZIP et dossiers FLAC des achats Qobuz et conserver la référence publique de l’édition.
- Importer individuellement les pistes téléchargées via la fonction officielle SoundCloud, avec leur provenance déclarée.
- Importer les téléchargements officiels de Free Music Archive avec une page publique et une licence compatible renseignées.
- Conserver la page publique de téléchargement et la licence déclarée pour les fichiers remis par les artistes et labels.
- Rechercher et importer les fichiers audio originaux publics d’Internet Archive après vérification de la licence et de l’intégrité des transferts.
- Intégrer l’API Jamendo pour les albums et pistes autorisés lorsque l’identifiant d’application est configuré, avec un parcours local dans les autres cas.
- Permettre la découverte explicite de fournisseurs audio autorisés par Codex et la préparation d’adaptateurs à relire avant leur intégration.
- Mesurer les recherches, les transferts, les importations et les limitations réelles par fournisseur.
- Comparer les éditions multisources à partir de preuves normalisées en conservant les tags et les mentions de version.
- Regrouper les offres automatiques d’une édition confirmée et sélectionner un fournisseur selon la vitesse, la qualité ou la préférence.
- Conserver les identités et les provenances dans l’index existant et éviter une nouvelle copie quand l’identité locale est prouvée.
- Afficher les preuves des rapprochements et accepter des alias d’artiste validés explicitement pour une édition.
- Conserver les empreintes Chromaprint disponibles comme preuve complémentaire des rapprochements entre encodages.
- Réutiliser les positions déjà intégrées d’une édition confirmée avant de lancer un transfert multisource.
