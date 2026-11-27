# Kara V1 — salle locale et BlackBox

> Guide historique. Voir [Kara V2](kara-v2.md) pour le parcours classique et les commandes actuelles.

## Démarrer la salle

Parole et Strawberry tournent sur ArchASP. Parole garde la propriété du lecteur, des tours, de la file d’import et des traitements Turbo. La fenêtre peut être minimisée. Fermer Parole arrête sa salle. Le client Tizen conserve son appairage LAN et se reconnecte pendant une coupure. Quand un joueur rejoint ou retrouve la salle, Kara rouvre l’affichage de l’application Tizen déjà ouverte si l’écran a été fermé depuis Parole.

Lorsque le fichier `$XDG_CONFIG_HOME/kara/https.json` existe, Parole ouvre automatiquement la salle au démarrage. Sur ArchASP il contient :

```json
{"origin":"https://kara.labfytools.com:8443","lan_address":"192.168.1.63"}
```

La chaîne complète et la clé sont lues dans `$XDG_STATE_HOME/kara/https/current/` après publication par `bin/kara-https-sync pull`. Parole accepte aussi `fullchain.pem` et `privkey.pem` à la racine de ce répertoire HTTPS. Les chemins absolus `fullchain` et `key` sont facultatifs dans le fichier de configuration. Les variables `KARA_HTTPS_ORIGIN`, `KARA_HTTPS_FULLCHAIN` et `KARA_HTTPS_KEY` permettent aussi de démarrer avec une configuration ponctuelle.

| Service | Adresse sur ArchASP |
| --- | --- |
| État et QR Tizen | IPv4 LAN, TCP 8765 |
| Mobile et administration | `https://kara.labfytools.com:8443` avec le chemin de la salle donné par le QR |
| Microphone WSS | Même nom et certificat, TCP 8444 par défaut |
| Prévol silencieux | `KARA_SILENT=1` coupe les entrées audio de Kara et les commandes de lecture. La sonde WSS reste disponible. |

Parole lit les connexions depuis le sous-réseau IPv4 de son interface LAN. `lan_address` sélectionne l’adresse d’écoute lorsqu’un partage de connexion USB ajoute une route par défaut. Le DNS public annonce l’adresse privée d’ArchASP, configurée manuellement sur son interface Wi-Fi. Garder le DNS, cette adresse et le sous-réseau de la TV synchronisés si le réseau change. Les clients invités doivent pouvoir atteindre ArchASP sur le LAN.

Le schéma HTTPS du QR est indépendant du certificat de signature WGT. Le certificat actuel est émis par Let’s Encrypt pour ce seul nom et expire le **22 février 2027**. La validation DNS-01 initiale a utilisé le TXT public `_acme-challenge.kara.labfytools.com`. Le certificat est sorti d’un espace Certbot privé sur ArchServer puis a été transféré par SSH. La clé est restée dans des fichiers d’accès restreint.

Sur ArchServer, la lignée privée de Kara se trouve dans `~/.local/state/kara-acme`. Certbot utilise le greffon `dns-ovh` et attend 120 secondes avant la validation DNS-01. Le fichier `ovh-kara.ini` est en mode 0600. L’autorisation OVH permet de sonder le statut de `kara.labfytools.com` et de `labfytools.com`, de lire, créer et supprimer les enregistrements dans la zone `labfytools.com` et de rafraîchir cette zone. Le greffon crée et supprime le TXT `_acme-challenge.kara.labfytools.com`. [Let’s Encrypt DNS-01](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge) · [Greffon DNS OVH](https://certbot-dns-ovh.readthedocs.io/)

Sur ArchASP, `kara-https-renew.timer` lance une vérification quotidienne à 09 h 30 avec un délai aléatoire maximal de 20 minutes. Le timer utilisateur est persistant et rattrape une échéance manquée à la reprise du gestionnaire utilisateur. La clé SSH dédiée est limitée à l’adresse LAN d’ArchASP et aux trois commandes Kara de renouvellement et de lecture de la paire TLS. `bin/kara-https-sync` vérifie le nom, la chaîne et la correspondance de la clé avant de publier la paire atomiquement. Les nouvelles connexions HTTPS et WSS prennent la génération publiée et les connexions déjà établies gardent leur session TLS.

### Renouvellement de secours

Sur ArchASP, déclencher la vérification et consulter son état.

```sh
systemctl --user start kara-https-renew.service
systemctl --user show kara-https-renew.service -p Result -p ExecMainStatus
systemctl --user list-timers kara-https-renew.timer --all
```

Si l’accès OVH automatisé échoue, tester une autorisation de remplacement sur ArchServer avec `certbot renew --dry-run --no-random-sleep-on-renew --cert-name kara.labfytools.com --dns-ovh-credentials CHEMIN_PRIVÉ` et les chemins de la lignée privée ci-dessous. Si le certificat arrive à échéance avant le rétablissement de cet accès, ouvrir une session interactive en tant que propriétaire de la lignée sur ArchServer et lancer DNS-01 manuel.

```sh
umask 077
base="$HOME/.local/state/kara-acme"
certbot certonly --manual --preferred-challenges dns \
  --cert-name kara.labfytools.com -d kara.labfytools.com \
  --config-dir "$base/config" --work-dir "$base/work" --logs-dir "$base/log"
```

Ajouter dans OVH le TXT demandé par Certbot. Confirmer sa présence sur les serveurs DNS faisant autorité avant de poursuivre. Retirer ce seul TXT après la validation. De retour sur ArchASP, publier puis vérifier les deux ports.

```sh
bin/kara-https-sync pull --host srv-kara-renew --ssh-config "${XDG_CONFIG_HOME:-$HOME/.config}/kara/renew-ssh"
for port in 8443 8444; do
  openssl s_client -connect "kara.labfytools.com:$port" \
    -servername kara.labfytools.com -verify_hostname kara.labfytools.com \
    -verify_return_error </dev/null 2>/dev/null | openssl x509 -noout -dates -fingerprint -sha256
done
```

Après le dépannage, rétablir `dns-ovh` sur ArchServer avec `certbot reconfigure --cert-name kara.labfytools.com --dns-ovh --dns-ovh-credentials "$base/ovh-kara.ini" --dns-ovh-propagation-seconds 120` et les mêmes chemins privés. Cette commande vérifie la configuration en préproduction avant de l’enregistrer.

## Organisation et tours

Le QR est le lien public de la salle locale. Dans Parole, **Salle / TV → Code organisateur** affiche un code à quatre chiffres à saisir dans **Admin** sur le téléphone. Le code est valable cinq minutes et la salle accepte cinq erreurs avant qu’il faille en afficher un nouveau dans Parole. La session du navigateur retrouve son rôle et ses choix après actualisation de la page. Les joueurs et leurs sélections mobiles vivent dans la salle ouverte.

Les joueurs recherchent les titres par artiste, album ou titre, ajoutent leurs chansons, les ordonnent et se déclarent prêts. Dans **Importations**, la recherche YouTube Music propose des artistes dès les premières lettres. Choisir un artiste pour afficher ses albums, puis ouvrir l’édition voulue pour voir sa pochette et ses pistes. On peut choisir la piste à ajouter à sa sélection une fois l’album disponible. **Tout télécharger** prépare l’import des albums affichés après confirmation des droits. Pour l’organisateur, le lot entre directement dans la file de Parole. Pour un autre joueur, le lot attend une approbation. Une relance reprend les albums en échec et conserve les résultats déjà intégrés. Le moteur d’import utilise son journal détaillé et ses règles de dédoublonnage. L’index des mobiles reprend les pistes publiées, puis Strawberry les retrouve par son moniteur de collection.

L’import affiche le nombre de pistes traitées, y compris quand la source échoue avant le transfert. Le moteur commun utilise `yt-dlp` dans `$PATH` par défaut. Pour une mise à jour propre à Kara, placer l’exécutable officiel vérifié dans `${XDG_DATA_HOME:-$HOME/.local/share}/kara/tools/yt-dlp`. Cette copie sert à Parole, au mobile et à la commande `kara`. ArchASP a été vérifié avec la version `2026.11.23`.

L’organisateur lance **Préparer la partie**. Parole déduplique les fichiers, utilise les références valides et fait travailler le moteur Turbo existant sur la sélection restante. **Relancer la préparation** reprend les fichiers en échec. Les décisions de chant sans note conservent les contrôles de préparation existants. L’organisateur lance la partie, peut mettre en pause, reprendre ou confirmer l’interruption d’un tour. Depuis **Nouvelle partie**, les choix restent disponibles et chaque joueur confirme à nouveau sa disponibilité.

Pour un tour mobile noté, le serveur ouvre d’abord le moteur de score. Le chanteur active explicitement son micro, puis la lecture démarre à la réception des premiers blocs. Le serveur autorise une seule session vocale par tour. Le réglage personnel s’applique au retour, progressivement, avec un plafond organisateur. La capture adressée à Whisper reste indépendante de ce réglage. La préférence est associée au joueur de la salle et à la sortie choisie. Les scores restent gouvernés par les mots évaluables et le timing. Le classement mobile utilise la moyenne arrondie des scores publiés pour chaque joueur et affiche le nombre de tours notés.

Dans **Micro → Mixage du tour**, le chanteur règle **Musique**, **Chanteur original** et **Ma voix** de 0 à 100 %. Les préférences se règlent aussi avant le tour dans **Mes chansons** et côté organisation dans **Admin → Joueurs et réglages audio**. Le bouton **Couper le micro** arrête immédiatement la capture mobile. Les limites de retour et de musique restent pilotées par l’organisateur. Les gains musicaux s’appliquent sur un seul pipeline GStreamer avec des transitions de 40 ms. Les deux pistes Demucs sont vérifiées avant la préparation du tour. Strawberry pilote le transport avec sa sortie mise à zéro pendant le mixage, puis retrouve son volume après le tour.

La voix originale est disponible dans les deux modes. Le mode **Entraînement sans note** se choisit avant la partie. En tour noté, les fenêtres où la piste vocale rend l’attribution incertaine sont marquées et écartées de la note. Le retour du joueur passe dans un traitement séparé de l’entrée Whisper. La calibration de restitution s’ouvre avec `KARA_MIX_CALIBRATION=1`, un plafond de retour initial de 50 % et un gain candidat pour la sortie Bluetooth. L’ancien réglage de chaque sortie reste la valeur par défaut tant qu’un gain de restitution validé n’y a pas été mémorisé.

## Profils de calibration du mixage

`python -B bin/_kara_monitor_profiles.py trials` liste les profils temporaires. Sélectionner un profil avec `KARA_MIX_TRIAL` au lancement de Parole. En mode `KARA_SILENT=1`, on peut préparer la salle et relire ses réglages. Pour l’écoute consentie, préparer un tour **Entraînement sans note** avec **Musique** et **Chanteur original** à zéro. Le plafond initial de chaque essai est 50 %. L’organisateur peut le relever jusqu’à la limite du profil. Ajouter la musique puis le guide vocal après l’écoute de la voix seule. Le profil est identifié dans BlackBox et les réglages permanents de la sortie restent disponibles.

| Profil | Sortie et lecteur | Gain d’entrée du retour protégé | Plafond du curseur |
| --- | --- | ---: | ---: |
| `vivobook-reference` | Vivobook, PipeWire natif, file de 2 blocs | ×1 | 60 % |
| `vivobook-retour-haut` | Vivobook, PipeWire natif, file de 2 blocs | ×1 | 85 % |
| `vivobook-voix-2` | Vivobook, PipeWire natif, file de 2 blocs | ×2 | 75 % |
| `vivobook-voix-3` | Vivobook, PipeWire natif, file de 2 blocs | ×3 | 70 % |
| `vivobook-file-souple` | Vivobook, PipeWire natif, file de 4 blocs | ×2 | 75 % |
| `edenwood-sbc-prudent` | EDENWOOD SBC, PipeWire Pulse, file de 4 blocs | ×1 | 70 % |
| `edenwood-sbc-voix` | EDENWOOD SBC, PipeWire Pulse, file de 4 blocs | ×3 | 58 % |
| `edenwood-sbc-natif` | EDENWOOD SBC, PipeWire natif, file de 4 blocs | ×3 | 58 % |

Les profils SBC sélectionnent l’EDENWOOD connecté directement, indépendamment de la sortie par défaut du PC. Le lecteur de retour passe à un gain de flux vérifié de 0 dB avant le premier PCM du micro. Le gain de voix et le limiteur restent appliqués par Kara. Pendant les essais, comparer le niveau à l’oreille, le délai, l’écho et les pertes de blocs avec le même morceau et un niveau de sortie noté. Les essais du 24 novembre ont montré un retour trop faible et une gêne d’écho sur les sorties physiques. La nouvelle configuration attend une écoute de validation.

## BlackBox

Parole ouvre désormais **BlackBox Studio**. Les réglages actifs, les pistes et le protocole de consentement sont décrits dans la [documentation V3](blackbox-v3/architecture.md). Les informations suivantes décrivent le format historique V1.

Dans V1, **Diagnostics** collectait événements, références et décisions. **Diagnostics + audio** pouvait enregistrer le PCM issu du Wi-Fi, l’entrée du worker, le retour numérique, le monitor de la sortie et le micro physique témoin. Le code historique ouvrait le monitor et le témoin pendant la lecture du morceau du tour actif.

La racine par défaut est `$XDG_STATE_HOME/kara/blackbox`, hors Git, mode 0700. Chaque session contient un manifeste JSON v1, `summary.json`, `events.jsonl`, `incidents.jsonl`, `REPORT.md` et éventuellement des segments WAV PCM16 dans `audio/`. Le manifeste final liste les tailles et SHA-256. Chaque événement a un identifiant croissant, un horodatage monotone serveur en nanosecondes et, selon le composant, un joueur, un tour, un morceau ou une position musicale. Les événements PCM indiquent le fichier, le taux, les canaux, la séquence et les positions d’échantillons. La révision TV relie la publication des paroles au compte rendu du navigateur Tizen.

Les trois pistes numériques existantes sont en PCM16. Le monitor stéréo et le témoin mono sont en WAV IEEE Float 32 bits à 48 kHz, sans requantification du DSP V5.1. Ils utilisent deux clients natifs distincts. Le lien du monitor est passif. La demande d’acquisition du témoin est limitée à sa propre branche et ne relie pas cette branche au sink. Les ouvertures, interruptions et pilotes effectivement observés figurent dans `capture.acoustic.turns`. Un monitor peut contenir plusieurs lecteurs. Les événements conservent la classification de la route observée.

Les horodatages du navigateur Tizen et du téléphone utilisent leur propre origine. Chaque cycle PipeWire conserve l’ID du pilote, la position du premier échantillon, le quantum, le compteur de xrun et l’horodatage du cycle sur `CLOCK_MONOTONIC`. Pour l’entrée mobile, les positions du worklet donnent la cadence PCM et la réception ancre chaque activation. BlackBox conserve séparément la position PCM, le temps d’émission, le temps de réception et les événements d’inférence. Les motifs d’arrêt connus du navigateur et le code de fermeture WSS sont enregistrés. Les événements `audio/music-clock` donnent les ancres Strawberry sur l’horloge serveur. L’alignement physique ADC, DAC ou Bluetooth se mesure en calibration. L’événement `audio/reference` identifie par SHA-256 le morceau local et les deux pistes séparées. Les événements `audio/stem-mix` et `audio/voice-volume` consignent leurs réglages.

Le budget par défaut est de 128 Mio pour les diagnostics et 16 Gio avec audio. `KARA_BLACKBOX_MAX_BYTES` définit un autre plafond. Les segments WAV sont bornés à 16 Mio de PCM et le serveur conserve 32 Mio libres. Sur le banc PipeWire isolé, cinq pistes ont produit 14,55 Mo en 12,12 s, soit environ 4,32 Go/h de lecture active, sans perte enregistrée. À ce débit le budget couvre près de quatre heures de lecture. Le processus Python a consommé environ 1,63 s CPU sur ce test, avec une hausse de RSS observée de 2 Mio. Les helpers et la collecte de provenance ont consommé environ 0,28 s CPU. Les échantillons d’utilisation GPU ont varié de 17 à 20 %, avec 19 % avant l’essai. Le pilote et le quantum de la sortie synthétique sont restés stables. Ces relevés concernent le banc isolé. L’analogique réel et l’EDENWOOD restent à mesurer avec le consentement requis.

### Commandes locales

```sh
bin/kara-blackbox list
bin/kara-blackbox summary latest
bin/kara-blackbox validate latest
bin/kara-blackbox timeline latest --turn ID --component turbo --limit 30
bin/kara-blackbox incidents latest
bin/kara-blackbox turns latest
bin/kara-blackbox inspect latest
bin/kara-blackbox score latest --player Camille
bin/kara-blackbox replay latest --turn ID
bin/kara-blackbox extract latest --id EVENT --output /tmp/kara-window.wav
bin/kara-blackbox extract-pcm latest --id PCM_EVENT --output /tmp/kara-tap.wav
bin/kara-blackbox replay-whisper latest --id EVENT --device gpu
bin/kara-blackbox analyze latest
bin/kara-blackbox prune --days 30
```

`latest` désigne la session la plus récente. Passer l’identifiant du dossier permet de choisir une autre session. `--root CHEMIN` sélectionne un espace de test. `--event`, `--song`, `--component`, `--turn`, `--id`, `--since` et `--until` filtrent le journal en flux. Les deux bornes temporelles sont en nanosecondes monotones. Pour extraire une fenêtre, prendre l’identifiant d’un événement `turbo/window`. L’extraction écrit du PCM Whisper 16 kHz et indique la couverture. Les échantillons absents sont remplis de zéros.

`extract-pcm` lit l’événement PCM choisi, conserve son format WAV et fournit les repères du cycle PipeWire pour une piste acoustique. `--frames` peut élargir la lecture dans le même segment. Dès que l’extrait dépasse le cycle initial, vérifier dans le journal les ancres des cycles suivants avant tout alignement. `validate` vérifie les tailles, empreintes, références PCM et indices d’horloge, et indique le nombre de lacunes dans l’index des cycles.

`replay` exécute le comparateur existant avec les références, les observations et la tolérance capturées. `replay-whisper` relance le décodeur sur la même plage et compare les mots et la classification disponibles. Les options `--backing CHEMIN --original CHEMIN` fournissent les références locales de la protection contre l’accompagnement. L’empreinte du morceau original est contrôlée lorsqu’elle est disponible. Le replay s’exécute hors ligne et conserve la session. Toute conclusion sur une boucle acoustique réelle demande les mesures physiques correspondantes.

`delete ID --yes` supprime une session terminée. `prune --days N` montre les sessions expirées par la politique choisie. Ajouter `--yes` pour appliquer cette sélection. Les captures vocales restent privées sur ArchASP.

## Vérifications

```sh
python3 -B tests/test-kara-v1.py -v
PAROLE_TEST_GTK=1 python3 -B tests/test-parole-import.py -v
```

Le S21 FE résout `kara.labfytools.com` vers `192.168.1.63`. Chrome a chargé le certificat public, ouvert la sonde WSS, compilé l’AudioWorklet dans un contexte hors ligne et restauré son rôle et sa sélection après rechargement. Un essai via la connexion cellulaire de l’iPhone a atteint le service public témoin sur le port 443 du routeur résidentiel. Les connexions aux ports Kara 8443, 8444 et 8765 ont expiré pendant que le serveur répondait sur le LAN.

Dans une salle de prévol silencieuse, le S21 FE a aussi enchaîné les commandes de préparation, de lancement, de pause, de reprise et de fin. Le navigateur a affiché le score de la fixture et l’état de BlackBox finalisée, puis lancé une nouvelle partie. Le CLI a validé la session. Un autre essai a extrait deux secondes de PCM synthétique et exécuté une passe large-v3-turbo via Vulkan avec une couverture de 100 %. La préparation, les notes et la restitution de la partie mobile ont été simulées.

Le WGT 1.3.0 a été signé avec le certificat auteur d’origine et le certificat de distribution du profil `kara-tv-physique`. L’installation et le lancement via SDB ont réussi sur la Samsung TQ55QEF1AUXXC sous Tizen 9.0. L’affichage plein écran et la lisibilité du QR ont été confirmés sur la TV. Une sonde Tizen temporaire a mesuré une zone d’affichage de 1920 × 1080 et chargé le QR par HTTPS avec le nom DNS de Kara. Le client installé lit l’état sur le port local 8765.

Sur iPhone 13 sous iOS 26.6, Safari a chargé la salle sans alerte de certificat, ouvert la sonde WSS et chargé l’AudioWorklet en mode hors ligne. La sélection a survécu au rafraîchissement et au retour au premier plan. Le code mobile ferme l’entrée lors du passage en arrière-plan et actualise la salle au retour. Les permissions et la reprise d’une capture réelle doivent être vérifiées pendant un essai vocal autorisé.

Le scénario silencieux sur le S21 FE a couvert les deux tours, les commandes de retour individuel et de volume musical, la reconnexion TV, le classement et la finalisation automatique de BlackBox. Le journal d’essai contient 44 événements, dont neuf rendus TV et un marqueur d’incident. Un essai distinct sur Parole a vérifié la commande de bureau, l’état partagé et l’arrêt depuis l’administration mobile. Les pistes vocales et acoustiques restent à valider lors d’une partie instrumentée.

L’import réel d’un album autorisé depuis le S21 FE a intégré sept pistes en 50 secondes, avec la pochette, les paroles intégrées aux sept pistes et trois LRC synchronisés. Strawberry a indexé les sept pistes, le mobile a ajouté le titre choisi et l’aperçu de la pochette a été vérifié. La préparation Turbo demandée depuis le téléphone a classé la référence du titre live insuffisante pour la notation et a préservé le LRC source. Le lot de 19 albums relancé depuis le mobile avec le moteur actualisé a intégré 411 nouvelles pistes et retrouvé un album déjà présent en environ 22 minutes. Strawberry a indexé les 418 pistes des 19 albums. Le tri et l’enrichissement représentent la majeure partie du délai.
