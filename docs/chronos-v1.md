# CHRONOS dans Kara

## Pendant une partie

Choisir un parcours dans **Minutage** avant de préparer la sélection. Huang
utilise Open-Unmix HQ et peut solliciter HTDemucs-FT si les indices phonétiques
signalent un passage difficile. Wav2Vec2 reprend le chemin phonétique du rap
avec une voix HTDemucs. L’option Huang / HTDemucs-FT demande cette vue
explicitement. La sélection est indépendante du genre déclaré.

La préparation utilise le LRC voisin ou une référence déjà obtenue pour
l’enregistrement. En leur absence, elle peut rechercher un LRC ou exploiter
un texte local, un tag ou un brouillon Turbo privé. Le résultat garde le
texte source et est associé au fichier et à ses fichiers voisins. Les
résultats acoustiques conservent aussi l’empreinte du PCM. Huang peut
s’appuyer sur son contraste phonétique lorsqu’une demande de partie
dispose d’un LRC exploitable. Les repères du LRC sont réservés à
l’affichage. Les mots et lignes sans repère acoustique corroboré gardent
un temps absent dans le résultat CHRONOS. Lorsque le calage CHRONOS est
partiel et que les deux versions ont le même texte dans le même ordre,
l’affichage complète les débuts de ligne avec le LRC. Chaque début
CHRONOS retenu reste entre les milieux qui le séparent des lignes source
voisines. BlackBox conserve les résultats acoustiques et le défilement
effectif séparément. Quand CHRONOS ne fournit aucun début de ligne
exploitable, Kara utilise les débuts du LRC ou du brouillon minuté.

Une préparation avec un LRC exploitable réutilise une référence de
notation Turbo valide si elle existe. **Relancer l’analyse Turbo** ou
réessayer la préparation force son calcul. Le score conserve ses seuils et
sa référence historique. Si seul le minutage est disponible, choisir
**Autoriser le chant sans note**, puis **Chanter sans note**. Le morceau
peut alors utiliser le mix original et la partie n’engage aucun calcul de
score. Le menu **Minutage historique** sélectionne le défilement historique.

Les calculs CHRONOS réutilisent le superviseur Parole et le verrou local du
moteur. Une validation explicite de la partie autorise CHRONOS avec
Strawberry en lecture. La préparation hors partie reste soumise à la
priorité du lecteur. Les protections secteur, température, PSI, RAM et
disque s’appliquent à l’admission et pendant le calcul. Le groupe de
calcul est limité à 8 Gio et à deux cœurs. Les vues vocales temporaires
sont supprimées à la fin du travail. Les reçus privés de minutage
partagent un plafond de 128 Mio.

## BlackBox Studio

Pendant la préparation, la capture de diagnostic enregistre le message
du moteur, l’étape, le code de sortie disponible et le motif de protection
éventuel. Un refus avant lancement laisse le code vide. Le journal conserve
aussi le code final du processus de préparation. Le masquage des secrets
BlackBox s’applique aux diagnostics.

Démarrer une capture avant le tour. Le mode vocal et le consentement du
microphone restent ceux de BlackBox V3. Dans **Rapport et rejeu**, sélectionner
**Référence des paroles** pour consulter ensemble les paroles originales,
les repères historiques et expérimentaux, les modèles et leurs empreintes,
les refus, le moteur de vérification et les indices par mot. Un tour avec
paroles générées conserve leur provenance et leur niveau de fiabilité dans
la capture consentie, avec l’état du tour sans note.

Dans **Chronologie**, choisir le tour et afficher un intervalle. Studio
projette les repères musicaux entre deux ancres de position Strawberry
continues. Les sauts et les zones sans ancres sont laissés vides. La
musique et la voix guide sont relues si leurs fichiers locaux correspondent
toujours aux empreintes. L’enveloppe de la voix isolée par CHRONOS est
conservée sur son horloge d’échantillonnage même après suppression du WAV.
Les pistes du joueur apparaissent selon le consentement et la couverture
effective. Les événements TV, PC, microphone et notation restent dans le
journal V3. Cette chronologie logicielle conserve l’incertitude de délai
physique entre lecture, microphone et affichage.

**Recalculer CHRONOS hors ligne** utilise l’enregistrement original vérifié par
empreinte. Studio présente les écarts par mot et ajoute une piste de repères
à la chronologie. Les repères d’origine restent dans la capture. Le score
expérimental réévalue les fenêtres de reconnaissance enregistrées avec les
nouveaux débuts admissibles. Les mots incertains n’entrent pas dans son
dénominateur de mots évaluables. Le reçu indique la couverture et la note
historique séparément. Les fenêtres restent celles du tour enregistré.
Un texte différent ou une fenêtre absente peuvent limiter la comparaison.
Les observations du joueur gardent leur statut de reconnaissance machine.

Pour les commandes locales, charger au préalable l’environnement du service.

```sh
set -a
. .runtime/chronos-local.env
set +a

bin/kara-blackbox lyrics-reference SESSION --turn TOUR
bin/kara-blackbox chronos-replay SESSION --turn TOUR --chronos-engine wav2vec2 --output ./comparaison.json
bin/kara-blackbox chronos-edition SESSION --turn TOUR --alternate-audio /chemin/autre-version.flac --output ./edition.json
```

Le comparateur d’éditions rapporte les fenêtres acoustiques distinctives,
les deux empreintes, les durées et la carte affine dans la région confirmée
lorsqu’elle existe. Il conserve les deux enregistrements et les paroles.
Si l’enregistrement original a déménagé, `--original` accepte un chemin
explicite sous la même empreinte. Les sorties de rejeu sont hors de
l’inventaire BlackBox et publiées sans remplacement.

## Installation locale et retour arrière

Le service local utilise un drop-in utilisateur dédié à cette expérimentation.
Son fichier d’environnement pointe vers les poids CHRONOS préexistants,
le Python du runtime Demucs et les dépendances locales Python 3.12.
Les poids et les dépendances tierces ne font pas partie de ce lot Git.
Pour revenir à la version précédente, pendant que la salle est inactive,
désactiver uniquement le drop-in CHRONOS et redémarrer Kara. Le lanceur
historique et les dossiers des captures et résultats restent utilisables.

```sh
chronos_dropin="$HOME/.config/systemd/user/kara.service.d/90-chronos-v1.conf"
mv -- "$chronos_dropin" "$chronos_dropin.disabled"
systemctl --user daemon-reload
systemctl --user restart kara
```

Le dépôt amont de Huang affiche MIT pour le code. Le checkpoint est
proposé par lien externe dans le README. La documentation consultée
n’explicite pas séparément les droits de redistribution de ce fichier.
Le déploiement expérimental utilise exclusivement le checkpoint local
épinglé par SHA-256. [Projet Huang et Benetos](https://github.com/jhuang448/LyricsAlignment-Multilingual)

## Contrôles de l’installation locale du 27 novembre 2026

Le service Kara a été basculé depuis `cda9175` pendant que le salon était
vide. L’état de la salle répond sur le processus expérimental et la fenêtre
Parole a été ouverte sous Sway. Le mode local par défaut est Huang. Les
autres parcours sont accessibles depuis le menu **Minutage**.

| Piste annotée | Parcours | Propositions à ±300 ms | Débuts corroborés à ±300 ms | Médiane des propositions |
| --- | --- | ---: | ---: | ---: |
| Crowd, rap anglais | Wav2Vec2 / HTDemucs | 518/521 | 443/521 | 28,9 ms |
| Séculaire, rap français | Wav2Vec2 / HTDemucs | 338/345 | 266/345 | 27,8 ms |
| Embers, rock anglais | Huang / HTDemucs-FT | 176/189 | 79/189 | 76,5 ms |
| Back In Time, piste scellée anglaise | Huang / UMX-HQ | 232/238 | 188/238 | 30,1 ms |

Les annotations n’entrent que dans le calcul des métriques. La colonne des
propositions implique le texte fourni. Les mesures corroborées incluent les
refus au dénominateur. Les traces sont privées dans
`.runtime/chronos-hardware-*.json` et `.runtime/replay-smoke/`.

La vérification technique couvre 119 tests ciblés, avec les tests GTK de
Studio, BlackBox V3, les chemins historiques de karaoké et de score, l’AEC,
le témoin vocal, le cache Demucs et les protections nocturnes. Un calcul rap
a déclenché la protection PSI avant l’isolation des allocations Demucs et
Wav2Vec2 dans des processus successifs. Les deux titres rap ont ensuite
terminé sous la protection. Le comparateur BlackBox a retrouvé les 31
ancres entre les versions de 96,88 s et 134,18 s de *No Sympathy*.
Le rejeu du même audio rap avec Huang a comparé 437 débuts de mots
corroborés par les deux parcours sur une capture dont la référence était
Wav2Vec2. L’empreinte du journal original est identique après le rejeu.

Les captures de vérification utilisent des événements de joueur
synthétiques. Une partie consentie avec un joueur reste nécessaire pour
qualifier la capture microphone en conditions réelles, la justesse
volontairement fausse et les écarts physiques de lecture ou d’affichage.

## Qualification de la préparation Wejdene

Sur la capture `session-20261127T172801-88224449cb28`, LRCLIB avait fourni
37 lignes Enhanced LRC. Turbo a échoué à 145,96 s après le raffinement
vocal. Le journal de cette version ne contient pas le diagnostic du
moteur. CHRONOS a ensuite été refusé parce qu’un flux jouait pendant
cette préparation hors partie.

La vérification locale a réutilisé le fichier audio réel et le LRC de
la capture, avec un flux PulseAudio silencieux actif. La demande hors
partie a conservé le refus de lecture et les 37 repères LRC. La demande
de partie a pris 9,7 s via le chemin GTK. Le défilement a retenu
11 débuts corroborés par Huang et 26 repères du LRC. Le contrôle du tour
a vérifié l’ouverture du mix original et l’absence de notation avec
un adaptateur de lecture de test. Il ne mesure pas le rendu physique
sur écran ou au microphone.
