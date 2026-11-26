# Qualification audio Omega

## Conditions de comparaison

Le corpus de transcription est celui des 50 extraits déjà évalués. Le banc relit les identifiants et les intervalles d’origine. Il contrôle le nombre de mots attendu sur chaque intervalle et fige les empreintes audio et les mots de référence. Les fichiers d’annotations indépendantes des chansons entières portent aussi leur empreinte. Le recalcul du mix permet de détecter une divergence avec les scores historiques. Les 8 899 entrées du corpus lexical de référence sont conservées à part. Le comparatif phonétique utilise l’export historique qui exclut les titres évalués et leurs équivalents détectés.

Les deux moteurs s’exécutent dans des groupes de processus successifs. Chaque rapport garde les temps et les mesures par étape. Le mode de comparaison charge les poids locaux existants et lit la bibliothèque sans écriture. Il peut réutiliser une séparation complète validée par PCM, modèle et intégrité FLAC. En l’absence de cette preuve, les essais courts séparent la fenêtre avec sept secondes de contexte de chaque côté avant de recouper l’intervalle d’origine. Le dossier de qualification est privé et distinct de la bibliothèque.

Le seuil nocturne reste à 1 % de PSI. Le comparatif manuel à 3 % conserve une réserve de 4 Gio pendant le traitement et les contrôles de température, d’activité Kara et d’espace disque. En mode strict, le banc exige aussi 4 Gio avant le départ et arrête le traitement au seuil normal de 1 %. Le PSI `some avg10` mesure le blocage de tâches sur l’hôte. Le PSS échantillonné, le pic RSS du worker et les compteurs AMD GTT mesurent des phénomènes différents. L’iGPU utilise la RAM de la machine. [Documentation PSI du noyau Linux](https://docs.kernel.org/accounting/psi.html)

## Configurations

| Variante | Entrée et règle |
| --- | --- |
| A · mix | Mono 16 kHz, une lecture de l’extrait, recherche de langue automatique. Les chansons entières suivent les fenêtres de production. |
| B · voix | Même décodeur, piste vocale HTDemucs, même origine temporelle. |
| C · niveau | Ajustement RMS vers 12 % de l’amplitude pleine échelle, variation limitée à ±3 dB et protection contre l’écrêtage. |
| D · filtre léger | Passe-haut à deux pôles à 75 Hz sur la voix isolée. |
| E · découpage | Recherche du creux d’énergie sur 200 ms dans le stem vocal. Cœurs d’environ 6 à 12 s, marges de deux secondes, attribution unique d’un mot par son début. Le moteur évalue le signal sur l’ensemble des cœurs. |
| F · relecture | Une seule fenêtre de 5 s autour du mot le moins sûr du stem. Faisceau de taille 3, validation croisée avec le mix à 220 ms près, seuil de 0,65 pour les deux observations. Les corrections restent sur les emplacements existants. |
| G · combinaison | Le mix fournit les emplacements de départ. La voix brute et la voix filtrée doivent confirmer chacune le même remplacement. Les repères d’origine sont conservés. |

La passe phonétique facultative utilise au plus cinq formes issues du lexique tenu à part. Une correspondance phonétique et la relecture sans contexte doivent soutenir toute modification. La campagne sépare le coût marginal de réanalyse du coût de la passe de base. Pour le banc, les scores de confiance servent à sélectionner un contrôle. L’exactitude se mesure sur la référence indépendante du décodeur.

Le WER et le CER utilisent la normalisation Kara, appliquée à la référence et à la sortie. Le CER compte les caractères du texte normalisé, espaces compris. Les insertions éclairent les erreurs d’ajout. Elles peuvent aussi représenter des ad-libs ou des chœurs absents du texte de référence. Sur les chansons annotées, les mots produits dans des intervalles de plus de deux secondes entre annotations sont recensés séparément. Les noms propres, les changements de langue, les voix superposées et les refrains nécessitent des annotations propres à ces phénomènes pour obtenir des taux interprétables. Le compteur d’argot se limite aux formes du vocabulaire Kara et mesure leur présence, sans faire une annotation phonétique.

Pour les chansons entières, les repères JamendoLyrics sont annotés indépendamment de Turbo. L’alignement des hypothèses suit l’ordre lexical, avec les écarts temporels comme départage à coût lexical égal. Un mot ou un début de ligne manquant reste au dénominateur de la couverture. Les comparaisons temporelles distinguent la couverture et la précision conditionnelle des repères appariés. Elles détaillent les seuils de 250, 500 et 1 000 ms ainsi que la médiane, les p90 et p95 et le maximum. Les fins émises par Turbo reposent sur la borne acoustique disponible, parfois le jeton suivant ou la fin de segment. [JamendoLyrics](https://github.com/f90/jamendolyrics) · [Jam-ALT](https://audioshake.github.io/jam-alt/)

## Premier diagnostic mesuré

Les deux anciennes exécutions se sont interrompues avant Turbo. La mémoire du modèle de transcription ne pouvait donc pas être la cause de ces arrêts précis. Les journaux n’ont conservé aucun échantillon mémoire de ces deux exécutions. Une reproduction stricte a arrêté Demucs en phase de séparation à 1,12 % de PSI, avec 5,21 Gio disponibles. Une tentative séparée a arrêté le lancement de Turbo F16 sur un titre entier à 1,33 % de PSI, avec 4,26 Gio disponibles. Sur le comparatif manuel de ce titre, le mix et le stem ont été décodés. La phase adaptative a ensuite été suspendue après 369 s lorsque la mémoire disponible est descendue à 3,88 Gio. Le PSI était alors de 0 %, après un pic de 2,39 % pendant le parcours. Le GTT maximal du système a atteint 7,08 Gio.

Sur une chanson entière de 193,68 s, les réglages CPU ont conservé deux fils PyTorch, le contexte de 7 s, 10 % de chevauchement et zéro décalage. Le candidat consomme les segments en série et garde le même ordre de sommation. Les deux fichiers FLAC et leurs PCM respectifs étaient identiques pour cet essai. Les durées couvrent le worker Demucs, de ses imports à la fin de l’encodage. Les conditions de charge et de récupération de mémoire de l’hôte ont varié entre les deux passages.

| Mesure | Demucs avec travailleur unique | Segments consommés en série |
| --- | ---: | ---: |
| Temps total | 221,82 s | 208,88 s |
| Pic RSS du worker | 1 664 Mio | 1 800 Mio |
| PSS maximal échantillonné du groupe | 1 561 Mio | 1 781 Mio |
| Mémoire disponible minimale | 4,91 Gio | 5,10 Gio |
| PSI maximal | 2,46 % | 1,14 % |
| Maximum CPU et GPU | 67,88 et 54 °C | 62,38 et 55 °C |
| Pages écrites en swap sur l’hôte | 514 264 | 230 726 |

La variante diminue le PSI et le temps sur cet essai. Son PSS plus élevé et le franchissement de 1 % écartent une conclusion de gain mémoire reproductible. Demucs 4.0.1 expose directement la consommation séquentielle avec `-j 0`. Le réglage nocturne de production conserve son exécution qualifiée précédemment. [Découpage Demucs](https://github.com/facebookresearch/demucs/blob/main/demucs/apply.py) · [Exécuteur séquentiel Demucs](https://github.com/facebookresearch/demucs/blob/main/demucs/utils.py)

Le relevé par étape place le pic du worker pendant la séparation proprement dite. Dans l’essai sériel, le maximum RSS cumulé du worker atteint environ 493 Mio après le chargement des poids et 557 Mio après le décodage. La séparation porte ce maximum à 1 800 Mio. À l’encodage FLAC, le PSS courant maximal échantillonné tombe à environ 1 099 Mio dans le groupe. Le pic de séparation combine les poids et les allocations de calcul et de restitution. Ces mesures permettent de localiser le pic sans attribuer chaque page au modèle ou à un tampon particulier.

### Pilote lexical et audio

Les trois premiers extraits du manifeste, en rock et dans les deux groupes de rap, ont fourni 52 mots de référence. Le mix a rendu 25 erreurs, la voix brute et la voix normalisée 30 chacune et la segmentation adaptative 31. La relecture ciblée et la relecture phonétique ont laissé toutes les graphies du stem en place. L’écart entre le mix et le stem est concentré sur le rap américain du pilote. Ce résultat décrit ces trois cas et motive une qualification par genre.

### Résultat apparié des 50 extraits F16

Les **50 extraits et 1 317 mots** sont couverts. Les substitutions, suppressions et insertions du mix reproduisent les valeurs historiques pour chacun des 50 titres. La première passe a terminé 39 extraits. La deuxième a récupéré les 11 cas reportés en relisant les séparations et les variantes terminées. Les deux passes ont conservé le profil d’essai et le même manifeste. Les poids F16 et les variantes restent distincts de l’essai Q5 sur chanson entière.

| Variante | Erreurs | WER | CER | Substitutions / suppressions / insertions | Passages incertains | Temps mesuré |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A · mix | 620 | 47,08 % | 37,55 % | 222 / 299 / 99 | 138 | 501,87 s |
| B · voix | 645 | 48,97 % | 37,02 % | 206 / 343 / 96 | 134 | 496,56 s |
| C · niveau | 644 | 48,90 % | 37,20 % | 212 / 340 / 92 | 138 | 490,09 s |
| D · filtre léger | 634 | 48,14 % | 38,48 % | 180 / 367 / 87 | 123 | 486,48 s |
| E · découpage | 691 | 52,47 % | 41,17 % | 195 / 401 / 95 | 135 | 723,74 s |
| F · relecture | 652 | 49,51 % | 37,58 % | 213 / 343 / 96 | 134 | 246,28 s en plus |
| G · combinaison | 628 | 47,68 % | 38,52 % | 228 / 300 / 100 | 138 | moins de 0,01 s en plus |
| Relecture phonétique de F | 652 | 49,51 % | 37,58 % | 213 / 343 / 96 | 134 | 281,21 s en plus |

Les durées A à E cumulent les mesures du décodeur par variante, hors chargement et séparation. F et la passe phonétique chiffrent leur travail supplémentaire. G compare les observations déjà obtenues en A, B et D. Les insertions mesurent les ajouts face à la référence, avec une ambiguïté possible pour les ad-libs ou les chœurs. La relecture F a changé 11 mots et la combinaison G en a changé 43, avec une dégradation du WER dans les deux cas. La relecture phonétique a été tentée sur chaque extrait, sans remplacement accepté. Les extraits n’offrent pas d’annotation statistique des noms propres, des contractions ou de l’argot. Aucune occurrence n’entrait dans le compteur des formes d’argot de cette version.

| Genre | Extraits / mots | A mix | B voix | C niveau | D filtre | E découpage | F relecture | G combinaison |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Rock | 6 / 124 | 22,58 % | 29,84 % | 29,03 % | 20,97 % | 37,90 % | 29,84 % | 21,77 % |
| Rap américain | 16 / 431 | 57,54 % | 66,59 % | 67,52 % | 66,13 % | 62,18 % | 67,05 % | 58,00 % |
| Rap français | 20 / 594 | 47,81 % | 46,80 % | 46,30 % | 46,63 % | 55,22 % | 47,47 % | 48,82 % |
| Chanson française | 2 / 36 | 8,33 % | 11,11 % | 11,11 % | 13,89 % | 11,11 % | 11,11 % | 16,67 % |
| Reggae | 4 / 83 | 49,40 % | 32,53 % | 31,33 % | 31,33 % | 37,35 % | 33,73 % | 48,19 % |
| Soul | 2 / 49 | 32,65 % | 24,49 % | 24,49 % | 30,61 % | 26,53 % | 24,49 % | 30,61 % |

Un rééchantillonnage descriptif de 4 000 tirages appariés par titre, avec quotas de genre fixes, estime l’écart voix moins mix entre −4,05 et +8,05 points pour l’intervalle à 95 %. Pour le découpage, l’intervalle va de +0,15 à +10,77 points. La sélection par confiance a essayé des marges entre 0 et 0,12 avec un rapport de nombres de mots entre 80 et 120 % du mix. Le choix de la règle a été entraîné sur les autres genres à chaque tour. Cette sélection donne 47,53 % de WER. Un choix rétrospectif par titre entre le mix, la voix brute et la voix filtrée descendrait à 40,17 %. Il utilise les références et borne seulement le gain possible entre ces trois hypothèses. Les effectifs de deux à quatre titres demandent davantage de données avant de qualifier un réglage spécifique au genre.

Pour les 13 extraits dont le texte de référence a la densité la plus élevée, à partir de 2,66 mots par seconde sur l’intervalle entier, le mix est à 44,27 % de WER et le stem adaptatif à 45,06 %. Dans les 24 extraits du milieu de la distribution, le WER passe de 46,70 % à 55,16 % et les suppressions de 130 à 231. Cette densité comprend les silences et n’équivaut ni au débit vocal instantané ni au tempo musical.

Les 47 nouvelles séparations ont duré 32,45 s en médiane, avec des pics PSS de groupe de 1 189 à 1 514 Mio et une température CPU maximale de 57,38 °C pendant les traitements terminés. Trois séparations ont été reprises du pilote. Les pics PSS échantillonnés pendant les phases Turbo terminées vont de 160 à 258 Mio. Les compteurs DRM du décodeur F16 indiquent jusqu’à 2 089 Mio de GTT résident. Le GTT de l’hôte a atteint jusqu’à 7,14 Gio pendant les phases Turbo terminées. La mémoire disponible minimale est restée à 4,24 Gio sur ces phases. Parmi les 11 reports, 10 correspondent au PSI et un à la réserve disponible. Deux reports se produisent dans Demucs et neuf dans Turbo, parmi le chargement du modèle, les buffers PCM et le décodage du mix.

Sur la chanson entière de 193,68 s, le premier essai Demucs avec fenêtres de cinq secondes s’est arrêté après environ treize secondes pendant la séparation. Le PSI valait 1,24 % pour 5,40 Gio disponibles et 54,5 °C côté CPU. Le réglage de cinq secondes n’est ainsi pas qualifié au seuil de production.

La reprise manuelle avec réserve de 4 Gio termine les fenêtres de cinq secondes en 276,23 s. Le pic PSS de groupe passe de 1 781 Mio pour les fenêtres sérielles de sept secondes à 1 606 Mio. Le PSI monte à 1,72 %. Les sorties ne sont plus identiques. Sur le PCM mono à 16 kHz, les deux stems vocaux ont le même nombre d’échantillons et une corrélation de 0,9955. L’énergie du résidu est à −20,39 dB face au stem de sept secondes après ajustement du gain. Cette mesure compare les sorties des séparateurs et laisse ouverte l’exactitude par rapport à une voix propre de référence.

### Turbo Q5 sur une chanson entière

Turbo Q5_0 a terminé les trois variantes sur le titre espagnol de 193,68 s, avec la séparation validée réutilisée et le seuil de PSI de 1 %. Le poids officiel occupe environ 548 Mio et porte le SHA-256 `394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2`. Les écarts entre variantes comparent la préparation du signal avec le même poids sur le même titre. La tentative F16 avait été interrompue et les conditions de l’hôte avaient changé. [Poids Turbo Q5_0 officiel](https://huggingface.co/ggerganov/whisper.cpp/blob/main/ggml-large-v3-turbo-q5_0.bin)

| Entrée Q5_0 | WER sur 458 mots | CER | Substitutions / suppressions / insertions | Passages incertains | Temps de décodage |
| --- | ---: | ---: | ---: | ---: | ---: |
| Mix | 8,08 % | 5,54 % | 10 / 17 / 10 | 21 | 86,33 s |
| Voix isolée | 6,77 % | 4,43 % | 9 / 13 / 9 | 20 | 77,51 s |
| Voix isolée et découpage adaptatif | 12,23 % | 10,11 % | 12 / 36 / 8 | 32 | 194,75 s |

Aucune variante n’a produit de mots dans les intervalles de plus de deux secondes entre annotations. Le PSS maximal du groupe a atteint 247 Mio pour un RSS de 316 Mio. Le minimum disponible a été de 4,60 Gio, pour un PSI maximal de 0,18 %. Les températures maximales observées ont été de 64,25 °C côté CPU et 51 °C côté GPU. Le GTT maximal de l’hôte a atteint 6,01 Gio. Les compteurs DRM ont attribué jusqu’à 1 016 Mio de GTT résident au client du décodeur. Le parcours complet a pris environ 362 s, avec 4 256 pages écrites en swap sur l’hôte pendant la mesure.

| Repère indépendant | Mix couvert / p95 / total à ±500 ms | Voix isolée couverte / p95 / total à ±500 ms | Découpage couvert / p95 / total à ±500 ms |
| --- | ---: | ---: | ---: |
| Début de mot | 93,89 % / 419 ms / 91,70 % | 95,20 % / 440 ms / 92,79 % | 89,52 % / 404 ms / 87,99 % |
| Fin de mot | 91,05 % / 463 ms / 87,55 % | 92,79 % / 503 ms / 87,99 % | 88,86 % / 468 ms / 85,15 % |
| Début de ligne | 96,08 % / 359 ms / 96,08 % | 94,12 % / 359 ms / 92,16 % | 90,20 % / 340 ms / 90,20 % |
| Fin de ligne | 74,51 % / 877 ms / 68,63 % | 84,31 % / 741 ms / 72,55 % | 86,27 % / 517 ms / 80,39 % |

Le p95 porte sur les repères retrouvés. La dernière valeur de chaque cellule conserve toutes les annotations au dénominateur. Le découpage a perdu 26 débuts de mots face au stem continu malgré un p95 calculé sur les repères restants plus bas. Le pire écart de début de mot sur le stem continu atteint 1 761 ms. Ces mesures font du stem continu un candidat à comparer sur d’autres genres et laissent l’objectif de 500 ms ouvert.

Parmi les 56 débuts annotés à moins de 500 ms des 23 frontières adaptatives, 6 sont absents dans le découpage et 3 dans le stem continu. Sur les 402 autres, il en manque 42 et 19 respectivement. L’essentiel de l’écart vient donc de l’intérieur des fenêtres sur ce morceau. Du côté des fins de mots dans le stem continu, les 384 bornes fournies par le jeton suivant ont un p95 de 477 ms. Les 41 bornes estimées par la fin du segment ont un p95 de 846 ms. Une seule règle de correction pour les deux origines est mal étayée par cet essai.

Le rap espagnol annoté comporte 16 lignes exactement répétées et 13 lignes dans le quartile de débit vocal le plus haut, à partir de 3,48 mots par seconde. En affectant chaque mot à une seule ligne selon son début, le stem continu et le découpage donnent respectivement 3,90 % et 9,74 % de WER sur les lignes répétées. Sur les lignes rapides, ces taux passent à 5,65 % et 1,61 %. Sur les 38 autres lignes classées par débit, ils passent à 8,38 % et 16,17 %. Ces taux dépendent de l’affectation temporelle aux lignes et des répétitions du même enregistrement. La sélection adaptative généralise mal sur ce morceau entier, malgré son résultat sur les lignes rapides.

#### Décalage et dérive sur blocs tenus à part

Pour chacun des six blocs de 30 secondes contenant du chant annoté, un décalage médian et une dérive ont été estimés à partir des débuts de mots situés dans les autres blocs. Une marge de deux secondes sépare l’estimation du bloc évalué. Les appariements d’origine restent fixes, de même que le dénominateur. Le tableau donne le p95 des écarts sur le stem continu. Le banc contrôle que les mesures brutes sont identiques à celles du reçu initial.

| Repère | Brut | Décalage seul | Décalage et dérive |
| --- | ---: | ---: | ---: |
| Début de mot | 440 ms | 328 ms | 325 ms |
| Fin de mot | 503 ms | 409 ms | 393 ms |
| Début de ligne | 359 ms | 253 ms | 248 ms |
| Fin de ligne | 741 ms | 732 ms | 731 ms |

Le décalage estimé sur le stem varie de −123 à −106 ms suivant le bloc. La part de tous les débuts de mots à ±500 ms passe de 92,79 % à 94,32 % avec chacune des corrections. Sur le mix, le décalage fait passer le p95 de fin de ligne de 877 à 981 ms. Sur le découpage adaptatif, il fait passer la médiane de fin de ligne de 60 à 154 ms. Cette estimation utilise des annotations du même titre. Il reste à quantifier la transférabilité entre chansons et à estimer un éventuel décalage uniquement à partir du signal ou de mesures indépendantes en production.

```sh
python3 -B tests/qualify-parole-clock-drift.py /dossier/prive/es-recuerdo/case.json vocals
```

### Extension à la pop anglaise et au country français

Les fichiers de référence proviennent de JamendoLyrics MultiLang. Les lignes dérivent des annotations manuelles des mots. Les débuts de sous-tokens créés par la normalisation ne reçoivent pas de repère manuel inventé. Sur le titre pop, 334 débuts sont évaluables pour 376 mots de référence. Sur le titre country, 315 le sont pour 369 mots. [Annotations JamendoLyrics MultiLang](https://huggingface.co/datasets/jamendolyrics/jamendolyrics)

| Titre et moteur | Entrée | WER | CER | Temps du décodeur | Tous les débuts de mot à ±500 ms | Tous les débuts de ligne à ±500 ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Cortez, *Feel (Stripped)* · 240,56 s · F16 | Mix | 15,96 % | 13,64 % | 105,69 s | 76,95 % | 85,71 % |
| Même titre · F16 | Voix | 19,68 % | 16,70 % | 105,95 s | 73,95 % | 85,71 % |
| Même titre · F16 | Découpage | 15,16 % | 12,17 % | 271,33 s | 80,24 % | 85,71 % |
| Même titre · Q5 | Mix | 16,49 % | 13,77 % | 120,20 s | 77,25 % | 88,10 % |
| Même titre · Q5 | Voix | 18,88 % | 15,93 % | 120,06 s | 74,85 % | 83,33 % |
| FLO, *l’abandon* · 209,70 s · Q5 | Mix | 74,53 % | 72,78 % | 98,57 s | 26,35 % | 30,23 % |
| Même titre · Q5 | Voix | 57,45 % | 53,23 % | 110,89 s | 44,44 % | 46,51 % |

Sur la pop, l’alignement F16 du découpage affiche encore un pire début de mot à 1 875 ms. Le p95 de fin de ligne est de 985 ms sur les repères appariés. Sur le country, la voix isolée réduit les suppressions de 262 à 183. Le p95 des débuts de mots appariés vaut 363 ms avec seulement 140 débuts retrouvés sur 315. Un seuil de publication doit tenir compte de cette couverture. Tous ces passages ont émis zéro mot dans les grands intervalles entre annotations.

Les trois décodages Q5 sur chansons entières ont fini au seuil de PSI de 1 % avec leurs séparations déjà disponibles. Sur les titres anglais et français, les pics enregistrés sont de 0,06 % et 0,36 %. Les réserves minimales mesurées sont de 5,33 Gio et 5,20 Gio. La tentative F16 espagnole a été arrêtée à environ 4 Gio disponibles pendant la lecture de la voix. La tentative F16 française a été interrompue par une variation de capot ou d’alimentation. Ces essais laissent ouverte la qualification complète de Demucs au seuil de production. Le titre pop n’indique pas un gain de vitesse lié à la quantification.

### Paroles superposées et exécution réelle du brouillon

La pop française *Pas que t’es pas* d’AZUL possède le marqueur `LyricOverlap` dans JamendoLyrics. Les deux passes Q5 terminent cette chanson de 202,41 s sous le seuil de 1 %, avec un PSI maximal de 0,44 %. Sur 296 mots de référence, le mix produit 43 erreurs, soit 14,53 %, et la voix isolée 35, soit 11,82 %. Le CER passe de 7,51 % à 5,74 %. Le décodeur prend environ 78 s par entrée. Parmi les 240 débuts de mot annotés, 86,67 % sont à ±500 ms sur le mix et 88,33 % sur la voix. Le p95 de fin de ligne passe de 1 208 à 1 084 ms. Chaque entrée produit un mot dans un grand intervalle entre annotations. Ces mesures couvrent le morceau entier, avec une seule annotation chronologique et sans référence séparée par voix. [JamendoLyrics](https://huggingface.co/datasets/jamendolyrics/jamendolyrics)

Le folk espagnol *Esencia* de NandoMalo porte le marqueur `Polyphonic`. Demucs a été arrêté après 89 s de séparation à 3,99 Gio disponibles au seuil manuel de 4 Gio, avec un PSI de 0 %. Le stem n’a donc pas été évalué. Les entrées d’AZUL et de NandoMalo ont été utilisées pour le calcul local sous CC BY-NC-SA et CC BY-SA respectivement. Les pistes, annotations et stems restent dans l’espace privé d’essai. [CC BY-NC-SA](https://creativecommons.org/licenses/by-nc-sa/4.0/) · [CC BY-SA](https://creativecommons.org/licenses/by-sa/4.0/)

Le chemin exact du brouillon de production F16 a également été exécuté sur une copie du titre country français. Il a atteint la transcription du mix et a été arrêté à 1,09 % de PSI, avec 4,21 Gio disponibles, après environ 10 s. Le relevé ne contient aucune phase Demucs et aucun brouillon n’a été publié pendant cette tentative.

### Alternance de langue, témoin construit

Un montage de trois segments de neuf secondes issus des titres espagnol, anglais et français a fourni 39 mots entièrement inclus dans les intervalles annotés. La lecture Q5 en une seule fenêtre a estimé le français et commis neuf erreurs, soit 23,08 % de WER en 8,87 s. Avec les frontières du montage connues et la langue estimée séparément sur chaque segment, les trois langues sont correctes et le WER passe à 20,51 % en 24,51 s. Fixer les trois langues de référence donne les mêmes huit erreurs en 12,69 s. Le montage mesure la sensibilité de la détection à une frontière connue entre enregistrements. Il reste à qualifier les changements de langue au sein d’un même chant.

### Détection d’activité sur un titre entier

Un second banc a comparé les niveaux RMS, le rapport de niveau entre le stem et le mix, et Silero VAD sur le même titre complet de 193,68 s. Chaque décision s’applique à un intervalle de 100 ms. Le centre appartient soit à un mot annoté, soit à un intervalle éloigné d’au moins 500 ms des annotations. Les 1 401 intervalles de la première catégorie et les 201 de la seconde déterminent les fractions du tableau. La seconde catégorie peut contenir des sons vocaux non annotés.

| Détecteur | Intervalles annotés conservés | Intervalles éloignés rejetés |
| --- | ---: | ---: |
| RMS du stem au-dessus de −43 dBFS | 99,64 % | 76,12 % |
| RMS du stem au-dessus de −36 dBFS | 99,07 % | 80,10 % |
| Rapport stem et mix au-dessus de −10 dB | 91,93 % | 58,71 % |
| Silero à 0,50, mix | 54,03 % | 97,51 % |
| Silero à 0,50, stem | 97,50 % | 92,54 % |

Silero CPU à un fil prend environ une seconde par entrée. À 0,50, il retirerait près de la moitié des intervalles annotés du mix et environ 2,5 % de ceux du stem. Le RMS reste un candidat pour choisir des frontières, avec chevauchement et conservation du signal complet. Une règle qui supprime des passages doit d’abord être mesurée sur davantage de genres, de chœurs et de voix faibles.

```sh
python3 -B tests/qualify-parole-activity.py mix.mp3 vocals.flac manual.csv
```

## Moteurs et pistes de recherche

| Piste | Compatibilité et portée |
| --- | --- |
| HTDemucs et Turbo | HTDemucs 4.0.1 utilise ici PyTorch CPU. Le whisper.cpp installé est `4979e04f`, avec Vulkan sur la Radeon 780M. Le worker de préparation utilise DTW, glouton `best_of=1`, température sans incrément, seuil de silence à 0,65, contexte désactivé et faisceau 3 pour les reprises demandées. L’API de whisper.cpp expose les paramètres VAD et de contexte. Leur disponibilité ne vaut pas qualification de leur usage sur le chant. [whisper.cpp](https://github.com/ggml-org/whisper.cpp) |
| Séparation comme détecteur d’activité | Les expériences publiées de Syed et al. justifient un témoin à segmentation guidée par les voix sur les morceaux longs. Elles motivent le comparatif local avec lecture de l’intégralité des cœurs. Elles ne prédisent pas un gain sur chaque genre ou sur chaque extrait. [Étude de séparation et transcription](https://arxiv.org/abs/2506.15514) |
| Silero VAD | Code et poids sous licence MIT. Un petit modèle est disponible localement. C’est un détecteur de parole. La sensibilité aux voyelles tenues, aux voix faibles et aux chœurs doit être mesurée avant de retirer du signal. [Dépôt Silero](https://github.com/snakers4/silero-vad) |
| SOFA | Alignement forcé spécialisé pour le chant, code MIT et export ONNX. Le texte est requis, avec une conversion adaptée en phonèmes. Le dictionnaire de démonstration vise le mandarin et les performances sur le rap français restent à établir. [SOFA](https://github.com/qiuqiao/SOFA) |
| WhisperX | Code BSD-2-Clause et étape d’alignement distincte par Wav2Vec2. Les modèles documentés distinguent anglais, français et espagnol. Cette étape fournirait un témoin d’alignement indépendant du DTW de Turbo. Elle nécessite de qualifier les poids et le coût de traitement local sur le chant. [WhisperX](https://github.com/m-bain/whisperX) · [Modèles d’alignement](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py) |
| MMS et adaptation LoRA | MMS fournit un témoin multilingue CTC dont le modèle de grande taille est sous CC BY-NC. Une étude sur l’adaptation LoRA à la musique polyphonique indique une amélioration expérimentale. Les poids appropriés, leur licence, la mémoire et le débit sur ArchASP demandent des vérifications avant intégration. [MMS](https://huggingface.co/facebook/mms-1b-all) · [Étude LoRA](https://arxiv.org/abs/2506.02339) |

## Reproduction du comparatif

```sh
"$HOME/.local/share/hyprwhspr/venv/bin/python" -B tests/qualify-parole-separation.py \
  .runtime/omega-comparatif --previous /dossier/prive/essai-50 \
  --index /dossier/prive/instantane.sqlite3 --library /home/Partage/Music
```

Pour reprendre, conserver le même dossier et les mêmes entrées. Chaque variante terminée possède un reçu atomique avec ses observations privées. Les dépendances de la relecture sont reprises depuis ces reçus. Un résultat interrompu garde sa propre trace des ressources. `--phase demucs` isole la séparation. `--stems-from` relit un lot antérieur après contrôle du manifeste et de chaque piste prête. `--annotations` remplace les arguments du corpus privé par un dossier JamendoLyrics contenant le manifeste des chansons entières. `--case` permet un pilote sur une sélection. `--summary` restitue les métriques globales et par genre des cas terminés sans lancer les modèles. Les essais à seuil manuel sont à déclarer explicitement. Les résultats restent expérimentaux et privés.
