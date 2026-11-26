# Rapport OMEGA II

Mission du 26 novembre 2026 suivie de 14 h 40 min 53 s à 16 h 41 min 06 s CET, soit **2 h 00 min 13 s** jusqu’au relevé de clôture. La branche contient **21 commits locaux** produits pendant l’opération, avec ce rapport. Aucun push effectué.

## Résumé opérationnel

La branche de travail reste `feat/audio-latency-optimization`, départ `4c4cab9`. Les redémarrages chargent les améliorations de Parole sur `kara.service`. Les validations locales gardent le texte et le minutage distincts, et le tableau de bord sépare désormais les publications des simples opérations. La bibliothèque conserve ses **13 880 pistes**, ses **13 201 chemins historiques**, ses **30 lots d’import** et ses **79 associations manuelles**. [État de production, sauvegardes et retour arrière](exploitation.md)

La référence de transcription libre reste le mix Turbo F16 à **47,08 % de WER** sur 50 extraits. Avec un texte connu, le CTC brut place 487 débuts sur 487 à ±500 ms sur un hip-hop anglais tenu à part, 312 sur 334 sur le premier titre évalué et 8 sur 176 sur un rock tenu à part. Il peut aussi placer des mots absents. Les filtres écartent les faux mots introduits dans les contre-épreuves, au prix d’une couverture réduite. Open-Unmix montre un coût mémoire inférieur sur les premiers extraits appariés. Le WER et la qualification du seuil de 500 ms sur chansons entières restent distincts. [Comparatifs, limites et modèles](qualification-audio.md)

## Changements qualifiés

| Commit | Travail | Preuve |
| --- | --- | --- |
| `bc8ad77` | Extraire les LRC intégrés sur un tour local et traiter les preuves PCM avec ou sans créneau réseau. | Deux FLAC de fixture avec publication locale, contrôle des quotas et conservation des originaux. |
| `d2eafb7` | Attribuer correctement une saisie manuelle et un brouillon Turbo validé. | Reçus distincts de texte et de minutage, enrichissement lexical sur versions approuvées. |
| `a6655b9` | Ventiler les publications, brouillons, validations et reports sur 1 h, 24 h et la fermeture protégée. | Règlement idempotent de tâches, bornes de périodes et rendu GTK isolé. |
| `d016660` | Annuler ou défaire la saisie sans mélanger les formats. | Le brouillon LRC peut être chargé, modifié puis annulé sans publication dans la fixture GTK. |
| `f16ea4e` | Éviter une recherche bloquée lorsque le texte local permet une préparation. | Le secours local reprend sans appel de fournisseur et la recherche de minutage redevient éligible à sa réouverture. |
| `9f37101` | Étendre les témoins négatifs du score. | Score nul pour les mots incorrects ou récités deux secondes hors repères. Guide, rejeu de la voix originale et capture incomplète écartés avant reconnaissance sur fixtures. |
| `977228f` | Périmer la provenance d’une référence remplacée. | Une fixture rétablit une empreinte PCM identique après remplacement des tags. Le nouveau texte reste exclu de la propagation. |
| `a6e6950` | Qualifier la reprise d’import malgré l’ordre libre des téléchargements parallèles. | Les deux requêtes initiales sont vérifiées sans ordre imposé. La reprise doit encore produire exactement une requête supplémentaire. |
| `b40089c` | Répéter et ralentir les passages douteux. | FFmpeg fournit trois répétitions identiques et conserve la hauteur à la tolérance du témoin à 440 Hz. Les options de GTK ont été vérifiées. |
| `0c6772b` | Qualifier l’alignement CTC sur des chansons entières et des textes falsifiés. | Deux chansons anglaises tenues à part et des témoins d’omission et de substitution. |
| `998fa94` | Comparer les répétitions vocales et les hypothèses lexicales. | Quatre chansons entières et 50 extraits relus. Aucun mot republié. |
| `e7e52c2` | Éviter les prises en charge nocturnes sans référence disponible. | Le texte local passe, la tâche sans référence garde son échéance et reprend à la réouverture du fournisseur. |
| `c42fe93` | Comparer Open-Unmix à Demucs. | Pilote sur les premiers extraits figés par genre, mesures du PSS et du WER. |
| `3a7a8da` | Comparer les règles de sélection mix et voix. | Politique explicite et banc sur références figées. L’extrait tenu à part doit passer un contrôle d’empreinte audio et de LRC. |
| `783069b` | Mesurer l’identité et les décalages d’éditions sans modifier la bibliothèque. | 24 comparaisons PCM et Chromaprint, trois fenêtres temporelles par couple mesurable. |
| `639aafd` | Exiger l’accord de deux modèles acoustiques pour un repère CTC anglais. | Le témoin Turbo Q5 confirme 201 repères sur *Feel*, dont 199 à ±500 ms. |
| `1f93db2` | Comparer Qwen CPU et Radeon. | Charge effective sous `Vulkan0` et extension à onze extraits terminés. L’excès d’insertions reste inclus. |
| `0fdd7d5` | Éprouver les crédits supplémentaires d’une distance phonétique. | Deux faux positifs synthétiques à une édition, les sources rejetées et le hors-temps restent à zéro. |
| `43df7f1` | Aligner le français avec un modèle CTC int8. | Deux chansons entières, dont un rap indépendant, avec graphèmes et contractions conservés. |
| `ce91faf` | Exiger le reçu final pour une comparaison mix et voix. | Un arrêt thermique peut laisser les deux observations intermédiaires. Le banc ne les comptabilise comme paire qu’après règlement. |

## Validation

La validation finale passe 27 fichiers, avec 276 cas Python comptabilisés. Un cas GTK ignoré hors de son environnement a réussi séparément sous Sway headless. Les quatre vérifications JavaScript, dont la simulation de Tizen, et les six configurations linguistiques passent. Les 51 tests d’import et la simulation USB ADB passent. Au premier passage, une assertion supposait un ordre entre deux requêtes parallèles. Le test corrigé vérifie leur contenu sans imposer l’ordre, et exige toujours exactement une requête supplémentaire à la reprise. Le compte rendu du premier passage conserve l’échec. La syntaxe C17 de DTW et du témoin PipeWire passe sous avertissements stricts, avec les en-têtes du moteur, ainsi que la vérification systemd. Les premières invocations C17 ont nécessité une correction des options d’inclusion ou de préprocesseur.

Les deux cas d’intégration de préparation `test_voice_silence_and_accompaniment_waveform_gates` et `test_cross_window_word_has_one_owner` exigent les dépendances Python du moteur vocal. Le venv `hyprwhspr` fournit NumPy et `pywhispercpp`. Les deux cas ont tourné dans ce venv et ont réussi. Les contrôles négatifs supplémentaires passent dans le même environnement. Le score refuse de créditer le texte attendu quand la capture est absente ou quand la piste connue explique le signal. Sur la fixture de capture à 60 % d’un segment, aucune reconnaissance n’est lancée. Ces contrôles portent sur le pipeline du code, des ondes synthétiques et des résultats de reconnaissance construits. Ils ne mesurent pas le chant d’un joueur dans la pièce.

Les protections nocturnes et la reprise ont été testées sur fixtures. L’inhibiteur a été vérifié sur production capot ouvert. La télévision, le téléphone, la fermeture physique du capot, la veille manuelle et l’AEC acoustique demandent un témoin matériel distinct.

## État déployé

Le dernier redémarrage a chargé `ce91faf`. Le service est actif sous le PID 1961547. Parole a été rouverte après l’acquisition du nom D-Bus, avec une seule fenêtre au premier plan. La file d’import a retrouvé `running` et l’inhibiteur de 480 minutes est présent capot ouvert. Les trois copies SQLite avant redémarrage sont distinctes et le contrôle de conservation des inventaires a réussi. [Contrôles et retour arrière](exploitation.md#déploiement-final)

Les fonctions déployées couvrent la propagation PCM devenue éligible hors quota, l’extraction des LRC intégrés, la gestion de la provenance des corrections, la répétition et le ralenti des passages, et le choix des tâches nocturnes admissibles lorsque les fournisseurs attendent. Les compteurs séparent les publications, validations et reports. Le CTC, Qwen, Open-Unmix, la sélection mix-voix, les rapprochements temporels d’éditions et les comparateurs AEC et phonétiques restent au stade du banc d’essai.

L’heure observée en production n’a enregistré ni publication ni chanson préparée en plus. Le total sur 24 h contient encore sept textes et un LRC publiés pendant OMEGA I. Les six fournisseurs de publication étaient hors créneaux. Le rendement nocturne, les recalculs effectivement évités et les latences acoustiques en pièce exigent des observations au-delà des fixtures et de l’heure de bureau. [Mesure de production](exploitation.md#heure-observée-en-production)

## Décisions scientifiques

- Le test de répétitions sur la voix isolée retrouve 51 phrases identiques sur 80 couples proposés. Vingt-neuf couples portent des paroles différentes. Le banc compare les erreurs entre occurrences sans recopier le texte.
- L’accord phonétique, temporel et lexical ajoute une proposition retenue sur les 50 extraits. Le WER reste à 47,08 %. Le corpus de 8 899 enregistrements d’OMEGA I est conservé. La relecture a utilisé le corpus tenu à part, de 8 821 enregistrements.
- Qwen3-ASR 0.6B Q8 tourne sur Radeon. Le WER de 112,50 % et les 163 insertions sur un extrait de rap français écartent son adoption. Le comparatif apparié donne 35,27 % à Turbo sur ces onze extraits. Open-Unmix réduit le pic PSS de séparation observé sur deux extraits. Il augmente les erreurs sur les deux raps indépendants appariés disponibles.
- Le CTC français filtré mesure 196 repères à ±500 ms sur 315 débuts comparables pour *Abandon*, contre 83 pour le témoin Turbo Q5. Sur le rap tenu à part, 196 repères sur 345 entrent dans la tolérance. Dans les contre-épreuves, le filtre retire les 16 faux mots que le treillis avait placés. Les repères sont des propositions de banc avec abstention, en amont d’une éventuelle validation.
- Aucun scénario synthétique AEC3 ou Speex ne satisfait à la fois l’annulation d’écho et la préservation de la voix. Les 34 décisions BlackBox se reproduisent. Les quatre enregistrements témoins n’ont pas de cycles de graphe communs, ce qui empêche un comparatif AEC physique sur ces captures.
- Les droits des fournisseurs et la compatibilité des rééditions restent des conditions de publication. Les quantités de tâches éligibles, d’indices acoustiques, de références vérifiées et de chansons effectivement complétées sont des mesures distinctes. [Audit des fournisseurs et des références](exploitation.md)
