# Reprise nocturne après interruption du secteur

Le 26 novembre 2026, à 21:23:42, Parole avait admis un traitement capot fermé. À 21:23:45, le secteur a été signalé absent et le descripteur `handle-lid-switch` libéré. Logind a suspendu la machine. Plusieurs transitions secteur/batterie ont suivi ; après retour secteur, l'ancien code ne réarmait le descripteur que capot ouvert. Le noyau a ensuite journalisé des erreurs UCSI. Le journal ne prouve pas si les premières pertes secteur sont physiques ou transitoires.

Le correctif suspend les calculs dès une lecture secteur négative ou indéterminée. Pour une fermeture déjà protégée uniquement, il conserve l'inhibiteur de capot au plus 15 secondes pendant la confirmation d'une perte secteur ; les calculs restent arrêtés. Après deux secondes de secteur continu, il reprend l'admission normale. Une perte durable libère les descripteurs après l'arrêt coopératif du travail. Capot ouvert, la perte secteur conserve son comportement immédiat.

Si la machine est éveillée et la session active, le retour d'un secteur stable permet aussi de réarmer le capot déjà fermé, après vérification de l'absence de demande de veille. Le verrou Sway et l'extinction des écrans sont toujours vérifiés avant le calcul. Le budget persistant de huit heures, les options nocturnes et les limites mémoire, température et stockage ne sont pas contournés. La veille manuelle continue d'utiliser `sleep:delay`, jamais `sleep:block`. Si le firmware ne réveille pas une machine réellement suspendue au branchement secteur, aucun processus utilisateur ne peut garantir cette reprise avant le réveil matériel.

Qualification automatisée : tests nocturnes (27 cas) et ressources (5 cas), incluant microcoupures, perte durable, reprise capot fermé, verrouillage, veille manuelle et expiration du budget. L'essai physique de débranchement/rebranchement capot fermé reste nécessaire.

Retour logiciel : redéployer la révision précédente au repos, sans restaurer l'index ni réinitialiser les files. Ne pas modifier logind, le VPN ou les protections matérielles.

## Durée illimitée sur secteur

Le réglage `night_minutes=0` désactive uniquement l’expiration temporelle. Les durées finies restent disponibles dans les paramètres. Le test simule 49 heures de maintien puis une perte secteur et confirme que l’inhibiteur est libéré après le délai de confirmation. Les protections thermiques, mémoire, disque, verrouillage et veille manuelle restent inchangées. Une endurance physique de 48 heures n’a pas été mesurée.
