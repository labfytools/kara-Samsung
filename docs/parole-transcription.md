# Qualification du contexte linguistique local de Turbo

La version de `whisper.cpp` installée pour cet essai est `4979e04f`. Elle expose un prompt initial et une liste de jetons de prompt. Le décodeur Turbo de l’essai accepte un contexte par fenêtre, avec 512 octets UTF-8 et 64 jetons au plus. Une trame sans contexte garde son encodage. Le décodeur conserve ses réglages, ses poids et ses horodatages acoustiques de production. Seul le banc demande des jetons de contexte.

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

## File de transcriptions à vérifier

En mode nocturne, un échec de recherche sans correspondance peut créer une tâche de transcription lorsque le titre est dépourvu de paroles et de LRC. Les erreurs de fournisseur reportent la recherche. Le même propriétaire de maintenance applique les limites de température, de mémoire et de stockage et traite un seul morceau à la fois. La tâche persiste dans la file Parole et reprend après interruption.

Demucs utilise le cache de Kara. Turbo décode une première fois le mix sans contexte lexical et conserve ses observations dans le cache privé, liées à l’audio, au modèle et aux paramètres. Il propose des lignes de texte et de LRC lorsque les mots de la fenêtre les soutiennent. Les lignes écartées ne sont pas complétées. L’état du brouillon reste à vérifier. La préparation Kara peut réutiliser les observations lorsque la nouvelle référence a le même audio, le même décodeur et le même mode linguistique.

Dans les détails du morceau, **Charger le brouillon Turbo** remplit l’éditeur pour le format manquant sélectionné. Écouter et corriger les paroles ou le minutage avant **Enregistrer**. L’enregistrement conserve la validation manuelle et protège une référence ajoutée entre-temps. Le brouillon ne change aucun fichier musical ni aucun LRC de la bibliothèque.
