# Indexation Parole V2

## Réconciliation de la bibliothèque

L’index Parole reste dans `maintenance.sqlite3`, en WAL. Strawberry fournit des données de collection en lecture seule. Le tri indexé d’OMEGA I et les preuves de provenance d’OMEGA II sont conservés. [OMEGA I](omega/rapport.md) · [OMEGA II](omega-ii/rapport.md)

La réconciliation compare les versions du fichier audio, des fichiers associés et du cache Kara. Un fichier associé est relu lorsque sa version change. La lecture bornée vérifie la version avant et après. Le cache de lecture distingue le dernier repère LRC non vide et la présence de texte. La durée du morceau reste un critère de validation du repère. L’index conserve ses pistes inchangées sans écriture et vérifie les disparitions sur un parcours complet. L’identité du répertoire racine et le périphérique des fichiers historiques protègent contre un point de montage indisponible. Les erreurs de parcours suspendent la suppression.

La console `INDEX` indique le résultat et la durée de chaque réconciliation. Les mesures de l’indexeur ventilent le parcours, les versions de fichiers, Strawberry, les métadonnées, FFprobe, les fichiers associés, le LRC, le cache de préparation, les écritures et l’attente avant transaction. Le chronométrage des métadonnées inclut FFprobe quand il est appelé. La transaction inclut le temps d’attente pour son ouverture. Les empreintes acoustiques relèvent des traitements d’import et de préparation existants.

## Premier relevé apparié

La bibliothèque est celle de production, consultée en lecture seule. Les écritures ont utilisé des sauvegardes SQLite privées de 13 880 pistes présentes. Les deux passages ont utilisé le même cache Kara et la collection Strawberry de l’hôte. Les chiffres sont deux passages individuels et leur écart inclut les effets du cache système et de la charge concurrente.

| Mesure | Référence `a895850` | Réconciliation V2 |
| --- | ---: | ---: |
| Inventaire | 4,90 s | 2,01 s |
| Pistes inchangées | 13 880 | 13 880 |
| Appels à FFprobe | 0 | 0 |
| Appels de lecture des fichiers associés, y compris les absents | 27 760 | 0 |
| Appels au parseur LRC | 27 760 | 0 |
| Contrôles du cache Kara | 13 880 | 13 880 |
| Réécritures de pistes | 13 880 | 0 |
| Transactions d’inventaire | 56 | 1 |

La première transaction V2 enregistre l’identité du stockage. Les passages suivants à inventaire égal et sans erreur n’ouvrent aucune transaction d’écriture. Une planification intégrale forcée a pris 0,57 s sur la référence et 0,54 s sur V2. V2 omet cette phase après une réconciliation stable si la politique de maintenance est déjà appliquée.

## Livraison persistante

Le résultat détaillé du tri fournit les tags déjà vérifiés sur la version publiée, avec son instant de publication. L’import enregistre les chemins et leurs preuves dans la base Parole. L’admission des enrichissements couvre aussi les chemins qui viennent d’un reçu de reprise et ceux qui n’ont pas ce détail. Un chemin est unique dans la file, et les notifications combinent les types de changements. La fenêtre démarre à 250 ms, s’étend au maximum jusqu’à 1,5 s lors d’une rafale et s’ouvre dès que 64 chemins attendent.

L’indexeur vérifie le périphérique, l’inode, la taille et les temps à la nanoseconde avant de réutiliser les tags. Il publie les pistes, planifie leur maintenance et accuse réception dans la même transaction WAL. Un événement plus récent conserve son numéro de livraison. Une lecture échouée ou une mutation concurrente diffère la livraison. Le chemin de secours reste la réconciliation. Lorsqu’une écriture de paroles prouve que tous les flux sont inchangés, l’identité PCM existante reste valable pour la nouvelle version. Les références Kara conservent leurs propres vérifications.

Le thread d’indexation appartient au bail de maintenance du service. Il traite des lots bornés pendant que les opérations réseau suivent leur propre cours. Le catalogue de la salle fusionne les chemins publiés. Un album importé ne demande plus d’inventaire global de Parole. Strawberry garde son mécanisme de surveillance et sa base en lecture seule.
