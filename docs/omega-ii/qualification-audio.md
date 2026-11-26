# Qualification audio OMEGA II

## Référence et critères

Les 50 extraits d’OMEGA I restent la comparaison historique. Le mix Turbo F16 totalise 620 erreurs sur 1 317 mots, soit 47,08 % de WER. La voix isolée atteint 48,97 %, le passe-haut des voix 48,14 % et la combinaison mix-voix 47,68 %. La couverture des repères et la précision parmi les seuls points appariés sont présentées séparément. Un mot forcé sans preuve indépendante reste un faux positif potentiel. [Qualification OMEGA I](../omega/qualification-audio.md)

| Groupe historique | Extraits | Mix F16 | Voix F16 |
| --- | ---: | ---: | ---: |
| Rock | 6 | 22,58 % | 29,84 % |
| Rap américain | 16 | 57,54 % | 66,59 % |
| Rap français | 20 | 47,81 % | 46,80 % |
| Chanson française | 2 | 8,33 % | 11,11 % |
| Reggae | 4 | 49,40 % | 32,53 % |
| Soul | 2 | 32,65 % | 24,49 % |

Les insertions du WER comprennent potentiellement des ad-libs réellement chantés et absents du texte. Les différences de consonnes, de basse, de chœurs, de réverbération et de débit restent des hypothèses sans écoute ni annotation spécialisée. Le patois jamaïcain et le code-switching ne sont pas validés comme classes de décision.

## Modèles étudiés

| Candidat | Code, poids et langues | Coût et niveau de preuve |
| --- | --- | --- |
| **HeartTranscriptor-oss** | HeartLib et poids Apache-2.0. Poids publics SafeTensors FP32 de 3,06 Go, dérivés de Whisper Medium. Les auteurs évaluent anglais, chinois, coréen, japonais et espagnol sur voix séparées. | Sur les 100 titres anglais de SSLD-200, les auteurs mesurent 28,16 % contre 47,37 % pour Turbo, avec la même séparation. Le travail ne fournit pas de résultat français ni de mesures sur le Vivobook. L’adaptation des poids au moteur local et sa parité restent à vérifier. [Article](https://arxiv.org/html/2601.10547v1#S5) · [Poids](https://huggingface.co/HeartMuLa/HeartTranscriptor-oss/tree/main) · [Code](https://github.com/HeartMuLa/heartlib) |
| **Qwen3-ASR** | Code et poids d’origine Apache-2.0, 30 langues dont anglais et français. Conversion 0.6B Q8_0 de 850 423 456 octets avec le moteur `transcribe.cpp`. | La soumission MIREX 2026 de la version 1.7B, alimentée par des fenêtres tirées de Whisper, donne 25,93 % de WER sur Jam-ALT contre 28,60 % pour son témoin Whisper. Cette mesure ne porte pas sur les poids 0.6B Q8, ni sur le rap ou le reggae d’OMEGA II. Le CPU AMD passe le témoin parlé. [Dépôt](https://github.com/QwenLM/Qwen3-ASR) · [Conversion GGUF](https://huggingface.co/memoravox/Qwen3-ASR-0.6B-gguf) · [Résultats MIREX](https://music-ir.org/mirex/wiki/2026:Lyrics_Transcription_Results) |
| **Wav2Vec2 Base 960h CTC** | Poids de base Apache-2.0, anglais de LibriSpeech. Export ONNX quantifié de 95 286 046 octets. Exécution CPU dans ONNX Runtime, sans PyTorch. | Exécuté sur une chanson entière et un témoin de texte délibérément faux. Candidat à l’alignement d’un texte anglais connu. Une extension phonétique multilingue utiliserait des poids et des règles de normalisation propres à la langue. [Poids de base](https://huggingface.co/facebook/wav2vec2-base-960h) · [Export utilisé](https://huggingface.co/Xenova/wav2vec2-base-960h/tree/a19f851b3d42865797e410752b4c570c871e4825/onnx) |
| **Echora Kara 300M** | Code AGPL-3.0 et poids gated CC BY-NC 4.0. Encodeur et têtes d’environ 354 M de paramètres. Mesures sur anglais, français, espagnol et allemand. | Sur 79 titres Jamendo, les auteurs rapportent 97,6 % des mots à ±500 ms après choix d’un poids de texte sur cinq titres du même ensemble. Les omissions, réordonnancements et ajouts du texte ne sont pas détectés par ce système. Accès aux poids sur acceptation authentifiée, coût AMD et mémoire locaux ouverts. [Code](https://github.com/hcx-labs/echora-kara) · [Carte et résultats](https://huggingface.co/hcxlabs/echora-kara-300m) |
| **Open-Unmix UMX-HQ** | Implémentation PyTorch MIT, poids UMX-HQ MIT, environ 35,6 Mo par stem. Séparation musicale indépendante de la langue. | Témoin candidat au remplacement de Demucs sur CPU. Les résultats de séparation MUSDB18 ne mesurent pas le WER des paroles. Mesurer le PSS, les consonnes et les pertes de voix avant de modifier le parcours. [Code](https://github.com/sigsep/open-unmix-pytorch) · [Poids et mesures](https://zenodo.org/records/3370489) |

Les travaux CTC en chant proposent soit une tête acoustique adaptée, soit des ancrages dans de courtes fenêtres suivis d’un alignement monotone. SOFA publie un outil spécialisé, avec des dictionnaires et des poids à établir pour chaque langue. Les travaux wav2vec2 multilingues améliorent la compréhension des limites du transfert, mais les résultats d’un papier ne suffisent pas à établir qu’un checkpoint précis est accessible sous une licence exploitable. Les variantes MDX23C et RoFormer sont intéressantes pour la séparation. Le gain documenté en SDR et la licence d’un moteur d’inférence ne déterminent ni le coût sur Radeon 780M, ni les droits attachés au checkpoint choisi, ni le WER. [CTC et attention sur les paroles](https://arxiv.org/abs/2207.09747) · [Alignement par ancrages](https://arxiv.org/abs/2102.09202) · [SOFA](https://github.com/qiuqiao/SOFA) · [Modèles de séparation](https://github.com/ZFTurbo/Music-Source-Separation-Training/blob/main/docs/pretrained_models.md)

## Premier résultat CTC

Sur *Feel (Stripped)* de Cortez, 240,56 s, les annotations indépendantes couvrent 334 débuts pour 376 mots normalisés. Le prototype lit le mix en fenêtres de 16 s entourées de 2 s de contexte, avec une seule occurrence de chaque trame au recouvrement. La grille du modèle a un pas de 20 ms et le treillis CTC conserve l’ordre des graphèmes du texte. Le contrôle remplace huit mots répartis dans la chanson. Ces mots sont volontairement absents de l’audio. La mesure considère le nombre total de repères annotés, y compris lorsque le filtre s’abstient. [JamendoLyrics MultiLang](https://huggingface.co/datasets/jamendolyrics/jamendolyrics)

| Parcours | Débuts évalués | Tous les débuts à ±500 ms | p95 des débuts évalués | Mots falsifiés conservés |
| --- | ---: | ---: | ---: | ---: |
| Turbo Q5 mix, OMEGA I | Voir reçu historique | 77,25 % | Voir reçu historique | Témoin non réalisé |
| CTC mix forcé | 334/334 | 93,41 % | 634 ms | 8/8 |
| CTC, minimum de posterior par caractère ≥ 0,20 | 98/334 | 28,74 % | 236 ms | 0/8 |
| CTC, minimum ≥ 0,60 | 53/334 | 15,57 % | 257 ms | 0/8 |

Le pire début forcé s’écarte de 9,25 s. Le passage sans filtre place 93,71 % des fins à ±500 ms. Le taux de débuts dans la tolérance augmente sur ce titre face au témoin Q5, mais le texte faux montre l’effet du chemin forcé. Le filtre élémentaire rejette ces faux mots au prix d’une forte perte de couverture. Le modèle sert d’indice expérimental pour un texte connu. Il n’est ni un décodeur libre évalué contre le WER historique, ni une validation de publication.

Le premier passage a pris 138,76 s sous un quota CPU de 30 % limité au groupe d’essai. Il a mesuré 756 Mio de PSS au plus pour le groupe échantillonné, 74,88 °C au CPU, 58 °C au GPU et 0,09 % de PSI mémoire. La passe sur le stem de ce titre a été interrompue à 78,75 °C. Les seuils de production sont restés actifs.

## Faisabilité du pilote Qwen CPU

Le binaire `transcribe.cpp` a été compilé dans l’espace privé sur la révision `3f61df75e726080c7597c5abc896d55b09330f43`. L’édition de liens locale fournit explicitement la bibliothèque CBLAS déjà installée. Le fichier Q8_0, publié avec son empreinte `f081b2d5e23bd669d92cc331d722a8a0681943b8e6f34b48996fd5c319b5acd8`, a passé le contrôle local. Une fixture parlée de onze secondes intégrée au projet amont a été transcrite en 3,25 s d’inférence, avec chargement en 0,58 s, sur le CPU. Le sous-processus voyait uniquement sa courte entrée audio et les ressources nécessaires. Son espace réseau était isolé.

Les essais de chant sur CPU ont produit 31 reports en plusieurs passes sur des extraits qui se recouvrent. Vingt-et-un sont attribués à la température, huit au PSI mémoire et deux à l’absence de marge d’admission. Le PSS maximal observé pour un groupe est de 1 736 Mio, le PSI maximum de 1,25 %. Les réglages comptent des passages à un ou deux fils et des plafonds de calcul appliqués seulement au groupe expérimental. Aucun extrait de chant CPU complet ne fournit un WER exploitable. Les températures et les reports mesurent la faisabilité dans l’état observé de l’hôte.

## Sélection prudente mix ou voix

Sur les reçus historiques, une grille de seuils utilise le nombre de mots au-dessus de 0,62 de confiance et la confiance médiane pendant les cinq premières secondes. Chaque séparation coûte un point d’erreur dans la fonction d’ajustement pour écarter les gains trop faibles. La grille a préféré **zéro séparation**. Les résultats ajustés sur ces 50 mêmes extraits sont descriptifs. Le choix reggae par genre aurait donné 46,01 % avec quatre séparations. Les références de ces quatre extraits avaient déjà motivé la règle. L’ensemble d’évaluation tenu à part fige 20 nouveaux enregistrements, 8 en rap américain, 8 en rap français et 4 en reggae, avec un texte total de 638 mots. Le filtre exclut les identifiants, les couples artiste-titre, les versions audio exactes et les paroles normalisées déjà rencontrées. Il partage des artistes avec le corpus historique, faute de titres admissibles en nombre suffisant avec exclusion de tous les artistes. Le regroupement de genre reprend la table utilisée pour échantillonner OMEGA I. Il ne devient pas un classificateur de production.

| Règle sur les 50 extraits historiques | WER | CER | Séparations nécessaires | Choix de la voix |
| --- | ---: | ---: | ---: | ---: |
| Mix systématique | 47,08 % | 37,55 % | 0 | 0 |
| Voix systématique | 48,97 % | 37,02 % | 50 | 50 |
| Genre reggae | 46,01 % | 36,59 % | 4 | 4 |
| Confiance et débit sur les cinq premières secondes | 47,08 % | 37,55 % | 0 | 0 |
| Basses et confiance initiale | 49,43 % | 39,09 % | 5 | 5 |
| Accord prudent si le stem existe déjà | 45,03 % | 35,73 % | 0 | 9 |

La mesure acoustique utilise la part de l’énergie entre 50 et 350 Hz sur les cinq premières secondes. Les médianes sont de 69,75 % pour le reggae, 66,86 % pour le rap américain et 47,61 % pour le rap français. Les plages se recouvrent largement. La règle déclenche à partir de 65 % d’énergie dans cette bande et sous 0,62 de confiance médiane du mix. Elle reproduit mal le gain du reggae et coûte 31 erreurs sur le lot historique. La dernière règle suppose le stem présent. Elle exige au moins 80 % du nombre de mots reconnus sur le mix et une confiance médiane supérieure d’au moins 0,05. Ces décisions ne lisent pas les paroles de référence. Leur WER est mesuré après le choix. [Banc de sélection](../../tests/qualify-parole-input-policy.py)

| Groupe | Substitutions mix → voix | Omissions mix → voix | Insertions mix → voix |
| --- | ---: | ---: | ---: |
| Rap américain | 71 → 64 | 156 → 203 | 21 → 20 |
| Rap français | 112 → 106 | 108 → 103 | 64 → 69 |
| Reggae | 9 → 11 | 26 → 12 | 6 → 4 |

Les omissions expliquent l’essentiel de la différence historique sur le rap américain et le reggae. Les métriques ne déterminent pas si Demucs a écarté des consonnes, des chœurs ou des ad-libs, ni si Turbo réagit aux changements de timbre. Le reggae reste représenté par quatre extraits historiques. Le rap français doit garder ses résultats séparés. Une amélioration moyenne ne doit pas compenser une perte dans un groupe.

## Première validation distincte sur le hip-hop anglais

Le titre *CROWD PLEASER* de JASON MILLER, 192,80 s et 555 mots normalisés, a été fixé avec *Embers* d’Avercage avant cet essai. Ce sont deux nouveaux enregistrements de JamendoLyrics, conservés pour le calcul local sous CC BY-NC-SA. Après un arrêt thermique, le passage hip-hop a repris deux fenêtres CTC sauvegardées et a terminé les onze autres avec le même modèle. Cette reprise a pris 152,12 s sous un plafond CPU local de 22 %, avec environ 761 Mio de PSS de groupe, 75,5 °C au CPU et 0,22 % de PSI mémoire. Les durées de reprise décrivent l’invocation complétée et ne comprennent pas le travail du passage interrompu. [Corpus épinglé](https://huggingface.co/datasets/jamendolyrics/jamendolyrics/tree/de188c963fd4539bc769b3feb83582e5a9595e36) · [Licence CC BY-NC-SA](https://creativecommons.org/licenses/by-nc-sa/4.0/)

| Témoin CTC sur le mix | Débuts évalués / annotés | Tous les débuts à ±500 ms | p95 sur les débuts retenus | Débuts de ligne à ±500 ms | Mots falsifiés conservés |
| --- | ---: | ---: | ---: | ---: | ---: |
| Treillis forcé | 487/487 | 100 % | 131 ms | 100 % | 8/8 |
| Minimum de posterior par caractère ≥ 0,20 | 217/487 | 44,56 % | 100 ms | 50,00 % | 0/8 |
| Moyenne géométrique ≥ 0,20 | 305/487 | 62,63 % | 100 ms | 62,20 % | 0/8 |
| Corroboration par décodage CTC sans texte | 144/487 | 29,57 % | 99 ms | Non mesuré | 0/8 |

Le dernier témoin autorise un repère forcé si le même mot apparaît dans le décodage sans texte, à moins de 500 ms et avec recouvrement des intervalles. Un mot libre corrobore au plus une occurrence, dans l’ordre. Les deux chemins partagent les mêmes émissions acoustiques. L’essai montre un veto complémentaire, avec une faible couverture sur ce titre. Le score de précision du treillis brut reste conditionné au texte juste. Le contrôle artificiel de huit substitutions ne suffit pas à valider des passages live, des refrains omis ou des ad-libs.

### Généralisation, abstention et références incomplètes

| Chanson entière | Débuts annotés | Treillis brut à ±500 ms | Moyenne géométrique ≥ 0,20 à ±500 ms | Corroboration libre à ±500 ms |
| --- | ---: | ---: | ---: | ---: |
| *Feel (Stripped)*, premier titre évalué | 334 | 312 (93,41 %) | 126 (37,72 %) | 54 (16,17 %) |
| *CROWD PLEASER*, hip-hop tenu à part | 487 | 487 (100 %) | 305 (62,63 %) | 144 (29,57 %) |
| *Embers*, rock tenu à part | 176 | 8 (4,55 %) | 0 (0 %) | 0 (0 %) |

Sur *Embers*, la couverture brute du treillis est de 176/176 car le texte est forcé. Le p95 dépasse 77,7 s. La moyenne à 0,20 ne garde qu’un mot, avec 76,5 s d’erreur. Le décodage libre produit seulement 16 mots contre 518 sur le hip-hop. Un prototype d’ancrage sur des paires libres contiguës n’a trouvé aucun ancrage dans ce rock. Sur le hip-hop, il place 54 débuts sur 487 à ±500 ms et son p95 dépasse 9 s. Un passage court qui se répète peut produire un ancrage à la mauvaise occurrence.

Le témoin remplace aussi huit mots courants par chanson, par exemple `you` par `we`, avec le mot falsifié absent des annotations dans la seconde voisine. Le filtre géométrique à 0,20 et la corroboration libre écartent les 24 substitutions. Le retrait d’une ligne intermédiaire, de cinq à huit mots, fait glisser un autre repère dans la période omise sur le titre hip-hop et sur *Feel*. La corroboration refuse ces deux repères. Sur le hip-hop, le treillis conserve 477 des 482 autres débuts à ±500 ms. La corroboration en conserve 144. Sur le rock, la dérive reste présente. La décision d’utiliser ce CTC comme source de minutage reste ouverte.

*Embers* a terminé en 200,40 s après reprise sous le plafond CPU local de 22 %. Le PSS de groupe culmine à 759 Mio et le PSI à 0,22 %. Le second passage intégral de *Feel* avec le filtre géométrique prend 209,71 s sous le même plafond, avec 760 Mio de PSS et 0,18 % de PSI. Les deux passages achevés utilisent le modèle ONNX et le vocabulaire signés. Les fenêtres du passage interrompu de *Embers* comptent dans le travail initial et sont relues par sa reprise.

Dans un environnement local contenant NumPy 2.2.6 et ONNX Runtime 1.24.4, [le banc CTC](../../tests/qualify-parole-ctc.py) utilise un manifeste de chanson entière avec les empreintes audio et des trois fichiers d’annotation indépendants. [Le banc de contre-épreuves](../../tests/qualify-parole-ctc-evidence.py) reprend le reçu et ses fenêtres acoustiques. Le fait de changer l’audio, les poids, le vocabulaire ou la version du banc invalide la reprise. Les normalisations sont en anglais. Le lexique et la référence attendue ne sont jamais fournis au modèle acoustique.

## Kara, écho et préservation de la voix

Le parcours microphone local de `_kara_voice.py` demande une source PipeWire d’annulation d’écho en mode moniteur et revient au microphone si elle est indisponible. Le chemin mobile peut demander le traitement du navigateur et rapporte les réglages appliqués. Le mode `remote-only` du navigateur vise les pistes WebRTC reçues. Le mode `all` demande de retirer les signaux joués par le système. La version et le chemin effectivement utilisés doivent être relevés lors d’un témoin. Le module PipeWire associe une capture à un signal de lecture. La bibliothèque SPA WebRTC de ce poste charge `webrtc-audio-processing-1.so.3`, comme le témoin AEC3, mais l’horloge des nœuds, le rééchantillonnage et le chemin de lecture restent spécifiques au graphe. [Module PipeWire](https://docs.pipewire.org/page_module_echo_cancel.html) · [API WebRTC AEC3](https://webrtc.googlesource.com/src/+/f0edfc399074da2c0b6a3fe866462fa464addf5c/modules/audio_processing/aec3/echo_canceller3.h) · [Contraintes de capture W3C](https://www.w3.org/TR/mediacapture-streams/#dom-mediatrackconstraintset-echocancellation)

Le rejeu synthétique à 48 kHz utilise des trames de 10 ms et une voix sèche connue. Les bibliothèques prises en charge localement sont WebRTC Audio Processing 1.3 et SpeexDSP 1.2.1. Les mesures de voix portent sur des signaux synthétiques et sont calculées après compensation du retard observé de l’algorithme.

| Scène synthétique | Écho retiré par AEC3 | Écho retiré par Speex | Gain de la voix faible AEC3 / Speex | Gain du chant AEC3 / Speex |
| --- | ---: | ---: | ---: | ---: |
| Retard fixe | 23,85 dB | 21,29 dB | −7,81 / −1,13 dB | −3,42 / −1,04 dB |
| Dérive de 180 ppm | 23,16 dB | 6,26 dB | −11,49 / −1,00 dB | −3,85 / −1,05 dB |
| Chemin à retard variable, réglé à 360 ms | 54,22 dB | 0,81 dB | −66,49 / −0,88 dB | −1,89 / −1,04 dB |
| Mix stéréo | 25,05 dB | 15,10 dB | −18,25 / −1,23 dB | −4,79 / −1,05 dB |

Le p95 du calcul de trame à retard fixe vaut 0,257 ms avec AEC3 et 0,259 ms avec Speex. Le délai de l’impulsion mesurée est de 8,96 ms et 0 ms respectivement. Ces valeurs excluent les tampons du périphérique et du graphe. Le retour de la propre voix en boucle baisse de 52,41 dB avec AEC3, accompagné d’une perte de 9,58 dB sur le chant. Speex réduit le retour de 15,23 dB avec une perte de 1,83 dB sur le chant. Aucun scénario ne valide à la fois l’atténuation et le seuil de préservation de 15 dB de SI-SDR. Les résultats n’isolent pas la voix du disque de l’accompagnement dans une vraie pièce. Le chemin temps réel et EasyEffects conservent leur configuration.

Le score compare des mots décodés sans le texte attendu, après des contrôles sur le signal et la fuite de la piste connue. La tolérance de score de 1 s sépare cette décision de l’objectif de repères à 500 ms. Les témoins de silence, accompagnement, voix originale, guide, parole différente, refrain hors repère et capture interrompue conservent l’absence de crédit lorsque leur signal ne permet pas d’attribuer le chant au joueur. Les essais sur capture enregistrée rejouent aussi les décisions historiques sans attribuer un nouveau score à un participant.

Le contrôle en lecture seule retrouve 12 captures BlackBox closes et intègres. Les 34 décisions de notation sont reproduites. Dans les quatre captures qui contiennent le moniteur et le témoin, aucun cycle identifié par la même horloge de graphe ne se recouvre. Le comparatif AEC sur matériel ne dispose donc d’aucune fenêtre commune. Le p95 synthétique ne décrit pas la latence du retour microphone en situation réelle.

## Suggestions lexicales après reconnaissance

[Le banc de consensus](../../tests/qualify-parole-lexical-consensus.py) relit le mix, les voix Demucs et les mêmes voix filtrées des 50 extraits. Une graphie du mix à faible confiance doit bénéficier d’un accord des deux parcours vocaux à 220 ms près, dans la langue détectée en commun. Un voisin lexicographique est utile uniquement si une expression de deux mots figure dans le corpus excluant les titres évalués. Les deux parcours vocaux partagent une séparation. Leur accord reste un indice pour l’éditeur.

L’égalité de la clé phonétique grossière a produit zéro proposition. Avec une distance d’édition d’au plus un entre clés d’au moins quatre lettres, deux propositions apparaissent. Une seule reçoit l’appui d’un bigramme, en rap américain. Le nombre d’erreurs reste à 620 sur 1 317 mots, et le WER à 47,08 %. Les accents, contractions, mots courts, argot et alternances de langues demandent des témoins phonétiques mieux adaptés. Le banc n’a posé aucune suggestion en production.

## Répétitions vocales

[Le banc local](../../tests/qualify-parole-repetitions.py) compare l’évolution spectrale de passages de 3 s espacés d’au moins 8 s. Sur la voix séparée, il exige que l’activité dépasse −38 dBFS pendant au moins 60 % des deux fenêtres. Il retient jusqu’à vingt couples par entrée et par titre, sans ouvrir le texte pour choisir ces couples. Les annotations indépendantes servent ensuite à qualifier l’égalité des phrases d’au moins trois mots. Les quatre titres sont des chansons entières déjà munies de stems et de résultats Turbo Q5.

| Signal | Couples proposés | Phrases identiques | Phrases différentes | Moins de trois mots annotés dans une fenêtre |
| --- | ---: | ---: | ---: | ---: |
| Mix | 80 | 36 | 23 | 21 |
| Voix séparée | 80 | 51 | 29 | 0 |

Parmi les 51 répétitions retrouvées sur la voix, les deux lectures Turbo n’ont pas le même nombre d’erreurs dans 31 couples. Le nombre d’omissions diffère dans 20, celui des substitutions dans 23 et celui des insertions dans 11. Ces décomptes se recoupent. Le signal vocal augmente le nombre de répétitions retrouvées dans ce lot, mais 29 phrases différentes restent proposées. Les variations et les ad-libs peuvent aussi faire diverger la référence. Ce résultat permet de chiffrer des occasions de comparaison dans l’éditeur. Aucun mot n’a été recopié ni publié. L’essai ne mesure donc aucun gain de WER après correction.

## Séparation vocale Open-Unmix sur CPU

[Le pilote UMX-HQ](../../tests/qualify-parole-unmix.py) fixe le premier extrait de chacun des six genres OMEGA I. Le seul poids téléchargé contient la tête vocale, 17 820 856 octets. Le bac isolé emploie ONNX Runtime sur un fil CPU, la STFT à 44,1 kHz, sept secondes de contexte de chaque côté lorsque disponibles et la phase du mix pour reconstruire la voix. Il réutilise le même Turbo F16 et le même intervalle temporel pour mesurer le WER. Ni la tête instrumentale ni le filtre multicanal de Wiener ne sont évalués. [Export ONNX épinglé](https://huggingface.co/edgetools/umx-hq/commit/87f26a5b2ed19488b5e453059a0a1d530423fe4a) · [Chaîne d’inférence Open-Unmix](https://github.com/sigsep/open-unmix-pytorch/blob/master/docs/inference.md)

| Premier passage | Mots de référence | Erreurs mix F16 | Erreurs Demucs + F16 | Erreurs UMX-HQ + F16 |
| --- | ---: | ---: | ---: | ---: |
| Rock | 17 | 1 | 1 | 1 |
| Rap américain | — | — | — | Report thermique |
| Rap français | 12 | 12 | 12 | 12 |
| Chanson française | — | — | — | Report thermique |
| Reggae | 18 | 3 | 4 | 3 |
| Soul | 23 | 11 | 8 | 7 |
| Quatre paires complètes | 70 | 27 (38,57 %) | 25 (35,71 %) | 23 (32,86 %) |

Les quatre séparations terminées prennent de 2,17 à 2,95 s de la création du modèle à l’écriture PCM. Le PSS de groupe échantillonné se situe entre 271 et 362 Mio. Les deux reçus Demucs avec PSS directement exploitable sur le reggae et la soul indiquent respectivement 1 318 et 1 386 Mio, pour 36,43 et 36,88 s. Les réglages locaux de charge et les dates de ces passages diffèrent. Le PSI maximal du pilote terminé vaut 0,41 %. Deux cas ont également attendu en admission. Les échantillons thermiques à 82,5 °C avant le départ du stem reggae font partie de cette attente. L’exécution n’a repris qu’après admission par le gardien commun. Les mesures de 1 781 Mio (sept secondes) et de 1 606 Mio (cinq secondes) sur une chanson entière appartiennent à la campagne Demucs OMEGA I.

Sur ce pilote, un moteur plus léger apporte une réduction de mémoire mesurée et un WER descriptif à vérifier. Il ne qualifie ni le rendu instrumental de Kara, ni le rap américain, ni le coût sur chanson entière. L’export conserve la phase du mix, ce qui peut laisser de l’accompagnement dans une voix estimée. La précision des consonnes et l’impact sur les chœurs n’ont pas d’annotation indépendante dans ce lot.
