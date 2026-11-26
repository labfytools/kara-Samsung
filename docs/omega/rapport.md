# Rapport de mission Omega

## Bilan

Mission du 26 novembre 2026, de 11 h 33 à 14 h 34 CET, soit 3 h 01 de travail et de calcul suivis. Branche locale `feat/audio-latency-optimization`, départ `1f962ba`. Parole et `kara.service` exécutent les corrections validées. À la dernière observation, le processus 1089599 possède seul le nom D-Bus et la fenêtre est affichée. Les bases passent `quick_check`, les 13 201 pistes et les états d’import sont préservés. Aucun push.

Le comparatif a reproduit les 50 résultats historiques du mix. Le meilleur réglage global qualifié sur ces 50 extraits reste le mix F16 à **47,08 % de WER**. La séparation vocale aide certains titres entiers et certains genres, avec des dégradations ailleurs. Sur les 50 extraits, le reggae progresse de 49,40 % à 32,53 % de WER sur quatre titres et le rap américain passe de 57,54 % à 66,59 % sur seize titres. Le mix continue d’alimenter les brouillons. Ceux-ci restent soumis à validation manuelle.

La préparation nocturne conserve les réglages existants. Après le redémarrage, l’inhibiteur de capot était armé pour le nouveau PID, la limite était de 480 minutes et les seuils de mémoire, de température et de disque avaient gardé leurs valeurs. Les essais physiques historiques restent les seules validations de fermeture du capot, de débranchement et de veille manuelle. [Déploiement et sauvegardes](exploitation.md) · [Qualification capot fermé](../parole-capot-ferme.md)

## Résultats audio

| Mesure | Résultat |
| --- | --- |
| Corpus exact F16 | 50 extraits, 6 genres, 1 317 mots. Mix 47,08 %, voix 48,97 %, niveau 48,90 %, filtre 48,14 %, découpage 52,47 %, relecture 49,51 %, combinaison 47,68 %. |
| Relecture phonétique | 50 essais, zéro substitution acceptée. Le corpus historique de 8 899 entrées est intact. L’export utilisé dans le banc en compte 8 821 après mise à part des cas d’évaluation. |
| Chansons entières Q5 | Quatre titres achevés sous le seuil de 1 % avec des stems déjà disponibles. WER mix et voix de 8,08 % et 6,77 % sur le rap espagnol, 16,49 % et 18,88 % sur la pop anglaise, 74,53 % et 57,45 % sur le country français, 14,53 % et 11,82 % sur la pop française aux paroles superposées. |
| Alignement | Les repères indépendants montrent des écarts maximaux au-delà de 500 ms. Sur le country, le p95 des débuts de mots appariés est de 363 ms sur la voix. Seuls 140 débuts sur 315 sont retrouvés. |
| Mémoire | Les onze reports de la première passe concernent dix dépassements PSI et un franchissement de réserve. Les causes enregistrées se répartissent entre Demucs, le chargement de Turbo, les buffers PCM et le décodage. Les deux anciens arrêts Demucs précédaient le démarrage de Turbo. |
| Demucs entier | Essais à 7 secondes en version à travailleur unique et sérielle, 221,82 s et 208,88 s pour le même PCM. Le sériel augmente le PSS de 1 561 à 1 781 Mio et franchit encore le seuil strict de PSI de 1 %. L’essai strict à 5 s s’est arrêté à 1,24 %. |
| Fenêtres Demucs de 5 s | Le profil manuel a terminé en 276,23 s, à 1 606 Mio de PSS, 1,72 % de PSI et 4,10 Gio disponibles au minimum. En sériel à 7 s, le temps était de 208,88 s et le pic de 1 781 Mio. Les deux sorties vocales ont une corrélation de 0,9955 en PCM mono 16 kHz. |
| GPU partagé | GTT résident observé jusqu’à 2 089 Mio pour le client F16 et 1 016 Mio dans l’essai Q5 espagnol. La mémoire graphique reste dans la RAM de l’hôte. |

Les résultats par genre, le CER, les substitutions, les insertions, les suppressions, les intervalles entre annotations et la couverture des repères sont détaillés séparément. Les insertions peuvent comprendre des mots réellement chantés et absents de la référence. Sur les extraits, les noms propres, les contractions et l’argot restent sans annotation suffisante pour publier un taux fiable. Les résultats des titres entiers portent sur un petit nombre d’enregistrements publics, avec quatre titres de trois genres et trois langues. [Qualification audio et méthode](qualification-audio.md)

Sur les neuf paires Q5/F16 complètes du sous-lot de douze extraits, le WER du mix passe de 31,11 % à 28,89 % sur 180 mots, tandis que la voix isolée passe de 31,11 % à 33,33 %. Les trois autres extraits Q5 ont été reportés pour PSI. La mémoire GTT résidente du client passe de 2 089 à 954 Mio. Le brouillon local Q5 du titre country a terminé, avec une seule ligne retenue et 36 segments écartés. La reprise a relu le même reçu en 0,66 s. Q5 reste expérimental.

## Performance et exploitation

| Sujet | Avant et après |
| --- | --- |
| Début de la liste de 80 pistes | Médiane 16,16 → 0,37 ms sur l’index réel copié, vingt lectures appariées et même ordre. |
| Recherche « love » | 15,25 → 2,49 ms en médiane. Le terme absent est resté dans la même plage, de 13,71 à 13,31 ms. |
| Références PCM quand la file externe est indisponible | Test de publication locale avec quotas pleins et sans débit de quota. La disponibilité locale prend 0,015 ms en médiane sur l’index échantillonné. |
| Publications externes | 7 textes et 1 LRC sur 11 h 11–12 h 11. Zéro texte et zéro LRC sur chacune des fenêtres suivantes, en présence de six fournisseurs à 200 créneaux sur 200. Les deux dernières fenêtres se chevauchent. |
| Inventaire | 13 201 audio, 7 453 fichiers LRC présents, dont 21 à vérifier. L’index en comptait 7 432 disponibles et 8 915 pistes avec paroles. Parmi les pistes indexées, 4 283 étaient sans paroles et 5 748 sans LRC. Deux derniers repères dépassent la durée audio. |
| Fiabilité du démarrage | Une ouverture trop rapide a pris le nom D-Bus avant le service lors d’un redémarrage. L’instance graphique a été fermée proprement. `Type=dbus` a été vérifié sur une unité de test à prise de nom retardée, puis en production. |

Le relevé final retrouve les 13 201 chemins de départ et 679 fichiers Opus récents supplémentaires. Les 13 880 fichiers indexés existent, et 8 915 pistes ont des paroles, 7 432 un LRC reconnu. La file d’import a conservé ses 30 identifiants, avec un passage de l’examen à la publication partielle. L’audit de rééditions a comparé 24 paires de 24 groupes. Douze ont exactement le même PCM. Les douze autres atteignent tout de même 0,90 de similarité Chromaprint. Aucune paire n’offrait une preuve de provenance suffisante pour publier des paroles ou un LRC automatiquement. [Audit et rendement](exploitation.md)

## Chantiers et validation

| Chantier | État | Validation |
| --- | --- | --- |
| Brouillons sur le mix, file persistante et diagnostics mémoire | Déployé | Tests de reprise, arrêt du groupe, télémétrie PSI, non-régression nocturne, tentative réelle F16 sous garde. |
| Écoute d’un passage incertain sur le mix ou la voix déjà séparée | Déployé | Quatre tests d’écoute, une fixture GTK et un flux `pw-play` sur deux canaux dans un graphe PipeWire virtuel. |
| Identité PCM, rééditions et quotas | Déployé | Deux éditions distinctes sur des FLAC de PCM identique. La vérification refuse le transfert lorsque la référence a changé ou que les références divergent. Le LRC local reste disponible quand les quotas externes sont épuisés. |
| Corrections manuelles et lexique | Déployé avec contexte désactivé | Reçus associés à la version audio et au fichier édité. Déduplication des preuves d’enregistrement. Relecture phonétique expérimentale. |
| VAD, voix isolée, nivellement, segmentation et multi-passes | Expérimental | Comparatif apparié, détection d’activité sur un titre, fenêtres prises dans l’audio original et chronologie indépendante. Les variantes demeurent dans les bancs. |
| AEC, score et BlackBox | Partiellement validé | 12 captures closes vérifiées, 34 décisions de score reproduites. Quatre couples moniteur/témoin à cycles disjoints sont écartés. WebRTC et Speex ont été testés sur signaux synthétiques dans un graphe commun. Le temps réel de Kara garde son chemin existant. |
| Imports, Tizen, mobile et USB | Partiellement validé | Tests d’import et de reprise, client Tizen en fixture, USB sur simulateur ADB. Aucune synchronisation réelle. Le téléphone et la télévision restent à vérifier physiquement. |
| Interface et SQLite | Déployé | Tests GTK isolés, sélection conservée, comparaison des plans et des pages de l’index réel. |
| Lecture de fichiers et confidentialité | Validé sur les chemins concernés | L’extraction BlackBox refuse les liens et bornes invalides. Les 97 fichiers et 16 répertoires BlackBox inspectés avaient des droits privés. Les fixtures, bases et médias restent dans le dossier privé. |

La suite large a exécuté 25 fichiers de tests Python et JavaScript, sans échec de fichier. Les 255 cas Python rapportés incluent un test GTK ignoré dans la suite générale et passé dans un environnement Sway et D-Bus isolé. Le lot GTK d’import a passé 51 tests et la simulation USB un test. Les deux sources C17 concernées ont passé la compilation de syntaxe sous avertissements stricts. Les lots ultérieurs ont été revérifiés par les tests ciblés de fournisseurs, de gouverneur, de persistance nocturne et d’audition, puis par le contrôle du service réel. L’AEC et l’écoute sont couverts par des mesures hors ligne ou par un puits audio virtuel. La partie physique reste à qualifier.

La découverte de sources conserve l’isolation de Codex et l’option automatique désactivée. Les essais d’alignement et de séparation restent locaux. Les fournisseurs documentés et leurs conditions figurent dans le relevé d’exploitation.

## Contrôle final

Le contrôle final rapporte 71 cas Python réussis et un cas GTK ignoré dans ce passage, déjà réussi dans le test GTK isolé. La fixture Tizen, les deux contrôles C17 stricts, la vérification systemd et Git ont réussi. La salle est en attente, la lecture est en pause et aucune synchronisation USB n’était active au relevé. Parole est visible sur le bureau. Le PID unique correspond à celui de l’unité systemd et de D-Bus. Le compteur de redémarrages automatiques vaut zéro et le journal de l’unité ne contient aucune entrée au niveau erreur depuis le déploiement final. L’inhibiteur Parole est armé et le profil TLP est revenu à Performance.

## Commits locaux

| Hash | Lot | Validation ou preuve |
| --- | --- | --- |
| `2835ab4` | fix(parole): éviter Demucs pour les brouillons sur le mix | File persistante, distinction mix et préparation Kara, tests de nuit. |
| `204af1a` | fix(ressources): consigner la cause des suspensions mémoire | Gouverneur, PSI, réserve et arrêt des processus enfants. |
| `8114321` | fix(blackbox): borner la lecture des fenêtres de rejeu | Fenêtres de rejeu, bornes et liens symboliques. |
| `61e409a` | fix(console): conserver les diagnostics de ressources au redémarrage | Relecture du diagnostic structuré après redémarrage. |
| `2dcbcb9` | feat(lyrics): réutiliser les références sur preuve PCM | Éditions distinctes, égalité PCM, provenance et révision des références. |
| `96875c2` | perf(ui): préserver les lignes stables de la bibliothèque | Fixture GTK isolée, identité des lignes et sélection. |
| `bfa60ab` | perf(sqlite): indexer l’ordre de la bibliothèque Parole | Parité du tri et lectures appariées de l’index réel copié. |
| `4e7cfd7` | feat(lexique): intégrer les corrections validées et dédupliquer les éditions | Reçus de corrections, exclusion de l’évaluation et déduplication. |
| `4072868` | feat(sources): mesurer les latences et les refus d’édition | Fenêtre de latences, p95 et motifs d’édition en fixture. |
| `6b97f49` | feat(transcription): signaler les passages à vérifier dans Parole | Rangs d’incertitude, borne des listes et relecture GTK. |
| `d80e97b` | test(transcription): comparer les entrées vocales avec reprise et mesures mémoire | Reprise par étapes, égalité des références et exécution réelle des 50 cas. |
| `d1301f1` | test(blackbox): qualifier le rejeu AEC sur une horloge commune | Fixture d’alignement AEC et rejeu hors ligne de 12 captures closes. |
| `c5f1a37` | docs(omega): consigner le redémarrage et le débit réel de Parole | Observation de production et relevé de la première heure. |
| `e11c804` | test(chant): comparer l’activité vocale aux annotations indépendantes | Repères vocaux indépendants et comparaison Silero/RMS. |
| `f2aa5a0` | docs(chant): mesurer Turbo Q5 sur une chanson entière | Chanson espagnole entière, repères et profil Q5. |
| `1368bef` | test(alignement): mesurer le recalage sur des blocs tenus à part | Blocs d’alignement tenus à part, départ et fin séparés. |
| `8d805ea` | fix(sources): respecter les refus d’accès des fournisseurs de paroles | Fixtures HTTP 403/429 et respect du délai fournisseur. |
| `e048f2d` | feat(parole): écouter les passages incertains des brouillons | Tests d’écoute, fixture GTK et flux PipeWire virtuel. |
| `8d213c6` | docs(omega): relever le second déploiement et les fournisseurs actifs | Second déploiement, reprise des files et audit fournisseurs. |
| `75efede` | fix(maintenance): traiter les références locales sans quota réseau | File nocturne à quotas épuisés, borne de la requête SQLite. |
| `24e499b` | docs(omega): préciser les limites mémoire et le rendement local | Profil mémoire des phases et audit des pistes et des LRC. |
| `347112e` | fix(service): attendre le propriétaire D-Bus avant la réouverture | Prise D-Bus retardée en test, redémarrage réel et unité vérifiée. |
| `3b11c2d` | docs(omega): relever la seconde heure et la fiabilité du redémarrage | Comptage horaire réel et observation après redémarrage. |
| `f3c3257` | docs(chant): consigner le comparatif intégral des cinquante extraits | Comparatif complet, références figées et bootstrap apparié. |
| `644b0a6` | docs(chant): qualifier les chansons entières en anglais et en français | Chansons anglaise et française, repères indépendants. |
| `02153d9` | docs(chant): mesurer un titre aux paroles superposées | Titre aux paroles superposées, repères et couverture. |
| `77e68d3` | docs(chant): mesurer les fenêtres Demucs courtes et les langues alternées | Fenêtres Demucs de 5 s et témoin trilingue construit. |
| `993b243` | docs(chant): mesurer le coût de Turbo Q5 et la reprise d’un brouillon | Neuf paires Q5/F16 et nouveau processus relisant le reçu de brouillon. |

## Limites et suite

1. Élargir la qualification Demucs au seuil de production sur plusieurs chansons complètes, avec phase et pression d’admission mesurées. Le cache permet de distinguer ce coût de celui de Turbo.
2. Évaluer la sélection de la voix sur des genres et enregistrements tenus à part. Le bénéfice observé en reggae sur quatre extraits et sur certains titres entiers demande confirmation avant une décision de production.
3. Améliorer l’alignement avec une estimation acoustique du décalage qui ne consulte pas les repères d’évaluation. Mesurer séparément le début, la fin et la couverture. Les voix multiples, l’alternance de langue naturelle et le rap rapide demandent des annotations supplémentaires.
4. Mesurer le rendement des sources pendant la prochaine fenêtre avec des créneaux disponibles. Déployer une extension de l’identité acoustique uniquement sur preuve suffisante. Un LRC doit conserver la même chronologie.
5. Compléter la validation manuelle par la comparaison d’hypothèses, la confirmation par ligne et la relance localisée. Réserver l’AEC et le score aux essais physiques courts, avec les autorisations de capture déjà prévues et l’USB réel à une session dédiée.

Les objectifs exploratoires de 25 % et 15 % de WER restent ouverts sur le corpus complet. Les relevés horaires ne montrent pas de rendement des sources avec des quotas disponibles après déploiement. Le contrôle confirmant une régression ou une amélioration physique du retour microphone reste à faire. Les tâches restantes, leurs dépendances et les résultats négatifs sont documentés dans les deux relevés de qualification et d’exploitation.

## Retour arrière

Les sauvegardes SQLite cohérentes et privées de la collection, de l’index et des files sont listées dans le relevé d’exploitation. Elles ont passé `quick_check`. Un retour logiciel ciblé doit garder la base courante lorsqu’elle est intacte. Pour un changement d’unité, recharger la définition utilisateur et attendre le nom D-Bus avant d’ouvrir Parole. Pour une restauration SQLite après corruption confirmée, préserver l’état courant, arrêter les propriétaires de la base puis suivre la procédure de restauration. [Sauvegardes et procédure](exploitation.md#sauvegardes-et-retour-arrière)
