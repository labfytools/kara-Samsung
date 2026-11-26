# Indexation Parole V2.1

## Lecture de tags

Le reçu de publication, les données Strawberry compatibles et les métadonnées mises en cache gardent leur priorité. Lorsque l’index doit lire un média, il peut lancer le petit lecteur TagLib. Un seul enfant est actif dans l’indexeur. Sa mémoire, ses sorties et son temps sont bornés. Le protocole transmet des tags UTF-8, une durée en millisecondes, le codec et une présence de pochette. La lecture des flux, l’extraction des images et les vérifications d’identité utilisent les outils existants. Les formats inconnus, les champs ambigus ou les échecs du lecteur reviennent à FFprobe. Un repère LRC à moins de 150 ms de la limite de validation déclenche aussi une mesure FFprobe. [API propriétés TagLib](https://taglib.org/api/classTagLib_1_1AudioProperties.html) · [API tags et pochettes](https://taglib.org/api/classTagLib_1_1FileRef.html)

Le lecteur est facultatif. `python3 -B bin/_parole_taglib.py` le compile avec les en-têtes de TagLib 2.x et le publie dans le dossier de données Kara de l’utilisateur. L’installation prend effet au prochain inventaire. Aucun compilateur et aucun processus TagLib résident ne sont nécessaires au fonctionnement courant. La version de protocole figure dans le nom de l’exécutable et dans sa réponse.

Le [banc](../tests/qualify-parole-taglib.py) a comparé cinq fichiers avec pochette et texte intégré. Les rapports comparent les crédits, les titres, les numéros, les paroles et les identifiants réellement présents. Le M4A nominal contient les tags usuels du conteneur. Sur un second M4A avec métadonnées indexées QuickTime, le repli FFprobe relit l’identifiant MusicBrainz.

| Témoin | FFprobe | TagLib | Présence de pochette |
| --- | ---: | ---: | --- |
| Opus | 1,3765 s | 1,370 s | Concordante |
| FLAC | 1,370 s | 1,370 s | Concordante |
| MP3 | 1,370 s | 1,416 s | Concordante |
| Ogg Vorbis | 1,370 s | 1,370 s | Concordante |
| M4A AAC | 1,370 s | 1,408 s | Concordante |

Sur 1 000 retagages répartis entre les cinq formats, le même parcours événementiel est passé de **47,01 s** à **2,65 s**, avec **1 000 appels FFprobe remplacés par 1 000 appels au lecteur léger**. Le CPU enfant est passé de 42,82 s à 1,85 s. Les deux exécutions ont livré 1 000 versions en dix transactions, vérifié le nouveau titre sur toutes les pistes et réussi `quick_check`. [Référence V2](parole-indexation-v2.md)
