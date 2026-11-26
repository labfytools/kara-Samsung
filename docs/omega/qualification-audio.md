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

### Pilote lexical et audio

Les trois premiers extraits du manifeste, en rock et dans les deux groupes de rap, ont fourni 52 mots de référence. Le mix a rendu 25 erreurs, la voix brute et la voix normalisée 30 chacune et la segmentation adaptative 31. La relecture ciblée et la relecture phonétique ont laissé toutes les graphies du stem en place. L’écart entre le mix et le stem est concentré sur le rap américain du pilote. Ce résultat décrit ces trois cas et motive une qualification par genre.

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
| MMS et adaptation LoRA | MMS fournit un témoin multilingue CTC dont le modèle de grande taille est sous CC BY-NC. Une étude sur l’adaptation LoRA à la musique polyphonique indique une amélioration expérimentale. Les poids appropriés, leur licence, la mémoire et le débit sur ArchASP demandent des vérifications avant intégration. [MMS](https://huggingface.co/facebook/mms-1b-all) · [Étude LoRA](https://arxiv.org/abs/2506.02339) |

## Reproduction du comparatif

```sh
"$HOME/.local/share/hyprwhspr/venv/bin/python" -B tests/qualify-parole-separation.py \
  .runtime/omega-comparatif --previous /dossier/prive/essai-50 \
  --index /dossier/prive/instantane.sqlite3 --library /home/Partage/Music
```

Pour reprendre, conserver le même dossier et les mêmes entrées. Chaque variante terminée possède un reçu atomique avec ses observations privées. Les dépendances de la relecture sont reprises depuis ces reçus. Un résultat interrompu garde sa propre trace des ressources. `--phase demucs` isole la séparation. `--stems-from` relit un lot antérieur après contrôle du manifeste et de chaque piste prête. `--annotations` remplace les arguments du corpus privé par un dossier JamendoLyrics contenant le manifeste des chansons entières. `--case` permet un pilote sur une sélection. `--summary` restitue les métriques globales et par genre des cas terminés sans lancer les modèles. Les essais à seuil manuel sont à déclarer explicitement. Les résultats restent expérimentaux et privés.
