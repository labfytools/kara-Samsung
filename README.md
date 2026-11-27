# Kara et Parole

Kara affiche sur une Samsung Tizen les paroles du morceau joué dans Strawberry. Lance un morceau sur le PC, ouvre Kara sur la télévision et chante avec le microphone de l’enceinte EDENWOOD. Le volume musical se règle dans Strawberry. Le retour du micro se règle sur l’enceinte.

La télévision suit la position de Strawberry, y compris après une pause ou un déplacement. Elle affiche le titre, l’artiste, la pochette et les paroles. Les repères d’origine sont utilisés au mot quand le LRC en contient, ou à la ligne. En l’absence de LRC, Kara affiche le texte simple disponible. **Minutage**, dans l’en-tête de Parole, permet de régler le décalage sans éditer le fichier.

## Parole

Parole gère les imports, la recherche multisource, les albums, les métadonnées, l’indexation et la synchronisation USB. **Minutage** permet aussi de rechercher le LRC du morceau en cours.

**Paramètres → LRC nocturnes** affiche la couverture de la bibliothèque et la progression de la nuit. Sur secteur et session verrouillée, la maintenance récupère les LRC d’origine manquants. La file SQLite conserve les échéances et reprend après redémarrage.

- [Guide Parole](docs/parole-v3.md)
- [Imports et téléchargements](docs/parole-imports.md)
- [Fournisseurs de paroles](docs/parole-sources-v3.md)
- [Fonctionnement capot fermé](docs/parole-capot-ferme.md)
- [Installation Samsung](tizen/app/README.md)
- [Diagnostics et archives BlackBox](blackbox/README.md)

## Installation locale

Le poste utilise Strawberry, MPRIS, Python avec GTK 4 et PyGObject, et les utilitaires d’import spécifiques aux sources utilisées. Le service utilisateur [kara.service](contrib/systemd/user/kara.service) démarre avec la session graphique. Le lanceur Parole ouvre la bibliothèque depuis le menu des applications. L’utilitaire `bin/kara-tv-package` prépare le paquet à signer dans un dossier privé.

Les anciens parcours sont conservés dans les branches locales `archive/kara-avant-back-to-basics-20261127` et `archive/kara-multijoueur-2.1.0`. Les données privées et les captures restent dans l’espace utilisateur.

Pour revenir à une version précédente, restaurer le raccourci, l’unité utilisateur et ses paramètres de lancement depuis la sauvegarde de déploiement, puis redémarrer le service. Le paquet Samsung signé précédent est conservé pour le retour arrière. La restauration de la version s’effectue sur les données présentes afin de garder les imports et LRC acquis depuis la mise à jour.
