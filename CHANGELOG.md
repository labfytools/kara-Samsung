# Journal des changements

## En développement

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
