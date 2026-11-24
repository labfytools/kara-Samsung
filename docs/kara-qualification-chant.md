# Qualification silencieuse de la voix du joueur

Le banc injecte des échantillons mono signés 16 bits à 16 kHz dans le chemin du joueur en mode préparé. Il utilise le modèle Whisper large-v3-turbo, le binaire DTW, la garde de rejeu, les fenêtres du jeu et le comparateur de production. Le décodeur reçoit le PCM et la langue, avec une invite vide. Les paroles et leurs repères ne servent qu’à la comparaison après décodage et au placement des fenêtres sur l’horloge du morceau. Aucun périphérique audio, lecteur ou micro n’est ouvert.

## Reproduction

Le lot utilise les pistes Demucs déjà en cache et deux extraits déterminés avant les essais. Il prend les lignes 4–5 d’*Une princesse est morte* et 4–6 de *Baby Don’t Cry*. Les 48 mots cibles comprennent 47 débuts de référence disponibles. La fixture parlée `jfk.wav` du checkout whisper.cpp déjà installé sert de témoin. Le banc utilise l’environnement Python du moteur vocal et le filtre Rubber Band du FFmpeg existant. Il lit les fichiers et les références préparées sans publication ni séparation.

```sh
kara_test_python="$HOME/.local/share/hyprwhspr/venv/bin/python"
mkdir -p .runtime/chant/reproduction
for suite in strategy context context-holdout voice timing leak audit; do
    "$kara_test_python" -B tests/qualify-kara-singing.py "$suite" \
        --library /home/Partage/Music \
        --output ".runtime/chant/reproduction/$suite.jsonl" || break
done
```

Chaque sortie est un nouveau JSONL privé. Les journaux contiennent les empreintes des sources, du manifeste et du modèle, l’identité du binaire, la configuration, les durées, les mots observés, les timestamps et la décision du comparateur. `--device cpu` permet une exécution sur CPU. La suite `leak` conserve tous ses diagnostics et sort en erreur si un cas sans joueur a reçu un crédit. `audit` contrôle les cinq titres sans inférence.

La hauteur fixe ou variable utilise Rubber Band avec préservation des formants. Un changement de hauteur seul conserve le nombre exact d’échantillons et le début du signal. La hauteur variable combine trois transformations à −2, +2 et −1 demi-tons. Le rythme varie de ±6 %. Un intervalle de 220 à 230 ms est étiré à tempo 0,42 pour le cas de note tenue. Deux cas font varier l’enveloppe et appliquent une baisse fixe de 24 dB. Ces simulations gardent une partie de la diction, du timbre et des respirations de l’artiste. Un chanteur réel peut modifier simultanément l’articulation, la durée, le souffle et l’attaque des syllabes.

## Comparaison des décodages

Le taux d’erreur lexicale (WER) additionne substitutions, omissions et insertions sur les unités normalisées de Kara. Pour les extraits complets, la même sélection de PCM et la même marge silencieuse encadrent chaque stratégie. L’essai du jeu ligne par ligne partitionne les mots par horloge pour le WER. Le comparateur de la note traite séparément le rattachement des mots aux lignes. Les durées sont les temps muraux cumulés hors chargement, mesurés avec le profil GPU à quatre fils.

| Décodage sur l’extrait | Erreurs sur le chant (48 mots) | Temps KDD + 2Pac | Erreurs sur la parole (22 mots) | Temps parole |
|---|---:|---:|---:|---:|
| Glouton, extrait entier | 2 (4,2 %) | 3,56 s | 0 | 1,69 s |
| Faisceau 3, extrait entier | 2 (4,2 %) | 3,69 s | 0 | 1,75 s |
| Glouton, fenêtres de 3 s sans chevauchement | 5 (10,4 %) | 9,50 s | 6 | 6,13 s |
| Glouton, pas de 3 s et 0,75 s de chaque côté | 2 (4,2 %) | 9,77 s | 0 | 5,71 s |
| Faisceau 3, pas de 3 s et 0,75 s de chaque côté | 2 (4,2 %) | 10,12 s | 0 | 6,42 s |
| Glouton, pas de 6 s et 1,5 s de chaque côté | 2 (4,2 %) | 6,54 s | 0 | 3,31 s |

Le glouton et le faisceau dans les fenêtres de jeu donnent chacun 10 erreurs lexicales sur 48 mots. Le glouton donne 38 crédits en 9,34 s cumulées et le faisceau 37 crédits en 10,08 s. Le glouton reste la stratégie du joueur. La préparation du morceau utilise son propre décodage multipasse.

## Contexte testé pour le jeu

Les essais ajoutent du PCM réel en fin de fenêtre. Ils conservent sa borne de début et la tolérance de notation. Les passages de contrôle sont les lignes 9–10 de KDD et 24–25 de 2Pac. Chaque cellule donne les corrects sur les évalués.

| Passage et voix | Fenêtre initiale | +750 ms | +1,5 s |
|---|---:|---:|---:|
| KDD, voix originale | 18/18 | 18/18 | 18/18 |
| KDD, −2 demi-tons | 9/18 | 18/18 | 18/18 |
| 2Pac, voix originale | 20/29 | 20/29 | 28/28 |
| 2Pac, −2 demi-tons | 20/28 | 20/28 | 28/28 |
| KDD contrôle, voix originale | 25/27 | 27/27 | 25/25 |
| KDD contrôle, −2 demi-tons | 20/23 | 24/25 | 23/24 |
| 2Pac contrôle, voix originale | 17/18 | 18/18 | 17/17 |
| 2Pac contrôle, −2 demi-tons | 15/16 | 16/16 | 16/16 |

Avec 750 ms supplémentaires, le comparatif ci-dessus gagne 17 crédits, dont huit sur les contrôles. La revalidation des 26 variantes de voix donne cependant **494 crédits, contre 497 dans la fenêtre initiale**. Le contexte supplémentaire fait passer le couplet de KDD à −2 et −1 demi-tons de 9/18 à 18/18. Sur 2Pac, il fait passer chacune des variantes à ±1 demi-ton de 28/28 à 20/29. Dans un contrôle ciblé sur la voix originale, le faisceau avec 750 ms obtient 27/27 sur 2Pac et 18/18 sur KDD. Ce résultat couvre deux extraits sans transformation.

Le jeu conserve la fenêtre initiale et évite d’ajouter jusqu’à 750 ms d’attente systématique avant décodage. L’option `--post-roll 0.75` reproduit le candidat avec les autres suites. L’extension d’essai s’arrête à la fin du morceau et respecte la limite de 30 s. Les journaux du réglage retenu se trouvent dans `.runtime/chant/qualification/`, ceux de l’essai à 750 ms dans `.runtime/chant/candidat-750ms/`.

## Voix transformées

| Transformation | KDD corrects/évalués | KDD écart médian | 2Pac corrects/évalués | 2Pac écart médian |
|---|---:|---:|---:|---:|
| Voix originale séparée | 18/18 | 0,00 s | 20/29 | 0,00 s |
| −4 demi-tons | 17/17 | +0,02 s | 20/29 | +0,01 s |
| −2 demi-tons | 9/18 | +0,02 s | 20/28 | 0,00 s |
| −1 demi-ton | 9/18 | +0,02 s | 28/28 | +0,01 s |
| +1 demi-ton | 18/18 | 0,00 s | 28/28 | 0,00 s |
| +2 demi-tons | 12/14 | +0,02 s | 20/28 | 0,00 s |
| +4 demi-tons | 18/18 | 0,00 s | 17/27 | 0,00 s |
| Hauteur variable | 18/18 | +0,01 s | 20/28 | 0,00 s |
| Rythme +6 % | 17/18 | −0,14 s | 28/28 | −0,22 s |
| Rythme −6 % | 18/18 | +0,13 s | 28/28 | +0,24 s |
| Note prolongée | 18/18 | 0,00 s | 20/29 | 0,00 s |
| Volume variable | 18/18 | 0,00 s | 20/29 | 0,00 s |
| Volume −24 dB | 18/18 | 0,00 s | 20/29 | 0,00 s |

Les médianes portent sur les correspondances lexicales crédibles. La sensibilité à la hauteur varie selon l’extrait et le sens du décalage. Sur ces transformations, les différences de crédits viennent des mots reconnus et de leur confiance. Le score compare uniquement le mot et son début. Il ne calcule ni hauteur ni note musicale.

## Temporisation dans les conditions du jeu

La source des décalages est la même voix transposée à +2 demi-tons. Cela réduit sa ressemblance de forme d’onde avec le mix et évite de confondre un retard du joueur et un rejeu de la voix originale. L’injection décale le PCM sur l’horloge du morceau. Les repères attendus et les bords des fenêtres restent fixes. Pour la dérive, l’étirement conserve la hauteur et ajoute 1,6 s en fin d’extrait.

| Retard injecté | KDD corrects/évalués | KDD écart médian | 2Pac corrects/évalués | 2Pac écart médian |
|---|---:|---:|---:|---:|
| 0 ms | 12/14 | +0,02 s | 20/28 | 0,00 s |
| 250 ms | 17/17 | +0,26 s | 20/29 | +0,24 s |
| 500 ms | 18/18 | +0,51 s | 20/29 | +0,49 s |
| 750 ms | 14/17 | +0,76 s | 24/27 | +0,74 s |
| 1 s | 5/17 | +1,02 s | 16/25 | +1,00 s |
| 1,5 s | 0/18 | +1,50 s | 0/29 | +1,50 s |
| 2 s | 0/18 | +2,00 s | 0/28 | +2,00 s |
| Progressif | 6/15 | +1,02 s | 15/28 | +0,88 s |

Les médianes portent sur les correspondances lexicales crédibles, y compris hors délai. À 1 s injectée, 12 mots reconnus ont un début DTW au-delà de 1 s, tandis que 21 restent crédités. À 1,5 s, 29 mots sont reconnus hors délai et aucun ne rapporte de point. À 2 s, 18 mots restent reconnus hors délai. La fin de certaines lignes sort de la fenêtre réelle. Le test du comparateur injecte aussi des timestamps exacts de part et d’autre des bornes. Il accepte ±1,00000 s et rejette ±1,00025 s. Cette vérification distingue la règle de score de l’erreur du timestamp estimé par Whisper.

## Fuites numériques et faux crédits

La corrélation directe écartait déjà les copies exactes. Sur l’original seul filtré à 950 Hz et décalé de 180 ms, elle a laissé passer 29 faux crédits. La garde utilise maintenant une corrélation basse fréquence pour proposer un retard, puis ajuste un filtre court à 25 coefficients sur des demi-secondes alternées. Il faut que le filtre explique au moins 94 % de l’énergie sur les demi-secondes gardées pour la vérification. Ce rejet ne soustrait rien du signal et n’applique aucune compensation de timing au joueur.

| Entrée simulée | KDD corrects/évalués | 2Pac corrects/évalués |
|---|---:|---:|
| Silence numérique | 0/18 | 0/29 |
| Instrumental seul, exact ou filtré | 0/0 | 0/0 |
| Mix original seul, exact ou filtré | 0/0 | 0/0 |
| Voix Demucs seule | 18/18 | 20/29 |
| Voix + instrumental à −18 dB | 8/8 | 17/17 |
| Voix + instrumental à −6 ou +3 dB | 0/0 | 0/0 |
| Voix + mix original à −18 dB | 8/8 | 29/29 |
| Voix + mix original à −6 ou +3 dB | 0/0 | 0/0 |
| Voix transposée + mix original à −18 dB | 16/16 | 27/27 |
| Voix transposée + mix original à −6 dB | 13/14 | 26/27 |
| Voix transposée + mix original à +3 dB | 0/0 | 0/0 |

Les niveaux sont ceux de la musique relativement à la voix en RMS dans chaque fenêtre. Les copies filtrées comportent un passe-bas et deux bandes différentes, avec des retards de 180, 310 et 370 ms. Les 35 fenêtres sans joueur ont donné zéro crédit. Les copies de musique sont écartées de la couverture. Le silence capturé est évalué comme une absence de mots. Les symboles `0/0` désignent une fenêtre ou un extrait écarté. Lorsque la musique dépasse la voix de 3 dB, les deux extraits sont écartés, même avec une voix transposée.

La piste Demucs et le mix original partagent la même voix, ce qui entraîne des exclusions lorsque la garde observe une forte cohérence. Les variantes à +2 demi-tons décalées de 25 ms réduisent cette ressemblance. Dans les mélanges avec voix, le banc compte les crédits du jeu et ne peut attribuer chaque mot crédité à une des deux sources. L’ajout de 750 ms améliore certains mélanges et en dégrade d’autres. Sur 2Pac avec instrumental à −18 dB, les crédits passent de 17/17 à 9/17. Le mix original à −6 dB avec voix transposée progresse de 39 à 44 crédits au total. Une pièce, un traitement automatique du microphone ou un trajet Bluetooth peuvent transformer la fuite au-delà des cas numériques vérifiés.

## Dénominateur et équité

| Grandeur | Définition |
|---|---|
| Total | Mots du manifeste préparé. Une annotation LRC tokenisée ou un mot écrit à tort peut faire partie de ce total. |
| Référence utilisable | Mots du manifeste portant un début jugé exploitable par la préparation. |
| Évalués | Mots cibles ayant reçu une décision correcte, incorrecte, en avance, en retard ou manquante. |
| Corrects | Mots reconnus avec confiance et début DTW à ±1 s de la référence, bornes incluses. |
| Incorrects | Mots évalués qui ne sont pas corrects. |
| Non évalués | Différence entre total et évalués, détaillée en mots sans repère, décisions incertaines ou fenêtres écartées, et fenêtres non traitées. |

**Score** = corrects / évalués. **Couverture du tour** = évalués / total. **Couverture de référence** = utilisables / total. Le seuil de publication est une couverture réelle d’au moins 70 %. L’affichage arrondit la note au point et tronque la couverture au pourcent inférieur. Les insertions restent au diagnostic et ne modifient pas le dénominateur. Une insertion peut venir d’un mot manquant dans le LRC, d’une détection répliquée dans une fenêtre voisine ou d’une erreur lexicale.

Le test comptable fournit au comparateur tous les mots utilisables aux repères du manifeste, puis omet chaque cinquième mot utilisable. Ce contrôle de la formule est distinct de la qualification d’inférence.

| Morceau | Mots écrits | Avec repère | Sans repère | Couverture maximale affichée | Score parfait sur les mots utilisables | Un mot utilisable sur cinq omis |
|---|---:|---:|---:|---:|---:|---:|
| Une princesse est morte | 723 | 636 | 87 | 87 % | 100 % | 80 % |
| Baby Don’t Cry | 934 | 735 | 199 | 78 % | 100 % | 80 % |
| Le geste | 666 | 571 | 95 | 85 % | 100 % | 80 % |
| Noche de Paz | 60 | 57 | 3 | 95 % | 100 % | 81 % |
| Nuestra Canción De Navidad | 240 | 228 | 12 | 95 % | 100 % | 80 % |

La part des mots correctement interprétés parmi ceux évalués a la même définition pour tous les morceaux. La quantité d’observations, la difficulté et la qualité des repères diffèrent. Kara affiche la note avec la couverture et les effectifs et ne calcule pas de classement entre morceaux. Un début erroné que le manifeste considère encore exploitable peut affecter la note. Le contrôle par annotation indépendante reste nécessaire pour ce cas.

## Prochaine séance réelle pilotée depuis Codex

Prévoir six prises courtes du même extrait et quelques minutes pour les diagnostics. Le téléphone affiche les consignes via ADB. Le repère de départ reste le surlignage de Kara, lié à Strawberry. Le microphone reste celui du chemin de jeu, sur le PC ou l’entrée audio déjà sélectionnée. Le navigateur du téléphone ne capture ni ne joue de son.

1. Au début de la séance réelle, Codex identifie le téléphone autorisé avec `adb devices -l` et lance le pilote ci-dessous dans une session interactive. Sélectionner *Une princesse est morte* dans Kara, puis utiliser **Préparer la partie** pour obtenir le tour noté. Le test porte sur les lignes 4–5 autour de 31 à 36 s.
2. Garder la même entrée et le même profil audio. Pour les quatre premières prises, couper physiquement la sortie tout en conservant l’instrumental et son horloge dans Kara. Pour les deux dernières, remettre l’instrumental au même niveau physique et garder ce réglage.
3. Pour chaque consigne affichée, activer **Enregistrer le prochain tour** avec l’accord du joueur, puis cliquer sur **Chanter** dans la ligne du même joueur. Suivre le surlignage, sans sauter ni mettre en pause. Attendre le résultat des deux lignes avant de cliquer sur **Terminer le tour**, généralement à partir de 41 s de lecture pour cet extrait. Codex repère le nouveau diagnostic et avance la consigne après vérification.
4. Les six consignes, dans l’ordre, sont parlé, chanté, chanté volontairement faux, chanté avec environ 1,5 s de retard, chanté avec instrumental et joueur silencieux avec le même instrumental. Le dernier essai doit attribuer zéro crédit. Un crédit vocal sur le témoin silencieux est un échec à examiner fenêtre par fenêtre.
5. Lire les résultats des deux lignes pour chaque prise. Le détail fournit l’attendu, le mot entendu, les débuts sur l’horloge musicale, l’écart, la confiance, la source, les sorties de Whisper et les motifs d’exclusion. Pour les erreurs discutables, annoter ensuite le début audible sur le WAV local. La consigne de retard est approximative et le WAV permet de mesurer le retard réalisé.

```sh
kara_test_python="$HOME/.local/share/hyprwhspr/venv/bin/python"
kara_test_song='KDD/Résurrection/05 - Une princesse est morte.opus'
"$kara_test_python" -B tests/kara-singing-adb.py \
    --serial "$kara_android_serial" --song "$kara_test_song" --first 4 --last 5

# À répéter sur chaque JSON correspondant à un WAV du tour enregistré
"$kara_test_python" -B tests/qualify-kara-singing.py review \
    --song "$kara_test_song" --sidecar "$kara_capture_json" --first 4 --last 5 \
    --output "$kara_review_jsonl"
```

Les WAV et leurs JSON associés sont stockés localement dans `${XDG_STATE_HOME:-~/.local/state}/kara/diagnostic/`. Les résumés de `review` portent sur la plage demandée. Le comparateur vérifie l’identité de la référence et de sa génération avant de relire les observations. Le pilote ADB réserve un port de boucle locale, crée uniquement son propre `adb reverse` et le retire à la fin. Le mode `--dry-run` vérifie les lignes et les consignes sans lancer ADB. Seul ce mode a été exécuté dans la qualification silencieuse.
