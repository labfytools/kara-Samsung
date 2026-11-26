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

## Classement et crédits

Le trieur consulte la règle manuelle sur le crédit complet, le titre de l’album et l’identifiant d’artiste lorsqu’il est connu. Une réédition couverte peut donc recevoir le dossier et le tag `albumartist` du même artiste canonique. Le tag `artist` et les crédits de provenance sont conservés. Le chemin proposé par une discographie continue d’orienter le classement sans autoriser à lui seul un changement de crédit. Une autre collaboration ou une édition hors de la règle reste indépendante. La file de reprise accepte un fichier retagué sur preuve du classement validé, du crédit original, de la position et du PCM.

`python3 -B bin/_parole_artist_tags.py --artiste 'Bob Marley'` relève les candidats de la bibliothèque indexée. L’option `--appliquer` exige de nouveau la règle et la version exacte du fichier. Elle crée une sauvegarde privée et durable de l’original avec un reçu SHA-256, vérifie l’identité du PCM et de tous les flux, puis publie le fichier atomiquement et notifie l’index. Une piste liée à une préparation est signalée pour réassociation et conserve sa version actuelle. Les reçus se trouvent dans `$XDG_STATE_HOME/kara/albumartist-backups`, ou sous `~/.local/state` sans configuration XDG. Strawberry découvre le fichier par sa surveillance. Le mode de synchronisation Android qui suit la disposition PC reçoit le chemin et les octets du fichier publié.

L’audit direct de 3 727 pistes sous `Bob Marley` a trouvé 3 701 tags déjà cohérents et 26 crédits hors des règles applicables. Aucun retagage de production n’était admissible. Les tests ont vérifié la sauvegarde exacte, le refus sur preuve PCM manquante, les préparations protégées, l’idempotence et l’identité PCM conservée par l’index après notification.

## Import mesuré

Le véritable importeur géré a publié une copie de sept pistes audio d’un album déjà présent sur le PC, dans une bibliothèque isolée. Le dossier d’entrée portait le crédit d’album couvert par une règle Bob Marley déjà validée. Les sept fichiers publiés conservent le PCM et le tag `artist` originaux. Le nouvel `albumartist` correspond au dossier, à Parole et à la collection du véritable Strawberry lancé dans un profil isolé. Le test observe les fichiers sur disque, puis les listes et les bases à intervalles d’environ 20 ms. La dernière publication audio est intervenue 8,36 s après le démarrage du processus d’import local.

| De la publication audio à… | Minimum | Médiane | Maximum |
| --- | ---: | ---: | ---: |
| l’index Parole | 0,270 s | 0,433 s | 0,582 s |
| la liste GTK Parole | 0,423 s | 0,589 s | 0,913 s |
| la collection Strawberry | 0,528 s | 2,750 s | 4,732 s |

Ce résultat mesure un import local et le véritable scanner Strawberry sur ce profil. Il ne comprend aucun débit fournisseur et ne mesure pas l’arrivée des titres sur un téléphone.
