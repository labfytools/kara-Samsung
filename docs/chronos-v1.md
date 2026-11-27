# CHRONOS dans Kara

## Pendant une partie

Choisir un parcours dans **Minutage** avant de préparer la sélection. Huang
utilise Open-Unmix HQ et peut solliciter HTDemucs-FT si les indices phonétiques
signalent un passage difficile. Wav2Vec2 reprend le chemin phonétique du rap
avec une voix HTDemucs. L’option Huang / HTDemucs-FT demande cette vue
explicitement. La sélection est indépendante du genre déclaré.

Le moteur local utilise le texte présent dans le fichier voisin, la référence
historique récupérée ou les tags. Il garde le texte source et associe les
résultats à l’empreinte SHA-256 du fichier et à celle du PCM. L’accord avec Turbo
et le contraste phonétique sont des indices. Les mots ou lignes sans repère
corroboré restent visibles avec un temps absent. Une avance du surlignage par
mot exige un préfixe de repères observés. Le bouton **Minutage historique**
rétablit l’affichage historique pour les prochaines sélections. La notation de
partie continue d’utiliser la référence historique et ses seuils. La justesse
mélodique n’entre pas dans le calcul CHRONOS.

Les calculs CHRONOS sont limités à la préparation interactive et aux
recalculs explicites. Ils réutilisent le superviseur Parole et le verrou
local du moteur. Les protections secteur, température, PSI, RAM et disque
peuvent suspendre le calcul et laissent le parcours historique disponible.
Le groupe de calcul est limité à 8 Gio et à deux cœurs de calcul. Les vues
vocales temporaires sont supprimées à la fin du travail, y compris sur sortie
contrôlée. Les reçus de minutage privés occupent au maximum 128 Mio.
Les tâches nocturnes continuent leur parcours de recherche de paroles et
de préparation existant. Le moteur expérimental ne publie aucun LRC dans
la bibliothèque.

## BlackBox Studio

Démarrer une capture avant le tour. Le mode vocal et le consentement du
microphone restent ceux de BlackBox V3. Dans **Rapport et rejeu**, sélectionner
**Référence des paroles** pour consulter ensemble les paroles originales,
les repères historiques et expérimentaux, les modèles et leurs empreintes,
les refus, le moteur de vérification et les indices par mot.

Dans **Chronologie**, choisir le tour et afficher un intervalle. Studio
projette les repères musicaux entre deux ancres de position Strawberry
continues. Les sauts et les zones sans ancres sont laissés vides. La
musique et la voix guide sont relues si leurs fichiers locaux correspondent
toujours aux empreintes. L’enveloppe de la voix isolée par CHRONOS est
conservée sur son horloge d’échantillonnage même après suppression du WAV.
Les pistes du joueur apparaissent selon le consentement et la couverture
effective. Les événements TV, PC, microphone et notation restent dans le
journal V3. Cette chronologie logicielle conserve l’incertitude de délai
physique entre lecture, microphone et affichage.

**Recalculer CHRONOS hors ligne** utilise l’enregistrement original vérifié par
empreinte. Studio présente les écarts par mot et ajoute une piste de repères
à la chronologie. Les repères d’origine restent dans la capture. Le score
expérimental réévalue les fenêtres de reconnaissance enregistrées avec les
nouveaux débuts admissibles. Les mots incertains n’entrent pas dans son
dénominateur de mots évaluables. Le reçu indique la couverture et la note
historique séparément. Les fenêtres restent celles du tour enregistré.
Un texte différent ou une fenêtre absente peuvent limiter la comparaison.
Les observations du joueur gardent leur statut de reconnaissance machine.

```sh
bin/kara-blackbox lyrics-reference SESSION --turn TOUR
bin/kara-blackbox chronos-replay SESSION --turn TOUR --chronos-engine wav2vec2 --output ./comparaison.json
bin/kara-blackbox chronos-edition SESSION --turn TOUR --alternate-audio /chemin/autre-version.flac --output ./edition.json
```

Le comparateur d’éditions rapporte les fenêtres acoustiques distinctives,
les deux empreintes, les durées et la carte affine dans la région confirmée
lorsqu’elle existe. Il conserve les deux enregistrements et les paroles.
Si l’enregistrement original a déménagé, `--original` accepte un chemin
explicite sous la même empreinte. Les sorties de rejeu sont hors de
l’inventaire BlackBox et publiées sans remplacement.

## Installation locale et retour arrière

Le service local utilise un drop-in utilisateur dédié à cette expérimentation.
Son fichier d’environnement pointe vers les poids CHRONOS préexistants,
le Python du runtime Demucs et les dépendances locales Python 3.12.
Les poids et les dépendances tierces ne font pas partie de ce lot Git.
Pour revenir à la version précédente, pendant que la salle est inactive,
retirer uniquement le drop-in CHRONOS, recharger systemd utilisateur puis
redémarrer Kara. Le lanceur historique et les dossiers des captures et
résultats restent utilisables.

Le dépôt amont de Huang affiche MIT pour le code. Le checkpoint est
proposé par lien externe dans le README. La documentation consultée
n’explicite pas séparément les droits de redistribution de ce fichier.
Le déploiement expérimental utilise exclusivement le checkpoint local
épinglé par SHA-256. [Projet Huang et Benetos](https://github.com/jhuang448/LyricsAlignment-Multilingual)
