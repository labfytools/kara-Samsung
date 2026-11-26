# Première partie laboratoire

## Prévol

Lancer le contrôle hors ligne avant de brancher les sources de test. La durée correspond au temps de jeu effectif. `--witness` ajoute son coût au calcul.

```sh
bin/kara-blackbox diagnose --hours 2
bin/kara-blackbox diagnose --hours 4 --witness
```

La commande vérifie le chemin du modèle Turbo, le moteur SPA s’il est installé, le service, l’espace, le plafond, la reprise d’un enregistreur de fixture et les quatre cas négatifs du score avec un témoin positif. Le code de sortie 2 indique un contrôle logiciel bloquant. On peut fournir les mêmes options locales CTC que dans le comparatif. Les contrôles qui impliquent consentement, téléphone, référence de sortie, horloges et latence restent à faire pendant le protocole.

À 6 Go par heure sans témoin, prévoir 12 Go pour deux heures et 24 Go pour quatre. Le plafond de 16 Gio couvre environ 2 h 52 de jeu actif à ce débit. Avec le témoin, l’estimation est de 6,9 Go/h, soit environ 2 h 29 avec le même plafond. `--budget-bytes` permet de vérifier le scénario d’un autre plafond. L’option ne change pas le plafond réel de Kara. Les WAV et le journal consomment le budget de collecte. Les petits rapports de finalisation s’ajoutent ensuite.

1. Ouvrir **Parole → BlackBox → Capture** et vérifier espace disque, serveur microphone, sortie, références et Turbo. Prévoir environ 6 Go par heure de lecture active, plus la réserve. Préparer les morceaux et choisir le premier tour avant de lancer la phase de mesure.
2. Ouvrir le client mobile et Tizen. Recharger la page mobile pour obtenir les réglages BlackBox, marqueurs et échanges d’horloge de V3. Le client Tizen installé peut garder son protocole actuel. Désigner les participants, recueillir leur accord, puis activer **Deep Analysis** ou **Benchmark**. Le joueur confirme l’accord sur son téléphone à chaque activation de micro. Laisser le témoin décoché, ou le sélectionner avec l’accord explicite des personnes concernées.
3. Relever le dispositif, la géométrie, le profil, les volumes Kara, le codec lorsqu’il est disponible, le retour et les traitements externes visibles. Stabiliser la pièce et garder les volumes système et EasyEffects sous contrôle manuel. À chaque changement de dispositif, codec, sortie, position du téléphone ou profil de traitement, consigner une nouvelle configuration et refaire la calibration pertinente.
4. Commencer par la sortie directe. Avec le retour coupé et un volume modéré choisi sur place, lancer explicitement **Calibration complète** si cette mesure est souhaitée. Utiliser **Annuler** en cas de gêne, saturation ou aléa. Le mode **Calibration** de BlackBox sélectionne les pistes de contrôle et attend un tour consenti. La sonde est déclenchée par l’outil de calibration séparé. Conserver son résultat local avec la configuration et marquer la phase. [Calibration existante](../audio-calibration.md#protocole-physique-s21fe-à-réaliser) · [Mesures de latence](../audio-voice-latency-measurement.md)
5. Faire un premier tour court, d’au moins quinze secondes, puis fermer la capture. Vérifier le SHA-256 et lire le rapport avant d’enregistrer une chanson entière. Le cycle doit contenir `MIC_RECEIVED` et `PLAYBACK_REFERENCE` ou `OUTPUT_MONITOR`, deux ancres mobiles et une plage logicielle commune continue d’au moins quatre secondes. Corriger la route, les choix de pistes ou l’accord avant une nouvelle tentative. Une paire acoustique exploitable requiert aussi une mesure physique du retard et une incertitude de recalage.

Si un délai a été mesuré avec le protocole adapté à la paire, conserver un reçu JSON local avec les champs `audio_configuration_id` et `delay_ms`. Le premier reprend `cycles[SESSION/TOUR].audio_configuration.id` du manifeste correspondant, le second le délai mesuré en millisecondes. `compare-aec --calibration RECU.json` contrôle cette empreinte avant d’utiliser le délai. Un redémarrage invalide par prudence les identifiants de sortie précédents. Garder le détail de la géométrie et des réglages matériels avec le reçu.

## Séquence à marquer

Utiliser le marqueur au début de chaque phase. Ajouter une note de fin ou une annotation d’intervalle lors de la relecture. Garder les phases longues de 15 à 30 s quand le morceau et le tempo le permettent. Indiquer la piste vocale originale, le retour, les faders et la position du téléphone dans le relevé.

| Phase | Marqueur | Exécution |
| --- | --- | --- |
| 1 | Calibration | Mesure facultative au volume modéré, configuration consignable et arrêt immédiat accessible. |
| 2 | Je ne chante pas | Micro ouvert et retour coupé. Observer le bruit de fond et les blocs effectivement reçus sur la plage la plus silencieuse possible. |
| 3 | Musique seule | Instrumental audible, guide et retour coupés, aucun chant. |
| 4 | Artiste seul | Fader musical à zéro et guide audible, aucun chant. |
| 5 | Voix parlée | Prononcer quelques phrases, guide coupé, en indiquant la position et le niveau du retour. |
| 6 | Chant normal | Chanter les mots prévus au tempo habituel, guide coupé. |
| 7 | Chant volontairement faux | Prononcer les mots correctement en changeant volontairement la hauteur. |
| 8 | Paroles volontairement incorrectes | Remplacer ou omettre des mots choisis et noter lesquels après le tour. |
| 9 | Chant avec l’artiste | Faire jouer le guide, puis chanter simultanément. Inclure une plage où le guide joue seul. |
| 10 | Changement du volume musical | Modifier le fader musical et marquer les paliers. Garder le retour stable. |
| 11 | Changement du volume micro | Modifier le retour avec les contrôles Kara et marquer les paliers. Indiquer s’il s’agit du volume de retour ou d’un gain de capture. |
| 12 | Rap rapide | Prendre un passage préparé avec repères et noter les mots vraiment prononcés. |
| 13 | Reggae | Répéter le contrôle sur le passage reggae préparé. |
| 14 | Chanson complète | Faire un tour complet, puis vérifier la couverture, le score final et le retour au salon. |

Refaire la séquence avec sortie Bluetooth. Garder géométrie et faders comparables et relever le codec. Une mesure du délai total par sortie ne sépare l’effet Bluetooth qu’avec les autres chemins tenus constants. Mesurer la latence d’affichage physique avec un témoin externe distinct si elle est nécessaire. Les reçus de révision Tizen mesurent un retour logiciel.

## Après chaque sortie

- Arrêter la collecte et ouvrir **Sessions → Vérifier l’intégrité**. Retrouver la session, les cycles, les pistes et les pertes dans **Rapport**.
- Utiliser les phases sans joueur pour évaluer le couplage avec le signal musical. Vérifier la préservation vocale sur une référence humaine indépendante et garder les essais où seul l’artiste est audible comme témoins négatifs du score.
- Comparer la chronologie, le score historique et une variante. Vérifier les mots sur le son capturé et consigner les annotations humaines. La tolérance du score d’environ une seconde et le repère musical recherché à 500 ms s’évaluent séparément.
- Conserver les données dans l’espace privé. Exporter le rapport expurgé pour transmettre des compteurs. [Commandes de rejeu](replay.md)
