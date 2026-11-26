# Qualification du contexte linguistique local de Turbo

La version de `whisper.cpp` installée pour ces essais est `4979e04f`. Elle expose un prompt initial et une liste de jetons de prompt. Le premier essai a utilisé au plus 512 octets UTF-8 et 64 jetons par fenêtre. Le banc ciblé porte ces deux bornes à 768 octets et 128 jetons pour mesurer des contextes de 50 mots. Une trame sans contexte garde son encodage. Le décodeur conserve ses réglages, ses poids et ses horodatages acoustiques de production. Seuls les bancs demandent des jetons de contexte.

Le [banc privé](../tests/qualify-parole-context.py) lit un instantané SQLite de Parole. Il exporte la provenance et les fréquences des formes et expressions de deux mots, par enregistrement. Il conserve les graphies, accents et contractions, et associe les observations à la langue détectée, au genre disponible, à l’artiste et à l’édition. Le corpus de l’essai prend les textes intégrés à la collection Strawberry ou aux tags, et les LRC dont la structure correspond à la durée du morceau. Cette qualification structurelle ne vérifie pas chaque mot. Les LRC classés comme générés et les provenances identifiées comme transcriptions sont écartés. Les fichiers texte sans attestation de correction ne sont pas utilisés dans cet essai.

Les titres d’évaluation sont retirés avant la sélection du lexique. Les identités de piste, les couples titre/artiste, les identifiants MusicBrainz, PCM et les textes normalisés identiques disponibles servent aussi à exclure des doubles. Des reprises sous des crédits différents peuvent subsister lorsque ces identifiants sont absents et que leur texte diffère. Les formes sont classées à l’intérieur du contexte demandé. Le banc conserve le corpus et les mesures dans un dossier privé.

```sh
"$HOME/.local/share/hyprwhspr/venv/bin/python" -B tests/qualify-parole-context.py \
  .runtime/parole-contexte/essai --index /chemin/vers/copie.sqlite3 \
  --library /chemin/vers/bibliotheque --case 'Artiste/Album/Titre.opus'
```

Ajouter une option `--case` par titre équipé d’un LRC de référence. Le banc prend les premières lignes de chaque LRC et conserve la même fenêtre PCM pour tous les réglages. Il essaie le mix, puis la voix séparée si les pistes Demucs existent déjà. Un essai de genre requiert le genre du titre dans l’index. Le texte du LRC sert uniquement au découpage et à l’évaluation.

L’inventaire Parole lit désormais le genre facultatif du catalogue Strawberry. Les pistes dont la collection fournit un genre sont mises à jour lors d’un inventaire. Les anciennes bases Strawberry sans cette colonne conservent leur comportement.

## Premier relevé sur ArchASP

Le 26 novembre 2026, 8 899 enregistrements ont fourni du vocabulaire. Trois extraits tenus à l’écart, en rap français, rap américain et reggae, ont fourni 109 mots de référence. Les neuf lectures ont utilisé Vulkan sur le GPU AMD, avec le mix original. Le genre et les pistes Demucs réutilisables n’étaient pas disponibles dans l’index et le cache de ces trois cas.

| Contexte | Erreurs / mots de référence | WER | Insertions | Temps Turbo |
| --- | ---: | ---: | ---: | ---: |
| Sans contexte | 33 / 109 | 30,3 % | 7 | 10,57 s |
| Général | 33 / 109 | 30,3 % | 18 | 10,16 s |
| Artiste | 53 / 109 | 48,6 % | 2 | 11,34 s |

Le reggae passe de 15 à 3 erreurs avec les deux contextes. Le couplet de rap français passe de 11 à 23 erreurs avec le contexte général. Avec le contexte artiste, le filtre acoustique ne retient aucun des 43 mots attendus de cet extrait. Ce lot ne qualifie aucun contexte pour les départs automatiques. Les paroles produites restent des observations d’essai.

Le relevé ci-dessus mesure des erreurs de mots sur des extraits. La précision temporelle de 500 ms demande une annotation indépendante des repères.

L’enregistrement d’une correction dans Parole conserve un reçu distinct pour le TXT et le LRC. Un nouveau relevé lexical accepte une correction seulement si son fichier et l’audio ont gardé les versions approuvées. L’éditeur refuse d’enregistrer sur un audio modifié depuis son ouverture. Les poids lexicaux des nouveaux relevés dédupliquent aussi les identités d’enregistrement connues, à partir du PCM, de l’identifiant MusicBrainz ou de l’ISRC. Les relevés historiques restent conservés et les brouillons en attente de validation n’alimentent pas le lexique.

## Banc ciblé par genre et par taille de contexte

Le [banc de 50 chansons](../tests/qualify-parole-targeted.py) décode le mix original sans contexte ni langue imposée. Il cherche le mot le moins sûr et découpe au plus cinq secondes autour de son début. Une nouvelle lecture en faisceau du même PCM intervient sans contexte. Le lexique est ensuite filtré par genre et langue détectée. Il classe en premier les graphies proches des mots incertains de la fenêtre. En français, cette proximité utilise des équivalences de graphèmes simples, comme `eau` et `o`. Un contexte ne reçoit ni le texte de référence ni celui d’un autre passage du morceau évalué.

Pour accepter un changement, la lecture en faisceau sans contexte et la lecture avec contexte doivent toutes deux retrouver la même autre graphie à 220 ms ou moins du début initial, avec une confiance supérieure ou égale à 0,65. Le changement conserve le début et la fin du mot initial. Cette règle interdit les mots ajoutés par le contexte. L’accord des lectures est une condition expérimentale pour proposer un remplacement. La qualité se mesure ensuite sur la référence tenue à l’écart.

Le banc lit les genres dans Strawberry. Sur l’instantané mesuré, les 7 431 pistes à LRC retenues par le filtre disponible portent le genre générique « Music ». Un fichier privé attribue donc des groupes de genre à certains artistes pour cet essai. La sélection n’écrit ni dans Strawberry ni dans les tags. Elle retire du corpus les enregistrements évalués et les copies détectées selon l’identité et le texte. Les références LRC délimitent les premiers extraits chantés, entre 5 et 19 s chacun, et fournissent les mots attendus.

```sh
"$HOME/.local/share/hyprwhspr/venv/bin/python" -B tests/qualify-parole-targeted.py \
  .runtime/parole-cible/essai --index /chemin/vers/copie.sqlite3 \
  --library /chemin/vers/bibliotheque --collection /chemin/vers/strawberry.db \
  --genres /chemin/vers/genres.json
```

Le répertoire parent de sortie doit exister. Le JSON de genre associe un nom de genre à une liste de noms d’artistes. Le banc enregistre les mesures après chaque chanson et l’option `--resume` reprend le même plan avec les mêmes entrées. Le test utilise un décodeur Vulkan à la fois. Il attend lorsque le capot est fermé, le secteur absent ou les seuils de mémoire et de température atteints. Le manifeste, le lexique et les mesures restent dans le répertoire privé.

### Résultats de la comparaison ciblée

L’essai du 26 novembre rassemble 20 extraits de rap français, 16 de rap américain, 6 de rock, 4 de reggae, 2 de chanson française et 2 de soul. Il totalise 641 s d’audio et 1 317 mots de référence. Le corpus d’apprentissage issu de l’instantané contient 8 821 enregistrements après les exclusions. Turbo a décodé les 50 extraits sans contexte en 636,19 s, hors chargement du modèle, avec 222 substitutions, 299 omissions et 99 insertions.

| Variante | Fenêtres réellement décodées avec contexte | Corrections admises | Erreurs après décision (50 extraits) | Temps cumulé des fenêtres avec contexte |
| --- | ---: | ---: | ---: | ---: |
| Sans contexte | 0 | 0 | 620 (47,1 %) | — |
| 5 mots | 8 | 0 | 620 (47,1 %) | 47,50 s |
| 10 mots | 8 | 0 | 620 (47,1 %) | 46,38 s |
| 20 mots | 8 | 0 | 620 (47,1 %) | 51,68 s |
| 50 mots | 8 | 0 | 620 (47,1 %) | 65,46 s |

Quarante extraits ont été écartés avant l’envoi d’un contexte faute de seconde lecture sans contexte qui confirme une autre graphie. Deux autres n’ont reçu aucune relecture contextualisée. Les huit autres ont reçu les quatre variantes, dont les cas de contrôle du veto. Le total d’erreurs conserve le premier décodage partout où le relecteur s’abstient. Les durées couvrent les fenêtres contextualisées et leurs éventuelles attentes de ressources. Le temps du faisceau sans contexte n’est pas inclus.

La relecture ciblée a préservé le texte de la première passe, avec un coût supplémentaire et aucun gain mesuré. Elle reste réservée au banc. Cette comparaison utilise le mix original et un extrait par morceau. Elle ne mesure ni l’apport de Demucs sur ces mêmes 50 titres, ni les écarts temporels des mots, ni un taux de LRC validés sur des chansons entières.

## File de transcriptions à vérifier

En mode nocturne, un échec de recherche sans correspondance peut créer une tâche de transcription lorsque le titre est dépourvu de paroles et de LRC. Les erreurs de fournisseur reportent la recherche. Le même propriétaire de maintenance applique les limites de température, de mémoire et de stockage et traite un seul morceau à la fois. La tâche persiste dans la file Parole et reprend après interruption.

Turbo décode le mix sans contexte lexical et conserve ses observations dans le cache privé, liées à l’audio, au modèle et aux paramètres. La séparation Demucs appartient au parcours de préparation Kara qui utilise les deux pistes. Le brouillon propose des lignes de texte et de LRC lorsque les mots de la fenêtre les soutiennent. Les lignes écartées ne sont pas complétées. L’état du brouillon reste à vérifier. La préparation Kara peut réutiliser les observations lorsque la nouvelle référence a le même audio, le même décodeur et le même mode linguistique.

Dans les détails du morceau, **Charger le brouillon Turbo** remplit l’éditeur pour le format manquant sélectionné. Écouter et corriger les paroles ou le minutage avant **Enregistrer**. L’enregistrement conserve la validation manuelle et protège une référence ajoutée entre-temps. Le brouillon ne change aucun fichier musical ni aucun LRC de la bibliothèque.

Le volet **Passages à vérifier** affiche jusqu’à vingt segments écartés avec leur intervalle et la proposition de Turbo. Les boutons repositionnent dans Strawberry le morceau déjà sélectionné. Le parcours de vérification respecte les préparations et les tours Kara en cours. La correction se fait toujours dans l’éditeur du format choisi.

### Essai du brouillon Vulkan sur un extrait réel

Parole a préparé un brouillon sur une copie WAV de 21,59 s dépourvue de tags de paroles et de fichiers associés. Le moteur Turbo seul a retenu deux lignes, écarté trois segments textuels incertains et conservé 62 observations de mots dans le cache privé en 21,82 s. Les deux lignes ont exactement les mêmes mots normalisés que le LRC de référence. Leurs débuts s’en écartent de 320 et 460 ms. Kara a relu la première passe sous les mêmes signatures audio, modèle et paramètres. Le fichier WAV a conservé la même empreinte et la bibliothèque d’essai est restée sans TXT ni LRC publiés.

Les deux essais de l’ancien parcours se sont arrêtés pendant la séparation Demucs, après environ 29 et 35 s. L’intervention du gouverneur mémoire a précédé le lancement de Turbo. Le journal historique ne contient ni les valeurs de PSI ou de mémoire disponible ni l’échantillon de charge concurrente à l’arrêt. Les valeurs de 320 et 460 ms comparent deux lignes au LRC existant. Une mesure générale du seuil de 500 ms exige des annotations indépendantes et des chansons entières.
