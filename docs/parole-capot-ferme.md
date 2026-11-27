# Validation de la recherche nocturne des LRC capot fermé

## Incident du 26 novembre 2026 sur ArchASP

| Heure locale | Observation |
| --- | --- |
| 07:29:30 | Une veille commencée la veille se termine. Le dernier état capot de logind est fermé. |
| 07:29:37 | `systemd-logind` annonce une nouvelle suspension. Aucun nouvel événement de fermeture n’apparaît à cette seconde, ce qui concorde avec sa réévaluation d’un capot déjà fermé. NetworkManager met le Wi-Fi en sommeil en réponse à la demande. |
| 07:29:38 | `systemd-suspend.service` démarre et le noyau entre en `s2idle`. |
| 07:30:01 | Logind journalise une ouverture alors que le capot est signalé physiquement fermé par l’utilisateur. Le noyau est encore dans le parcours de reprise. |
| 07:30:09 | Sortie de `s2idle`, puis réactivation du réseau. Proton VPN retrouve son tunnel à 07:30:18. |
| 07:30:59 | Nouveau signal de fermeture. Logind déclenche une veille quatre millisecondes plus tard. |
| 08:57:09 | Signal d’ouverture, sortie du noyau et reprise de Parole après environ 86 min 10 s de veille. Proton VPN se reconnecte à 08:57:17. |

La configuration chargée donne `HandleLidSwitch=suspend` et `HandleLidSwitchDocked=ignore`. `HandleLidSwitchExternalPower` est commenté, donc le secteur utilise `HandleLidSwitch` hors station d’accueil et hors configuration à plusieurs écrans. `IdleAction=ignore`. Les inhibiteurs `sleep:delay` de NetworkManager, UPower, Realtime Kit et swayidle retardent brièvement une suspension déjà demandée. L’inventaire avant correction ne contenait aucun inhibiteur `handle-lid-switch`. [Configuration logind](https://www.freedesktop.org/software/systemd/man/262/logind.conf.html)

Le service `swayidle.service`, y compris son remplacement utilisateur, lance `labfy-lock`, commande l’extinction et le réveil des écrans, met les médias en pause et reçoit l’annonce de veille. QuickShell fournit le verrou Wayland utilisé sur cette machine. Son action **Veille** dans le centre de contrôle appelle manuellement `systemctl suspend`. Les configurations Sway et QuickShell inspectées ne déclarent aucune commande automatique de suspension sur fermeture. Le pilote ACPI expose le capot `LID0` sur le périphérique d’entrée que logind surveille. Le noyau de ce démarrage a journalisé `[Firmware Bug]: Unexpected lid state reported by firmware` le 25 novembre. En mode `button.lid_init_state=method`, le pilote relit `_LID` pendant sa reprise. Le faux signal journalisé par logind précède l’achèvement de la veille et devient visible dans Parole par `LidClosed`. Cette séquence rend une remontée ACPI de reprise probable. Le journal de l’incident ne permet pas de distinguer une valeur de firmware transitoire d’un autre événement de bas niveau. [Pilote Linux](https://github.com/torvalds/linux/blob/master/drivers/acpi/button.c) · [Documentation ACPI du noyau](https://docs.kernel.org/firmware-guide/acpi/acpi-lid.html)

Les réglages de production avaient activé le mode nocturne et la préparation écran éteint. Les options de capot fermé et de qualification thermique étaient désactivées. Le code attendait en plus une session verrouillée, inactive et une tâche de calcul admissible pour prendre un inhibiteur. Parole a donc réagi à la suspension après la décision de logind. Le plafond précédent était de 120 min par défaut, avec une limite de 360 min dans l’interface.

## Essai physique

Le [programme d’observation](../tests/observe-parole-night.py) exécute le propriétaire de maintenance de la version testée sur une copie locale d’un morceau et de son LRC de référence. L’index, les pistes séparées et les publications restent dans le répertoire de l’essai. Les tâches fournisseur du candidat sont désactivées pour éviter un second quota en parallèle de la production. Le programme réalise des sondes HTTPS via le tunnel et une préparation Kara réelle avec les protections normales. Les paliers de l’essai peuvent être séparés si une interruption entraîne la veille.

1. Démarrer capot ouvert, sur secteur, sans lecteur ni partie Kara en cours. Une lecture Bluetooth peut rester active après le verrouillage. Vérifier son état avant une mesure de calcul. Utiliser un dossier neuf sur un volume qui conserve 20 Gio libres. Sur cette machine, le profil Économie a produit une préparation complète capot fermé à 62,6 °C CPU et 56 °C GPU lors de la campagne précédente. Relever le profil initial. Un essai au profil Économie peut le louer temporairement avec `tlpctl launch --profile power-saver`. Vérifier la valeur effective avec `tlpctl get`.
2. Attendre **Inhibiteur de capot actif · essai prêt** et vérifier la ligne `Parole … handle-lid-switch … block` de `systemd-inhibit --list`, alors que la session est encore ouverte. La sonde initiale doit donner un état réseau connecté et un code HTTP réussi via l’interface VPN.
3. Fermer physiquement le capot sur secteur et attendre 8 à 12 minutes. Laisser l’ordinateur éveillé jusqu’à obtenir une préparation complète ou une raison de report mesurée. Le journal doit montrer la fermeture, le verrou confirmé, l’extinction des écrans et l’avancement de la file. Les données capot fermé doivent montrer des sondes VPN réussies, des températures sous les seuils pendant le calcul et aucune entrée en veille. Un ralentissement du VPN peut être mesuré aux intervalles de quinze secondes des sondes.
4. Rouvrir le capot. Vérifier que l’écran revient sur le verrou, puis déverrouiller. Vérifier le motif de clôture et le décompte de la dernière fermeture. Un nouvel essai de débranchement ferme ensuite le capot sur secteur, débranche le chargeur pendant une préparation et relève l’arrêt du moteur et la disparition des inhibiteurs. Avec la politique de logind ici, une nouvelle mise en veille peut suivre immédiatement tant que le capot est fermé.
5. Rebrancher et rouvrir. Avec `handle-lid-switch:block` à nouveau actif, demander **Veille** depuis QuickShell. Réveiller le PC normalement. Les journaux doivent montrer l’entrée et la sortie de `s2idle`, et le verrou doit rester en place. Une demande pendant un moteur actif doit donner un arrêt propre du groupe avant que logind poursuive après son délai normal.

Exemple de lancement dans la session Sway active, en remplaçant les deux chemins d’essai.

```sh
systemd-run --user --unit=parole-capot-essai --collect -p RuntimeMaxSec=32min \
  --working-directory="$PWD" \
  /usr/bin/python3 -B "$PWD/tests/observe-parole-night.py" \
  "$PWD/.runtime/parole-capot-essai" --audio "/chemin/vers/titre.opus" \
  --vpn-interface proton0 --minutes 30
journalctl --user -u parole-capot-essai.service -f
```

Le relevé `samples.jsonl` contient les deux horloges, les températures CPU/GPU, le secteur, les états bruts ACPI et logind, le verrou, les écrans, la raison courante et les tâches terminées. Un écart entre la progression de `CLOCK_BOOTTIME` et celle de l’horloge monotone montre un passage par la veille, à confirmer dans `suspend.log`. Le `bilan.json` est produit à la fin. Le journal de maintenance du dossier d’essai précise les phases Demucs/Turbo. La commande `systemctl --user stop parole-capot-essai.service` met fin à l’essai.

## Activation et retour arrière

La politique système utilise les réglages logind, Sway, swayidle, QuickShell, NetworkManager et Proton existants. Après le test, sauvegarder l’index SQLite par son API de sauvegarde en ligne et relever les valeurs initiales de `night_lid`, `night_lid_qualified` et `night_minutes`. Activer les deux options de capot dans **Parole → Paramètres → LRC nocturnes**, puis redémarrer `kara.service` quand aucune partie et aucun import ne risquent d’être interrompus. Vérifier que le nouveau propriétaire publie l’événement d’armement capot ouvert. Le premier essai de production sur une nuit entière doit confirmer le bilan et l’absence d’événement de suspension non sollicité.

Pour revenir au comportement précédent, décocher **Rechercher les LRC lorsque le capot est fermé**. Parole libère le descripteur et logind applique sa politique habituelle. Restaurer les seules valeurs nocturnes sauvegardées si nécessaire. Une restauration de la base entière écraserait les tâches et bilans plus récents. Les relevés restent dans le dossier privé de l’essai.
