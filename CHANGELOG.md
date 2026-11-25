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
