# Préparation des chansons et score Kara

## Préparer une partie

Dans **Joueurs**, chaque joueur choisit un fichier du catalogue Strawberry, puis **Préparer la partie** lance le traitement du lot. Un morceau sélectionné par plusieurs joueurs ne passe qu’une fois dans ce lot. Les tours restent désactivés pendant la préparation. Parole affiche la part des mots évaluables sur les titres prêts à noter et la couverture des titres dont la référence reste insuffisante. Après un échec, on peut relancer, changer de morceau, écarter les titres en échec ou autoriser explicitement le chant sans note pour les fichiers toujours accessibles. Un tour sans note utilise la lecture ordinaire et n’ouvre pas le moteur de capture.

La fenêtre **Préparer des chansons** permet d’alimenter le même cache hors partie. **Relancer l’analyse Turbo** reprend le travail local si une référence valide existe. Pour un titre dont la référence a été refusée, cette action relance le traitement. Hors mode jeu, **Minutage** permet d’afficher le LRC d’origine pour comparer le défilement ou revenir au calage source à l’écran. Le traitement utilise les fichiers existants, sans passer par Strawberry, GStreamer, un périphérique audio ou le micro. Un LRC local lisible est prioritaire. Pour un titre sans LRC, le résolveur de Parole peut obtenir des paroles minutées à partir des métadonnées et de la durée. Cette recherche conserve sa réponse dans la préparation et laisse le fichier audio et la bibliothèque intacts.

Parole peut également transmettre une référence textuelle sans minutage à Turbo, après sa dernière recherche de LRC. Les mots sont associés aux observations en ordre monotone. Une ligne sans premier mot corroboré garde un début inconnu lors des reprises locales. La génération publie un fichier adjacent exclusivement si chaque ligne dispose d’un repère acoustique et si le manifeste satisfait les critères de notation Kara. L’écriture atomique refuse un LRC apparu entre-temps. Le manifeste conserve l’empreinte du texte d’origine.

Les nouveaux reçus peuvent enregistrer une empreinte PCM comprenant l’ordre des canaux et le taux d’échantillonnage. Elle permet la réassociation après un changement de conteneur ou de tags si les échantillons sont identiques et si le modèle et la référence restent valides. Demucs utilise la même identité de flux pour réutiliser les deux pistes avec les paramètres et le modèle correspondants.

```sh
python3 -B bin/_kara_prepare.py --backend-logs "/chemin/vers/le-morceau.opus"
```

La commande et les fenêtres GTK peuvent réutiliser une piste vocale déjà mise en cache pendant la reprise locale. L’option CLI `--separate-if-needed` autorise une nouvelle séparation quand le résultat initial est proche des seuils et qu’il reste des passages à revoir. `--force` relance la préparation et `--cache` choisit un dossier d’essai séparé.

## Flux audio et correction LRC

- FFmpeg décode la première piste audio en PCM signé 16 bits, mono à 16 kHz. Un fichier temporaire privé permet les accès avec chevauchement. Aucun changement de vitesse n’est appliqué. Turbo traite des pas de 24 s avec jusqu’à 2 s de contexte de part et d’autre. Chaque début de mot appartient à un seul pas, y compris près d’une coupure.
- Whisper.cpp emploie le préréglage DTW de large-v3-turbo, un décodage glouton à température zéro, le contexte indépendant entre fenêtres et une invite vide. Le manifeste conserve les segments, le texte observé et leur probabilité de non-parole. Les repères dérivent des positions DTW et des probabilités des jetons. Les positions DTW sont expérimentales dans ce backend. [API et préréglage de whisper.cpp](https://github.com/ggml-org/whisper.cpp/blob/master/include/whisper.h)
- Kara normalise les apostrophes, accents, élisions et variantes orthographiques pour le rapprochement. Il cherche des correspondances globales de mots, dans l’ordre. Un mot ne fournit un repère que si son appariement occupe un rang unique parmi les alignements optimaux et si la confiance des jetons atteint 0,35. Une répétition qui admet plusieurs placements reste incertaine. Pour les LRC simples aux repères inversés, la transcription peut aussi corroborer l’ordre des lignes du fichier.
- Le début de ligne provient du premier mot corroboré par les autres mots de cette ligne. Un timestamp déjà à 75 ms ou moins du repère obtenu peut être conservé pour le défilement. Un repère incertain conserve le timestamp source si la chronologie le permet. Si un placement intermédiaire est nécessaire pour l’affichage, il est étiqueté comme tel. Un début de ligne incertain et un mot sans repère se distinguent dans le manifeste.
- La validation exige une ligne temporelle cohérente, au moins 70 % de mots repérés, 70 % de débuts de ligne repérés et 48 % de concordance lexicale du côté de la transcription initiale. Ces seuils contrôlent la publication et restent des heuristiques.
- Chaque mot garde son identifiant dans la ligne, son texte original, son texte normalisé, ses limites de caractères, son début, sa fin éventuelle, sa confiance et la provenance des deux limites temporelles. `next_token` et `segment_end` désignent des bornes estimées. Une fin éloignée ou ambiguë demeure vide. Une note tenue peut occuper plus de 500 ms. Le LRC exporté code les lignes et le manifeste contient les repères individuels nécessaires au score.

## Reprise locale et consolidation

La passe sur le morceau complet reste le point de départ pour tout fichier sans référence vérifiée. Une génération existante fournit directement ses repères. Kara cherche les mots non résolus et encadre leur recherche par les mots déjà repérés. Sur un long trou, le LRC peut servir à placer la fenêtre de recherche. Seul un début issu du décodage DTW est retenu comme repère de mot.

Deux fenêtres qui se chevauchent sont décodées dans la langue connue du morceau, avec une invite vide et trois candidats en recherche en faisceau. Une reprise dédiée en mode glouton donne davantage de contexte aux débuts de ligne isolés que le reste de la phrase contredit. Kara exige un mot de voisinage retrouvé, un appariement ordonné unique dans chaque fenêtre, une confiance d’au moins 0,55 sur les deux observations, d’au moins 0,70 sur l’une, et des débuts à 300 ms ou moins l’un de l’autre. Les autres repères gardent leur heure. Pour un début isolé, le manifeste garde aussi l’heure écartée et le motif. Le premier début observé est conservé avec l’autre observation dans les preuves. Un mot aperçu dans une seule fenêtre peut conserver une proposition marquée incertaine, sans repère de notation.

La reprise traite jusqu’à 28 portions du mix et jusqu’à 6 portions de piste vocale Demucs, avec deux décodages par portion. Elle s’arrête quand les fenêtres prévues sont épuisées ou que la couverture de 95 % est atteinte. Les mots issus de Demucs sont intégrés un par un et identifiés comme tels. [Article HT Demucs](https://arxiv.org/abs/2211.08553)

Pour un texte sans minutage, la couverture de 95 % des mots permet de passer la reprise initiale uniquement lorsque chaque début de ligne est déjà établi. Un début manquant reste candidat aux reprises locales, avec les mêmes fenêtres bornées et les mêmes preuves acoustiques exigées à la publication.

Un refus Vulkan strict peut conserver les observations de première passe dans son reçu privé. Lors d’une nouvelle tentative explicite sur le même fichier audio avec une référence de paroles, Kara les réutilise si les empreintes de l’audio et du modèle, le profil du décodeur et la langue demandée concordent. L’alignement, les reprises locales et la décision de publication s’exécutent sur le nouveau texte. Le reçu insuffisant demeure exclu du score. Une relance forcée, un reçu ancien ou invalide, ou un changement de modèle déclenche une nouvelle transcription.

Un véritable alignement forcé imposerait la suite de phonèmes ou de caractères du texte à un décodeur acoustique. WhisperX l’illustre avec un modèle phonémique sur la parole. Un travail de l’Ircam teste une approche CTC sur le chant. Le parc logiciel examiné ne contenait pas les poids acoustiques d’un tel aligneur. L’intégration reste conditionnée à un essai sur le corpus annoté de Kara, avec mesure de l’erreur temporelle, de la couverture et du coût. Deux observations du même Turbo corroborent un placement local, avec des erreurs potentiellement corrélées. [WhisperX, Interspeech 2023](https://www.isca-archive.org/interspeech_2023/bain23_interspeech.pdf) [Alignement CTC sur le chant, Ircam](https://doi.org/10.3390/app13031854)

Le Vivobook utilise le backend Vulkan de la Radeon 780M avec quatre fils hôtes. Le modèle GGML large-v3-turbo installé a été identifié comme F16 (`ftype=1`), avec quatre couches de décodeur et 100 langues déclarées par whisper.cpp. `KARA_TURBO_DEVICE=cpu` sélectionne le profil CPU à huit fils. Le même contexte sert aux titres successifs d’un lot. Une séparation Demucs nouvellement requise libère le contexte pour borner la mémoire. Pendant chaque tour, le moteur vocal garde le modèle chargé. Le processus qui prépare les morceaux et celui qui capture la voix ont leur propre durée de vie.

Sur le même extrait de 25 s, le traitement après chargement a pris 1,91 s en Vulkan et 10,79 s sur CPU. Le backend Vulkan a déclaré 1 624 Mo de poids et environ 397 Mo de tampons de calcul. Sur la fixture parlée de 11 s, le processus natif a atteint 199 Mio de RSS visible par Linux. Les allocations Vulkan sont des chiffres distincts du RSS. Le chemin de reconnaissance du joueur a produit 22 mots en 1,87 s sur cette fixture, avec un chargement de 0,96 s. Le silence numérique et la copie exacte du fichier d’accompagnement ont produit zéro mot créditable.

## Cache

Le dossier par titre sous `${XDG_STATE_HOME:-~/.local/state}/kara/prepared` contient un `prepared.json` et un LRC dont le nom est son SHA-256. Le manifeste rassemble les empreintes de l’audio, des paroles et du modèle, le profil du backend, la méthode d’alignement, la date, les segments et le rapport. Un verrou par titre protège la publication. Le LRC est écrit et synchronisé avant la publication atomique du manifeste. Une interruption entre les deux laisse un LRC sans manifeste et le titre reste à préparer.

Pour la préparation nocturne, Parole vérifie les caches existants et passe les candidats de paroles au même moteur. Un LRC utilisable est corrigé dans le cache Kara. Si le minutage original est inexploitable ou échoue au contrôle acoustique, une référence de secours vérifiée peut être utilisée, liée à la version exacte du LRC original conservé. Après une dernière recherche classique, un texte de référence reste utilisable même si un fournisseur est indisponible. Il ne permet de générer un fichier que si le début de chaque ligne a été observé. La recherche préalable fournit des candidats sans écrire de tags ni écraser de LRC. Le décodeur confirme l’initialisation effective de Vulkan avant une nouvelle analyse automatique. Une correction de ligne fondée uniquement sur une interpolation d’affichage empêche la validation nocturne.

À l’entrée en préparation, Kara vérifie les empreintes et la préférence de décodage. Un cache déjà consolidé ne déclenche aucun chargement du modèle. Un cache validé antérieur peut être enrichi sans retranscrire le morceau entier, avec conservation de l’identité du moteur de première passe. Le même manifeste peut enregistrer un résultat insuffisant, avec les empreintes et le rapport, sans référence utilisable pour la note. Une sélection suivante réutilise ce constat tant que les données restent identiques. L’affichage réutilise les références tant que les empreintes de métadonnées du fichier, du modèle et des paroles correspondent. Les anciennes corrections de Parole restent utilisables pour l’affichage standard lorsque le cache Turbo est absent. La préparation de partie exige sa propre validation.

## Horloge et score

Le repère commun est la position musicale de Strawberry, échantillonnée via MPRIS et rattachée à l’horloge monotone Linux. Le signal `Seeked`, les variations de vitesse et les pauses ouvrent une nouvelle portion continue de chronologie. Le score compare les débuts DTW du joueur à ceux du morceau à partir de la position des échantillons. Le délai d’inférence n’intervient pas dans cette conversion. Le curseur manuel de Parole reste un réglage d’affichage hors mode jeu. La capture conserve la compensation de tampon de 75 ms configurée par le chemin existant. Le décalage acoustique réel des sorties et des micros reste à mesurer. Les téléphones du salon conservent leur rôle de sélection et leur horloge ne sert pas à dater la voix sur le PC.

Pour un tour noté, Kara utilise l’instrumental isolé déjà pris en charge par Parole. Chaque fenêtre doit disposer d’une capture continue et d’une provenance instrumentale. La garde compare le PCM capturé à l’instrumental et au mix d’origine. Elle combine la corrélation directe avec un ajustement par filtre court sur une partie de la fenêtre, puis vérifie l’ajustement sur le reste. Un signal bien expliqué par le morceau rejoué rend la fenêtre non évaluable. Le silence numérique est filtré avant le décodeur, puis les débuts de mots doivent être soutenus par le signal et le seuil de confiance. La propagation acoustique et les résidus de voix restent à qualifier sur le matériel.

Turbo reçoit le signal et la langue avec une invite vide. Le jeu décode en glouton sur des fenêtres par ligne. Le comparateur reçoit séparément les paroles attendues. Il aligne les mots avec insertions, omissions et substitutions, et rapproche les occurrences répétées de leur repère temporel. La tolérance de ±1 s inclut les deux bornes. `KARA_SCORE_TOLERANCE` permet de la régler, en secondes. La hauteur de la voix n’entre dans aucun calcul. Une note est publiée à partir de 70 % de tous les mots écrits évalués. Elle rapporte les mots corrects et dans la fenêtre aux mots cibles effectivement évalués. Les insertions restent visibles dans les diagnostics. Une référence sans repère exploitable ou une reconnaissance incertaine réduit la couverture. L’interface affiche la couverture du tour à côté de la note et détaille celle du manifeste. [Banc de qualification du chant et protocole ADB](kara-qualification-chant.md)

## Validation silencieuse initiale sur le Vivobook

| Morceau | Durée | Préparation | Lignes / mots | Corrections proposées | Écart moyen / max | Couverture mots / débuts de ligne | Incertains mots / lignes | État |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Noche de Paz — Myriam Hernández | 3:03 | 26,72 s | 14 / 60 | 12 | 1,247 / 2,150 s | 93,3 % / 85,7 % | 4 / 2 | Prêt |
| Nuestra Canción De Navidad — Myriam Hernández | 3:36 | 19,53 s | 42 / 240 | 38 | 0,572 / 1,200 s | 94,6 % / 90,5 % | 13 / 4 | Prêt |
| Une princesse est morte — KDD | 4:18 | 59,25 s | 76 / 723 | 44 | 0,167 / 0,590 s | 81,1 % / 72,4 % | 137 / 21 | Prêt après Demucs |
| Le geste — KDD | 4:11 | 30,86 s | 69 / 666 | 54 | 1,009 / 6,760 s | 82,1 % / 79,7 % | 119 / 14 | Prêt |
| Baby Don’t Cry — 2Pac | 4:22 | 137,98 s | 97 / 934 | 45 | 0,187 / 1,890 s | 70,6 % / 66,0 % | 275 / 33 | Refusé |

Exemples issus des LRC validés, en secondes depuis le début du fichier.

| Morceau | Ligne | LRC d’origine | LRC préparé |
|---|---:|---:|---:|
| Noche de Paz | 2 | 29,470 | 30,700 |
| Noche de Paz | 3 | 33,370 | 35,520 |
| Nuestra Canción De Navidad | 1 | 10,940 | 11,240 |
| Une princesse est morte | 2 | 24,450 | 24,560 |
| Le geste | 3 | 9,910 | 10,940 |

Les écarts absolus portent sur les débuts de ligne corroborés. Ils décrivent le déplacement proposé par Turbo. La précision acoustique demande des annotations indépendantes. Pour le titre de 2Pac, la piste vocale a abaissé la couverture à 53,4 % des mots et 45,4 % des débuts de ligne. Le cache contient uniquement les quatre préparations validées. Leur rechargement a pris 0,95 s pour le premier titre, empreinte du modèle comprise, puis 4 à 9 ms par titre. Le journal natif est resté vide pendant ce rechargement.

## Validation silencieuse de la reprise multipasse

| Morceau | Mots repérés avant → après | Débuts de ligne avant → après | Mots incertains avant → après | Lignes incertaines avant → après | Portions mix + voix | Temps de la séance |
|---|---:|---:|---:|---:|---:|---:|
| Baby Don’t Cry — 2Pac | 70,6 → 78,7 % | 66,0 → 78,4 % | 275 → 199 | 33 → 21 | 28 + 6 | 160,09 s |
| Une princesse est morte — KDD | 81,1 → 88,0 % | 72,4 → 84,2 % | 137 → 87 | 21 → 12 | 22 + 6 | 109,78 s |
| Le geste — KDD | 82,1 → 85,7 % | 79,7 → 79,7 % | 119 → 95 | 14 → 14 | 21 + 0 | 89,89 s |
| Noche de Paz — Myriam Hernández | 93,3 → 95,0 % | 85,7 → 92,9 % | 4 → 3 | 2 → 1 | 2 + 0 | 6,67 s |
| Nuestra Canción De Navidad — Myriam Hernández | 94,6 → 95,0 % | 90,5 → 92,9 % | 13 → 12 | 4 → 3 | 1 + 0 | 3,76 s |

Les cinq références sont validées. Pour les quatre titres déjà en cache, les durées sont celles de la reprise locale. Pour *Baby Don’t Cry*, le total comprend 25,92 s pour la transcription du mix entier et 134,12 s pour la reprise. Ses 659 repères initiaux sont identiques à ceux de la première passe contrôlée. La reprise ajoute 73 mots du mix et 3 de la piste vocale déjà disponible. Sur *Une princesse est morte*, les 50 ajouts proviennent du mix et aucun des essais supplémentaires sur la piste vocale n’a été retenu.

Sur quatre portions difficiles de *Baby Don’t Cry*, avec deux fenêtres par portion, le glouton a récupéré 9 repères en 13,91 s. Le contexte audio raccourci en a récupéré 7 en 7,07 s. Le faisceau de trois candidats a récupéré 43 repères en 15,14 s. Le pic natif RSS Linux de ce banc est resté entre 199 et 200 Mio pour les trois variantes. Le lot complet est resté à environ 199 Mio de pic natif visible, avec 1 624 Mo de poids déclarés côté Vulkan. Le RSS natif correspond au maximum du processus partagé, et les mesures historiques par morceau ne contenaient pas de pic comparable.

Pour la ligne 53 de *Le geste*, le premier mot se trouvait à 157,800 s dans l’ancienne préparation et le suivant à 165,780 s. Les fenêtres 154–170 s et 163–170 s ont maintenant toutes deux fourni 165,560 s pour le début. Le LRC source indique 164,560 s. Le décalage de cette ligne est donc ramené de −6,760 s à +1,000 s et l’écart maximal du morceau est de 1,840 s. Le manifeste conserve le repère écarté et les deux observations retenues.

Le rechargement des cinq titres a pris 0,98 s pour le premier et 3 à 9 ms pour les suivants. Il a utilisé les manifestes sans lancer Whisper. Les SHA-256 des cinq fichiers audio et des cinq LRC sources sont restés identiques. Chaque portion du tableau a donné lieu à deux décodages distincts. Pour *Le geste*, le compte inclut la vérification finale d’une seule portion.

## Prochaine séance avec voix

Le [protocole ADB de six prises](kara-qualification-chant.md#prochaine-séance-réelle-pilotée-depuis-codex) utilise les mêmes lignes pour la voix parlée, chantée, volontairement fausse, retardée, accompagnée et le témoin silencieux. Les annotations indépendantes ci-dessous servent à mesurer la précision temporelle du système.

## Protocole indépendant pour la précision à 500 ms

1. Figer les empreintes des cinq fichiers et tirer des passages avant de regarder les résultats. Inclure des débuts intacts, des refrains répétés, des mots rapides, des notes tenues, des paroles sur accompagnement dense et des passages incertains. Inclure systématiquement la ligne 53 de *Le geste* et ses voisines dans le lot d’audit.
2. Donner à deux annotateurs les paroles et le fichier dans un éditeur qui affiche le temps depuis le premier échantillon. Masquer les offsets du LRC et les sorties Turbo. Pour chaque occurrence, relever le premier phonème audible, le repère de fin si discernable, l’identité de la ligne et l’incertitude. Une contraction couvrant plusieurs unités normalisées partage la même annotation acoustique. Relever un mot absent ou inaudible sans lui donner de date.
3. Résoudre les désaccords de plus de 200 ms et les désaccords d’occurrence au moyen d’une troisième écoute ciblée. Garder l’incertitude restante. Figer les annotations avant de les rapprocher des variantes.
4. Comparer sur le même échantillon la première passe, la reprise locale et, si disponible, un aligneur phonémique candidat. Rapporter l’erreur médiane et le 90e percentile des débuts, la proportion des débuts annotables correctement appariés à ±500 ms, la couverture par provenance et les associations au mauvais refrain. Publier les abstentions et les effectifs de chaque sous-groupe.
