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

Les résultats privés distinguent décodage PCM, identité PCM, inférence Demucs, encodage FLAC, chargement Turbo, transcription, alignement, reprises, validation et écritures. Les durées instrumentées excluent les phases imbriquées. Les compteurs hôte sont échantillonnés environ chaque seconde. Le taux d’occupation CPU vient de la différence de compteurs actifs entre les échantillons, le GPU de `gpu_busy_percent`, la pression mémoire de PSI, et le stockage des secteurs et du temps d’activité par périphérique. Le signal PPT du pilote AMD ne représente pas la consommation électrique totale de la machine. `ru_maxrss` distingue le maximum du processus Python et celui de ses enfants. Les tailles allouées viennent de `st_blocks`. Les exécutions terminées sont reprises sous la même configuration et les interruptions gardent un reçu séparé.

## Qualification temporelle indépendante

Le témoin privé retient trois enregistrements de JamendoLyrics MultiLang, révision `de188c9`. Il comprend *l’abandon* de Flo (français, CC BY), *Feel (Stripped)* de Cortez (anglais, BY) et *Te Recuerdo* de Wilson Way (espagnol, CC BY-SA). L’inférence reçoit les lignes de texte seules. Le banc ouvre ensuite les fichiers d’annotation manuelle pour la comparaison. Les débuts de ligne fournis par le jeu dérivent du premier mot de chaque ligne. Un mot scindé en plusieurs unités par Kara est exclu du calcul temporel par mot, car l’annotation ne donne pas de repère distinct à chaque unité. Les enregistrements et les paroles restent hors dépôt. [JamendoLyrics MultiLang](https://huggingface.co/datasets/jamendolyrics/jamendolyrics/tree/de188c9) · [Article de Durand, Stoller et Ewert](https://arxiv.org/abs/2306.07744)

Pour rejouer, fournir un corpus JSON avec `id`, `song` (nom relatif d’un MP3), `language` et `annotated: true`. Placer les lignes sans minutage dans le fichier `.txt` de même nom que le MP3. Utiliser `--library` pour la racine privée de ce corpus, `--corpus` pour le JSON et `--phase annotated`. Donner ensuite au [comparateur](../tests/qualify-kara-reference-timing.py) le dossier d’annotations (`lines.csv`, `manual.csv` et `words.words.txt`) et le JSON des lignes observées dans `evidence/`. Il rapporte les repères acoustiques évaluables, les absences, la couverture, la médiane, le 95e percentile et la proportion dont l’écart absolu est au plus de 500 ms.

## Relevé ArchASP du 25 novembre 2026

| Mesure | Résultat |
| --- | --- |
| Référence historique, deux morceaux complets | 4,43 chansons/h et 60,4 Mio par chanson. [Détail](parole-v3.md#essais-nocturnes-sur-archasp) |
| Référence élargie | Les dix audios et LRC sont accessibles. Quatre essais Demucs, répartis sur deux titres, ont été arrêtés par la pression mémoire avant publication. Le dernier échantillon disponible avant arrêt indiquait 1,16 à 1,29 % de temps sous pression pour les trois essais instrumentés, pour un seuil de 1 %. |
| Témoin indépendant | La première analyse Vulkan a été arrêtée après 4,3 s de mesure, à 2,47 % de pression mémoire. Aucun écart temporel publiable sur ce témoin. |
| Capot fermé | Qualification prolongée sous charge en attente. Les mesures historiques portent sur une fermeture sans calcul lourd. |

Les journaux privés de cette campagne se trouvent sous `.runtime/preparation-perf/`. Les tentatives interrompues sont exclues du débit et des moyennes de traitement.

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

Le manifeste Turbo et ses repères sont déjà persistés séparément des pistes. Il est possible d’enrichir une référence à partir du mix, ou d’employer une piste vocale disponible pour les reprises. Quand les pistes manquent, une référence acceptée donne une préparation partielle. Au plafond actuel, la politique d’admission protège l’ensemble des préparations et ne crée pas de nouvelle tâche de référence seule. Un éventuel traitement au plafond devra mesurer séparément son espace de travail et ses références publiées. Il devra aussi limiter la taille d’une nouvelle publication au budget disponible.

### Compatibilité du moteur de séparation

L’environnement installé associe Demucs 4.0.1 à PyTorch et Torchaudio 2.6.0 `+cpu`. Il déclare `cuda=None` et `hip=None`. Le Vulkan de whisper.cpp équipe Turbo. Demucs utilise le modèle `htdemucs`, des segments de 7 s, un chevauchement de 10 %, zéro décalage aléatoire et une tâche de segment. Le traitement produit les deux stems FLAC. Demucs expose un parallélisme de segments sur CPU et plusieurs modèles. Leur coût mémoire, leur vitesse et la qualité nécessitent des comparaisons sur des morceaux terminés. [Implémentation Demucs](https://github.com/facebookresearch/demucs/blob/main/demucs/apply.py) · [Matrice AMD ROCm](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/reference/system-requirements.html)
