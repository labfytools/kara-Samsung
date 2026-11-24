# Protection contre les répétitions du retour micro

## Diagnostic

Un **retard** décale la première restitution de la voix. Un **écho répété** reproduit la phrase plusieurs fois après l’arrêt de la voix. Le **Larsen** apparaît lorsque la boucle acoustique entretient ou amplifie certaines fréquences. Des rebonds décroissants peuvent donc être audibles avant tout sifflement.

Le téléphone exécute un seul Worklet actif en retour normal. Sa sortie locale est mise à zéro. Le navigateur garde un propriétaire de session et ferme la piste, le port du Worklet et le WebSocket à l’arrêt. Le serveur garde un verrou jusqu’à la fin du lecteur et ne remet chaque bloc retenu qu’une fois à son entrée. Les deux canaux reçoivent le même PCM simultanément pour la sortie stéréo. Le graphe PipeWire inspecté sans ouvrir le retour n’avait ni nœud Kara actif, ni module ou nœud apparent de monitoring en boucle. Cette observation concerne l’état du poste au moment de l’inspection.

Un test a reproduit une course de reconnexion. Une nouvelle demande arrivée pendant le démarrage attendait derrière l’ancienne, qui pouvait encore devenir active. Le serveur donne désormais la priorité à la dernière demande authentifiée et récupère l’ancien lecteur avant le suivant. Le navigateur conserve la séquence émise par le Worklet jusque dans le WebSocket. Un message répété ou tardif ne reçoit plus de nouvelle identité. Le serveur rejette les doublons et retours en arrière, et fixe le format après le premier bloc valide. L’ancien format Stable sans en-tête reste accepté pour une session homogène. Ce format n’a pas de séquence pour distinguer un doublon d’un bloc identique.

La **réinjection acoustique** reste l’hypothèse principale pour la phrase qui rebondit. Les tests logiciels n’ont pas produit de double lecture. La reproduction et l’origine du symptôme sur le S21 FE restent à vérifier, y compris les réflexions de la pièce et tout routage actif au moment où le problème survient.

## Traitement de retour

**Protection contre les répétitions** est activée à l’ouverture de la page et indépendante de Stable, Équilibré et Faible latence. Elle applique un gain fixe de **−9 dB** au PCM du retour, après la compression existante, avant la conversion stéréo int16. Le facteur est combiné avec le coefficient de crête de la compression. Le premier retour est plus faible et chaque nouveau passage dans la boucle subit à nouveau l’atténuation. La protection agit sur toute la voix, avec la même échelle pour la parole faible, les voix fortes et le chant soutenu. Elle ne coupe aucun passage selon le niveau et n’ajoute aucun tampon. La baisse de niveau peut rendre des sons faibles moins audibles. L’arrondi int16 reste applicable aux tout petits échantillons.

Le réglage est fixé au démarrage de chaque session pour éviter un changement brusque de gain. Le volume de retour démarre à 50 % dans la page et pour une requête sans volume. Le curseur est plafonné à 100 % quand la protection est active. Le réglage s’applique au seul nœud Kara. Pour retrouver le transfert précédent, **Couper**, décocher **Protection contre les répétitions**, puis **Activer le micro**. Le démarrage prudent s’applique aussi lorsque la protection est désactivée. **Revenir à Stable** continue de sélectionner le profil de référence indépendamment de la protection.

La valeur `−9 dB` représente le gain numérique appliqué. Elle ne mesure pas un taux d’écho supprimé. Aucun détecteur de réinjection n’est affiché. L’efficacité dépend du couplage acoustique, du volume physique et des traitements sur le téléphone. Augmenter le gain de sortie pour retrouver le niveau initial peut annuler le bénéfice. La protection réduit le risque d’une boucle durable. Avec un couplage ou un gain excessif, le bouton **Couper** reste la réponse immédiate.

La calibration complète suit son chemin réservé de 12 essais. Son Worklet reste en écoute locale, les seules émissions sont les sondes bornées du serveur et les essais vérifient le rejet de PCM micro côté WebSocket. Le réglage de protection concerne uniquement le retour normal.

## Annulation d’écho du navigateur et référence distante

Pour la capture vivante, Kara demande toujours `echoCancellation: true`, `noiseSuppression: true` et `autoGainControl: false`. Le JSON replié rapporte les valeurs de `getSettings()` lorsqu’elles sont exposées, y compris `all` et `remote-only` pour l’annulation d’écho. Une propriété absente reste inconnue. Les réglages effectifs du S21 FE sont à lire pendant l’essai. Chrome peut avoir une référence des sons rendus par le téléphone. Le PCM Kara part par WebSocket et est diffusé par Arch, sans chemin de restitution de référence dans Chrome Android. Une valeur déclarée active ne prouve donc aucune annulation de cette enceinte. [Media Capture and Streams](https://www.w3.org/TR/mediacapture-streams/#def-constraint-echoCancellation).

Une porte de bruit ou une atténuation qui se déclenche sur le seul niveau micro ne dispose pas d’information suffisante pour séparer une répétition d’une syllabe douce, d’une tenue ou d’une fin de mot. L’atténuation uniforme préserve cette forme d’onde, avec le compromis de niveau annoncé.

Un AEC distant à même niveau d’écoute demanderait une référence du moniteur de la sortie Arch sélectionnée, comprenant le retour Kara et la musique effectivement envoyée à ce périphérique. Il faudrait apparier cette référence avec la capture Android, estimer le décalage et la dérive des deux horloges, traiter les sauts de délai et réinitialiser l’état aux changements de sortie. Le moniteur de sortie ne voit pas le retard acoustique ajouté après l’encodage Bluetooth. Une référence mal alignée risque d’atténuer la voix directe, surtout lorsque la voix et ses répétitions se superposent. WebRTC Audio Processing sépare le flux de capture et le flux de restitution de référence et expose un délai entre les deux. Le module d’écho de l’autre outil Kara concerne une source PipeWire locale. Le test de retour Wi-Fi conserve la solution sans référence et sans dépendance supplémentaire. [API WebRTC Audio Processing](https://webrtc.googlesource.com/src/+/refs/heads/main/api/audio/audio_processing.h).

## Résultats logiciels

Le modèle injecte la sortie int16 dans le micro synthétique à des délais fixes de 95, 210 et 375 ms, puis avec baisse du gain de boucle et avec délai variable. Il couvre la parole faible, la voix forte, le silence, les débuts et fins de syllabes et le chant soutenu. Pour un délai fixe de 210 ms et un gain de réinjection de 0,6, la différence de niveau RMS entre protection activée et désactivée vaut −9,00 dB sur le premier retour, puis −17,75, −26,51 et −35,33 dB sur les trois répétitions. Les tests comparent aussi l’énergie des répétitions par rapport au premier retour. Ces mesures portent sur un modèle de boucle, sans réponse de pièce, traitement Android ou codec Bluetooth réel.

Le traitement n’ajoute aucun retard de signal. La mesure du CPU du noyau DSP NumPy sur ArchASP, médiane sur neuf séries de 15 000 itérations, donne 2,12 µs désactivé contre 2,10 µs activé pour 480 échantillons, et 2,42 contre 2,44 µs pour 960. Les écarts mesurés sont de l’ordre de la variabilité du test. Le délai acoustique reste à mesurer.

Les suites suivantes utilisent des fixtures générées et des lecteurs simulés. Aucun retour réel ne se déclenche pendant leur exécution.

```bash
node tests/test-kara-audio-monitor.js
node tests/test-kara-calibration-capture.js
node tests/test-kara-calibration-engine.js
(for test in tests/test-kara-*.py; do python3 "$test" || exit; done)
git diff --check
```

## Campagne sur le Galaxy S21 FE

1. Sur ArchASP, démarrer le serveur local avec `python3 -u bin/_kara_micro_test.py` et ouvrir le lien privé dans Chrome sur le S21 FE. Sélectionner les haut-parleurs analogiques du PC. Garder Strawberry en pause. Poser le téléphone à distance fixe, régler les haut-parleurs à faible volume et garder les mêmes réglages de voix et de retour, initialement 90 % et 50 %. Si le premier retour est inaudible, établir un niveau prudent avant le début de la comparaison.
2. **A · Stable, protection désactivée.** Décocher la protection avant d’activer. Dire une phrase courte puis garder plusieurs secondes de silence. Compter les répétitions nettement audibles après le premier retour. Faire une seconde passe avec voix douce, fins de mots et un passage chanté tenu. Noter l’intelligibilité, les craquements et le délai perçu. Relever si possible les réglages micro déclarés dans le diagnostic. Couper.
3. **B · Stable, protection activée.** Cocher la protection et recommencer à position, gains, phrase et passage chanté identiques. Comparer le nombre de rebonds et leur niveau par rapport au premier retour, ainsi que les fins de mots. Garder les volumes fixes pour cette comparaison. Couper.
4. **C · Équilibré, protection activée.** Refaire le même passage. Vérifier la continuité et le délai perçu. Essayer Faible latence uniquement si Équilibré reste stable. Si le son se met à boucler ou à monter lors de n’importe quelle passe, appuyer immédiatement sur **Couper**. Si le premier retour devient trop faible ou si les répétitions restent gênantes, le test ne valide pas la correction pour cette installation.
