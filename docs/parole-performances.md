# Banc de préparation Parole V3

Le [corpus](../tests/fixtures/preparation-cases.json) fixe dix morceaux entiers de la bibliothèque locale. Il couvre le rap rapide et lent, les voix mélodiques, les refrains répétés, les anciens enregistrements et les éditions récentes, en français, anglais et espagnol. Quatre cas repassent sans minutage à partir du texte Strawberry. Pour ces quatre cas, le rapprochement par les mêmes règles de normalisation que Kara a confirmé l’identité des mots et de leur découpage en lignes entre le texte et le LRC source.

## Rejouer la référence

```sh
umask 077
tlpctl launch --profile power-saver --reason "Banc de préparation Kara" \
  --appid fr.labfy.Kara.Benchmark -- python3 -B tests/benchmark-kara-preparation.py \
  .runtime/preparation-perf/baseline \
  --index /chemin/vers/maintenance.sqlite3 --profile power-saver
```

Le banc utilise des copies privées, lit l’index en lecture seule et garde la production en service. Il emploie la configuration nocturne de référence avec HTDemucs sur CPU à deux fils et large-v3-turbo Vulkan à quatre fils. Les observations se font capot ouvert sur secteur, avec les limites CPU/GPU de 78/75 °C, la réserve de 20 Gio et les garde-fous mémoire. Il surveille aussi les moteurs Kara et les flux multimédia. Le profil temporaire du banc est détenu par `tlpctl launch` et a été rendu lors des sorties observées. L’usage de ce client pour un essai supervisé ne change pas la politique de Parole sur TLP.

Le réglage par défaut interrompt l’essai à 1 % de pression mémoire PSI. Pour un comparatif manuel, un dossier d’essai distinct peut utiliser `--memory-psi 3` avec au moins 4 Gio de mémoire disponible pendant le traitement. Le banc conserve les mêmes seuils thermiques, la priorité des moteurs Kara et l’échantillonnage de la charge globale, y compris quand des téléchargements ont lieu. Cette mesure manuelle a ses propres conditions d’admission. Parole garde son seuil nocturne de 1 %.

Les résultats privés distinguent décodage PCM, identité PCM, inférence Demucs, encodage FLAC, chargement Turbo, transcription, alignement, reprises, validation et écritures. Les durées instrumentées excluent les phases imbriquées. Les compteurs hôte sont échantillonnés environ chaque seconde. Le taux d’occupation CPU vient de la différence de compteurs actifs entre les échantillons, le GPU de `gpu_busy_percent`, la pression mémoire de PSI, et le stockage des secteurs et du temps d’activité par périphérique. Le signal PPT du pilote AMD ne représente pas la consommation électrique totale de la machine. `ru_maxrss` distingue le maximum du processus Python et celui de ses enfants. Les tailles allouées viennent de `st_blocks`. Les exécutions terminées sont reprises sous la même configuration et les interruptions gardent un reçu séparé.

Pour comparer une modification Turbo dans un nouveau dossier, utiliser `--phase timed plain --stems-from /dossier/de/reference`. Le banc vérifie l’identité des fichiers audio et utilise les mêmes pistes validées en lecture seule. Pour comparer les profils CPU Demucs existants, utiliser `--phase demucs` et `--demucs-threads 2` ou `6` dans des dossiers distincts.

## Qualification temporelle indépendante

Le témoin privé retient trois enregistrements de JamendoLyrics MultiLang, révision `de188c9`. Il comprend *l’abandon* de Flo (français, CC BY), *Feel (Stripped)* de Cortez (anglais, BY) et *Te Recuerdo* de Wilson Way (espagnol, CC BY-SA). L’inférence reçoit les lignes de texte seules. Le banc ouvre ensuite les fichiers d’annotation manuelle pour la comparaison. Les débuts de ligne fournis par le jeu dérivent du premier mot de chaque ligne. Un mot scindé en plusieurs unités par Kara est exclu du calcul temporel par mot, car l’annotation ne donne pas de repère distinct à chaque unité. Les enregistrements et les paroles restent hors dépôt. [JamendoLyrics MultiLang](https://huggingface.co/datasets/jamendolyrics/jamendolyrics/tree/de188c9) · [Article de Durand, Stoller et Ewert](https://arxiv.org/abs/2306.07744)

Pour rejouer, fournir un corpus JSON avec `id`, `song` (nom relatif d’un MP3 ou WAV), `language` et `annotated: true`. Placer les lignes sans minutage dans le fichier `.txt` de même nom que l’audio. Utiliser `--library` pour la racine privée de ce corpus, `--corpus` pour le JSON et `--phase annotated`. Donner ensuite au [comparateur](../tests/qualify-kara-reference-timing.py) le dossier d’annotations (`lines.csv`, `manual.csv` et `words.words.txt`) et le JSON des lignes observées dans `evidence/`. Il rapporte les repères acoustiques évaluables, les absences, la couverture, la médiane, le 95e percentile et la proportion dont l’écart absolu est au plus de 500 ms.

La qualification mesurée prend les huit premières lignes annotées de chaque titre. Chaque extrait WAV préserve le taux et les canaux du flux source. L’extraction ajoute jusqu’à deux secondes avant la première ligne et 450 ms après la dernière. Les temps de référence sont transformés sur la même horloge d’échantillons, avec trois extraits de 30,0 à 33,9 s. Les fichiers se trouvent sous `jamendo-excerpts/`. Le moteur F16 a travaillé sur le mix et les paroles sans minutage. Les temps d’inférence comprennent les reprises locales.

| Langue | Repères | Mesurés / annotés | Médiane | P95 | À 500 ms ou moins |
| --- | --- | ---: | ---: | ---: | ---: |
| Anglais | Mots | 42/44 | 289 ms | 537 ms | 88,1 % |
| Anglais | Débuts de ligne | 7/8 | 280 ms | 558 ms | 85,7 % |
| Espagnol | Mots | 72/75 | 130 ms | 419 ms | 97,2 % |
| Espagnol | Débuts de ligne | 8/8 | 84 ms | 342 ms | 100 % |
| Français | Mots | 52/59 | 131 ms | 430 ms | 100 % |
| Français | Débuts de ligne | 5/8 | 39 ms | 130 ms | 100 % |
| **Ensemble** | **Mots** | **166/178** | **178 ms** | **473 ms** | **95,8 %** |
| **Ensemble** | **Débuts de ligne** | **20/24** | **114 ms** | **342 ms** | **95,0 %** |

Vingt unités lexicales sont non évaluables par mot, car issues de la scission d’un mot court annoté en un seul temps. Douze mots et quatre débuts annotés restent sans repère acoustique. Le seuil de 500 ms est dépassé sur sept mots et un début de ligne parmi les observations comparables. Le seuil ne constitue donc pas une garantie individuelle sur ce témoin.

La quantification Q5_0 a été comparée sur les trois mêmes extraits, avec la même configuration Vulkan et sans le contexte court. L’empreinte SHA-256 du fichier téléchargé a été vérifiée. Le temps cumulé est passé de 146,0 à 97,3 s (−33,3 %). La couverture est restée à 166/178 mots et 20/24 débuts de ligne. Le P95 global des mots est passé de 473 à 454 ms et le P95 global des lignes est resté à 342 ms. Sur l’extrait anglais, le P95 des débuts de ligne est monté de 558 à 798 ms. F16 reste la référence de production. Les mesures par langue sont conservées sous `timing-f16-scores.json` et `timing-q5-scores.json`. [Poids Q5_0 distribués par whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp/blob/main/ggml-large-v3-turbo-q5_0.bin)

## Relevé ArchASP du 25 novembre 2026

| Mesure | Résultat |
| --- | --- |
| Référence historique, deux morceaux complets | 4,43 chansons/h et 60,4 Mio par chanson. [Détail](parole-v3.md#essais-nocturnes-sur-archasp) |
| Référence élargie | Les dix audios et LRC sont accessibles. Au seuil de 1 %, les essais Demucs ont été interrompus avant publication. Les trois premiers essais instrumentés ont atteint 1,16 à 1,29 % de pression mémoire. Après libération de mémoire et stabilisation du profil, un autre essai s’est arrêté à 1,52 %, avec 5,9 Gio encore disponibles. |
| Témoin indépendant | La première analyse intégrale à 1 % de PSI a été arrêtée après 4,3 s. La qualification F16 a ensuite terminé sur trois extraits annotés. |
| Capot fermé | La réouverture a interrompu une première mesure après 4,3 s. La seconde séquence de charge s’est arrêtée après 29,0 s à 3,05 % de PSI, avec des maxima CPU/GPU de 64,9/58 °C. |

Les journaux privés de cette campagne se trouvent sous `.runtime/preparation-perf/`. Les tentatives interrompues sont exclues du débit et des moyennes de traitement.

La qualification physique utilise `--closed-lid` dans un dossier séparé. Démarrer sur secteur et capot ouvert. Le banc prend un inhibiteur logind temporaire pour l’action du capot, annonce sa disponibilité et attend la fermeture physique. Il garde le plafond de 70 °C sur chacun des deux capteurs. La réouverture et une demande de mise en veille interrompent les moteurs. À la sortie des deux essais, l’inhibiteur a disparu et le profil Performance initial a été rétabli. La qualification prolongée reste en attente.

### Premier parcours complet du comparatif manuel

Le code de référence provient de `5b6403d`, avant la correction de reprise des débuts de ligne. La mesure utilise le profil Économie sur secteur, capot ouvert, avec `--memory-psi 3`. *Good Guy* (2 min 22) a donné une préparation validée à partir de son LRC source.

| Mesure | Temps ou résultat observé |
| --- | ---: |
| Demucs complet | 167,6 s |
| Décodage de l’entrée Demucs | 0,51 s |
| Inférence Demucs | 159,6 s |
| Encodage des deux FLAC | 0,67 s |
| Parcours Turbo complet | 326,6 s |
| Chargement du modèle | 2,10 s |
| Décodage PCM des fenêtres Turbo | 0,80 s |
| Transcription initiale hors décodage PCM | 47,36 s |
| Alignement initial | 0,024 s |
| Reprises hors décodage PCM et validation | 273,83 s (40 décodages) |
| Validation de la référence | 0,005 s |
| Écriture des références | 0,016 s |
| Total des deux processus | 494,2 s |
| Cadence extrapolée de ce titre | 7,28 chansons/h |
| Cache alloué par ce titre | 25,09 Mio |
| Couverture mots / débuts de ligne | 83,91 % / 78,69 % |

Demucs a consommé 307 s de CPU cumulées et rapporté 1,56 Gio de RSS maximal pour ses enfants. Le parcours Turbo a consommé 104 s de CPU et rapporté 208 Mio de RSS maximal pour ses enfants. Pendant Turbo, le GPU a été occupé en moyenne à 94,4 % sur les échantillons hôte et la mémoire disponible n’est pas descendue sous 4,24 Gio. Les maxima s’établissent à 71,9 °C côté CPU et 66 °C côté GPU sur les deux étapes. La couverture décrit les repères produits. La précision sera mesurée sur le témoin annoté.

Sur ce même titre, le texte Strawberry seul a donné 75,97 % de mots et 75,41 % de débuts de ligne couverts, après 242,3 s et 34 décodages locaux. Quinze débuts restaient non établis, dont treize avec le premier mot absent. Huit de ces lignes n’avaient aucun mot repéré. Le parcours avec le LRC source a trouvé deux de ces quinze débuts. La génération a donc conservé son refus de publication.

### Référence complète sur dix titres

Les dix parcours ont utilisé HTDemucs CPU à deux fils et large-v3-turbo Vulkan F16 à quatre fils, capot ouvert, profil Économie et comparatif manuel à 3 % de PSI. Ils totalisent 4 762,9 s de calcul. Sept titres ont été validés, soit 5,29 validations par heure, en incluant dans le temps de calcul les trois titres refusés. Le cache moyen alloué, calculé sur les dix titres, est de 28,47 Mio. Le lot historique de deux morceaux est un échantillon différent.

| Cas du corpus | Demucs (s) | Turbo (s) | Reprises (s) | Cache (Mio) | Mots | Débuts de ligne | Validation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| fr-rap-dense | 187,6 | 322,2 | 253,3 | 32,76 | 89,0 % | 91,5 % | Oui |
| en-rap-alternance | 167,6 | 326,6 | 273,8 | 25,09 | 83,9 % | 78,7 % | Oui |
| fr-rap-lent | 170,7 | 480,7 | 403,9 | 23,45 | 51,2 % | 38,5 % | Refusée |
| fr-refrain | 182,0 | 325,4 | 261,9 | 25,16 | 91,4 % | 77,5 % | Oui |
| fr-chanson-ancienne | 155,4 | 162,4 | 83,9 | 21,65 | 95,2 % | 97,0 % | Oui |
| en-chant-studio | 184,2 | 288,9 | 234,5 | 24,30 | 70,8 % | 54,2 % | Refusée |
| en-rap-live | 184,8 | 494,3 | 405,5 | 30,29 | 65,1 % | 54,9 % | Refusée |
| es-chant-studio | 254,1 | 298,2 | 217,2 | 43,38 | 91,9 % | 86,8 % | Oui |
| es-rap | 223,9 | 76,1 | < 0,01 | 32,79 | 99,0 % | 100 % | Oui |
| es-chant-ancien | 214,6 | 63,1 | < 0,01 | 25,88 | 95,5 % | 90,3 % | Oui |
| **Moyenne** | **192,5** | **283,8** | **213,4** | **28,47** | **83,3 %** | **76,9 %** | **7/10** |

Le temps moyen total est de 476,3 s. Les reprises représentent 75,2 % du temps Turbo sur ces dix titres. La charge CPU de l’hôte était de 37 % pendant les deux moteurs. L’occupation GPU moyenne était de 26,7 % pendant Demucs, où elle inclut l’activité graphique de fond, et de 95,1 % pendant Turbo. Le CPU a atteint 75,6 °C pendant Demucs et 74,9 °C pendant Turbo. Le GPU a atteint 69 °C. La mémoire disponible est restée au-dessus de 5,21 Gio pendant Demucs et 4,24 Gio pendant Turbo. Le PPT moyen exposé par AMD était de 15,5 W et 16,7 W respectivement. Les compteurs d’E/S par périphérique et les données par titre sont conservés dans `baseline-manual/results/` et `baseline-summary.json`.

Les quatre variantes texte seul ont terminé en 224,3 à 329,2 s. Elles couvrent de 69,2 à 91,5 % des mots et de 54,2 à 91,5 % des débuts de ligne. Les quatre publications LRC ont été refusées.

### Contexte audio réduit pour les reprises Turbo

Le candidat isolé conserve le modèle F16, le DTW et la taille du faisceau. Sur les seules reprises en faisceau, il encode la fenêtre fournie avec deux secondes de marge, arrondies à un bloc de 64 positions d’encodeur. La première passe et les reprises gloutonnes conservent leur contexte. Les trois essais utilisent les pistes Demucs exactes du banc de référence.

| Cas | Turbo de référence → candidat | Reprises de référence → candidat | Mots | Débuts de ligne | Validation |
| --- | ---: | ---: | ---: | ---: | --- |
| fr-rap-dense | 322,2 → 270,8 s | 253,3 → 183,1 s | 89,0 → 88,8 % | 91,5 → 92,7 % | Oui → Oui |
| en-rap-alternance | 326,6 → 210,5 s | 273,8 → 147,3 s | 83,9 → 71,7 % | 78,7 → 65,6 % | Oui → Refusée |
| es-chant-studio | 298,2 → 148,6 s | 217,2 → 71,6 s | 91,9 → 88,3 % | 86,8 → 81,6 % | Oui → Oui |

Le candidat réduit de 33,5 % le temps Turbo cumulé sur ces trois titres. La perte de couverture sur *Good Guy* provoque le refus d’une préparation auparavant validée. Le candidat est écarté. Les traces privées sont conservées sous `ab-local-context/`.

### Stockage et coût d’ouverture

Le relevé du cache de production donne 160,82 Mio d’instrumentaux FLAC, 145,67 Mio de voix FLAC, 0,047 Mio de métadonnées Demucs et 0,645 Mio de références Turbo et LRC. Les tailles sont allouées et les liens physiques déjà dédupliqués par inode. Les pistes existantes sont en FLAC stéréo. Les deux fichiers représentent plus de 99 % de l’espace mesuré.

Sur cinq paires de pistes complètes déjà validées, le [banc de stockage](../tests/benchmark-kara-stem-storage.py) a réencodé des copies au niveau FLAC 8 avec FFmpeg. Les dix fichiers ont passé la vérification PCM intégrale, y compris le taux, le nombre d’échantillons par canal et l’origine à zéro des deux pistes. La prélecture est celle du pipeline Kara avec sortie silencieuse, répétée dix fois par variante et par paire. Elle exclut le périphérique de sortie, Strawberry et la latence du premier son physique.

| Mesure | FLAC actuel | FLAC niveau 8 |
| --- | ---: | ---: |
| Espace pour cinq paires | 204,88 Mio | 203,74 Mio |
| Moyenne par paire | 40,98 Mio | 40,75 Mio |
| Médiane de prélecture | 3,82 ms | 3,72 ms |
| 95e percentile de prélecture | 5,92 ms | 4,39 ms |
| Réencodage supplémentaire d’une paire | — | 1,38 s |

Le gain de 1,14 Mio sur cinq paires représente 0,55 %. Le réencodage supplémentaire est écarté. Les brouillons Demucs sont déjà temporaires et les réassociations PCM utilisent des liens physiques. Sur ce relevé, une compression des manifestes Turbo viserait moins d’un Mio au total.

Un essai gzip niveau 6 sur les sept manifestes existants donne 624 Kio alloués avant compression et 108 Kio après, soit 516 Kio d’économie. Le contenu JSON décodé est identique. La médiane de lecture et désérialisation en mémoire est passée de 0,49 à 0,60 ms par manifeste. Le format de production est conservé vu le poids des références dans le cache.

Le manifeste Turbo et ses repères sont déjà persistés séparément des pistes. Il est possible d’enrichir une référence à partir du mix, ou d’employer une piste vocale disponible pour les reprises. Quand les pistes manquent, une référence acceptée donne une préparation partielle. Au plafond actuel, la politique d’admission protège l’ensemble des préparations et ne crée pas de nouvelle tâche de référence seule. Un éventuel traitement au plafond devra mesurer séparément son espace de travail et ses références publiées. Il devra aussi limiter la taille d’une nouvelle publication au budget disponible.

### Compatibilité du moteur de séparation

L’environnement installé associe Demucs 4.0.1 à PyTorch et Torchaudio 2.6.0 `+cpu`. Il déclare `cuda=None` et `hip=None`. Le Vulkan de whisper.cpp équipe Turbo. Demucs utilise le modèle `htdemucs`, des segments de 7 s, un chevauchement de 10 %, zéro décalage aléatoire et une tâche de segment. Le traitement produit les deux stems FLAC. Demucs expose un parallélisme de segments sur CPU et plusieurs modèles. Leur coût mémoire, leur vitesse et la qualité nécessitent des comparaisons sur des morceaux terminés. [Implémentation Demucs](https://github.com/facebookresearch/demucs/blob/main/demucs/apply.py) · [Matrice AMD ROCm](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/reference/system-requirements.html)

### Demucs à six fils CPU

Les dix morceaux de référence ont été rejoués à six fils, avec les mêmes poids, segments, chevauchements et sorties FLAC. Le comparatif manuel a utilisé le profil Économie capot ouvert et le seuil de 3 % de PSI. Le dernier titre a exigé une reprise après un arrêt à 3,28 % de PSI.

| Mesure sur dix titres | Deux fils | Six fils |
| --- | ---: | ---: |
| Temps Demucs moyen | 192,5 s | 120,7 s |
| CPU cumulé moyen par titre | 358,8 s | 613,3 s |
| Maximum CPU / GPU | 75,6 / 69 °C | 71,4 / 65 °C |
| Maximum RSS rapporté pour un enfant | 2 142 Mio | 1 681 Mio |

Le temps mural baisse de 37,3 %. Les vingt pistes ont le même taux, le même nombre d’échantillons et la même origine temporelle que leurs témoins. Les différences PCM sont limitées à une unité de quantification 16 bits. Le plus grand rapport RMS entre l’écart et la piste correspondante est de −100,1 dB. Les profils produisent des arrondis différents.

Au seuil de 1 % appliqué par Parole la nuit, un nouveau passage à six fils sur `en-rap-alternance` s’est arrêté après 29,2 s à 1,23 % de PSI. Les deux fils restent le réglage nocturne. Les mesures sont conservées sous `demucs-six/`, `demucs-six-strict/` et `demucs-six-comparison.json`.

### Profil énergétique

Le profil Équilibré a été essayé sur le même titre de Joe Dassin, capot ouvert sur secteur, à deux fils, avec les mêmes limites thermiques. Demucs a pris 81,5 s, contre 155,4 s en Économie. Son maximum CPU est passé de 74,9 à 77,8 °C. Le PPT moyen exposé par AMD est passé de 15,9 à 20,4 W. Le parcours Turbo a franchi son seuil thermique après 4,25 s, avec 76 °C lus sur le GPU. Économie reste le profil du comparatif complet. Le profil initial Performance a été rétabli à la fin du test. Les mesures sont conservées sous `profile-balanced/`.

### Cas de génération depuis un texte

Une fixture passe par la publication LRC de Kara avec 24 mots observés sur 25 et quatre débuts de ligne sur cinq. Avant la correction, le seuil de couverture des mots déclenchait l’arrêt avant le premier essai local, alors que la publication exigeait encore le cinquième début. Après la correction, la même fixture publie les cinq lignes. Elle exécute une seule passe globale simulée, puis deux décodages simulés sur des fenêtres de moins de sept secondes. Le PCM encadrant ces fenêtres est décodé par FFmpeg. Aucun seuil acoustique ou de publication ne change. Le test mesure la décision et le travail demandé au décodeur. Le résultat sur les chansons complètes reste à qualifier.
