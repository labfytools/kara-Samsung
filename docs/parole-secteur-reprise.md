# Reprise nocturne après interruption du secteur

Le 26 novembre 2026, à 21:23:42, Parole avait admis un traitement capot fermé. À 21:23:45, le secteur a été signalé absent et le descripteur `handle-lid-switch` libéré. Logind a suspendu la machine. Plusieurs transitions secteur/batterie ont suivi ; après retour secteur, l'ancien code ne réarmait le descripteur que capot ouvert. Le noyau a ensuite journalisé des erreurs UCSI. Le journal ne prouve pas si les premières pertes secteur sont physiques ou transitoires.

Le correctif suspend les calculs dès une lecture secteur négative ou indéterminée. Pour une fermeture déjà protégée uniquement, il conserve l'inhibiteur de capot au plus 15 secondes pendant la confirmation d'une perte secteur ; les calculs restent arrêtés. Après deux secondes de secteur continu, il reprend l'admission normale. Une perte durable libère les descripteurs après l'arrêt coopératif du travail. Capot ouvert, la perte secteur conserve son comportement immédiat.

Si la machine est éveillée et la session active, le retour d'un secteur stable permet aussi de réarmer le capot déjà fermé, après vérification de l'absence de demande de veille. Le verrou Sway et l'extinction des écrans sont toujours vérifiés avant le calcul. Le budget persistant de huit heures, les options nocturnes et les limites mémoire, température et stockage ne sont pas contournés. La veille manuelle continue d'utiliser `sleep:delay`, jamais `sleep:block`. Si le firmware ne réveille pas une machine réellement suspendue au branchement secteur, aucun processus utilisateur ne peut garantir cette reprise avant le réveil matériel.

Qualification automatisée : tests nocturnes (27 cas) et ressources (5 cas), incluant microcoupures, perte durable, reprise capot fermé, verrouillage, veille manuelle et expiration du budget. L'essai physique de débranchement/rebranchement capot fermé reste nécessaire.

Retour logiciel : redéployer la révision précédente au repos, sans restaurer l'index ni réinitialiser les files. Ne pas modifier logind, le VPN ou les protections matérielles.

## Durée illimitée sur secteur

Le réglage `night_minutes=0` désactive uniquement l’expiration temporelle. Les durées finies restent disponibles dans les paramètres. Le test simule 49 heures de maintien puis une perte secteur et confirme que l’inhibiteur est libéré après le délai de confirmation. Les protections thermiques, mémoire, disque, verrouillage et veille manuelle restent inchangées. Une endurance physique de 48 heures n’a pas été mesurée.

## Plafond thermique configurable à 95 °C

Sur demande explicite de l’utilisateur, les trois réglages nocturnes peuvent atteindre 95 °C, y compris capot fermé. Les valeurs par défaut restent conservatrices (78/75/70 °C). Le calcul est interrompu dès que l’un des capteurs atteint la limite effective ou si un capteur nécessaire est absent. La protection matérielle, le gouverneur mémoire et la réserve disque restent inchangés. Le plafond ne constitue pas une température cible ni une qualification d’endurance capot fermé.

## Pression mémoire nocturne

Le journal du 26 novembre à 21:59 montre deux interruptions de Demucs par `some avg10` à 1,77 % et 2,08 %, malgré 6176 et 6672 Mio disponibles. Le seuil historique de 1 % reste applicable au démarrage et aux traitements hors calcul lourd capot fermé. Pour un calcul lourd déjà admis, capot fermé, le gouverneur tolère `some avg10 < 5 %` et `full avg10 < 3 %` ; les deux mesures doivent être valides. La réserve au démarrage et en cours de calcul, les températures, le secteur et les limites disque restent inchangés. Après un refus mémoire pendant un calcul lourd, aucun autre calcul lourd nocturne capot fermé ne démarre pendant 30 secondes ; le travail léger peut continuer s’il est admissible. Les valeurs `some` et `full` et leurs seuils figurent désormais dans les diagnostics. La politique distingue les admissions et les continuations, même dans la fenêtre du cache des mesures. Ces seuils sont une politique locale expérimentale, non une recommandation universelle du noyau. Voir https://docs.kernel.org/accounting/psi.html. La qualification sur une chanson réelle et l’endurance restent nécessaires.

### Priorité au calcul capot fermé

Sur demande de l’utilisateur, le PSI partiel et complet devient informatif pour l’admission et la poursuite d’un traitement lourd nocturne capot fermé. Les seuils expérimentaux de 5/3 % décrits ci-dessus sont remplacés dans ce cas uniquement. Les mesures doivent toujours être disponibles et valides. Les réserves de RAM au démarrage (4096 Mio par défaut) et en cours de calcul (1536 Mio), la pause de 30 secondes après un véritable refus mémoire, les contrôles thermiques, disque et secteur restent en vigueur. Le PSI continue également à limiter les travaux d’indexation et les traitements légers concurrents ; les règles capot ouvert sont inchangées. Le test contrôlé admet 40 % de PSI partiel et 25 % de PSI complet avec RAM disponible suffisante, rejette une nouvelle admission à 3072 Mio et interrompt un calcul à 1280 Mio. Ces valeurs sont simulées, non une qualification physique de stabilité. Une pression soutenue peut encore ralentir fortement les calculs ; aucune modification du swap, de zram ni des mécanismes OOM du noyau n’est effectuée.

### Mode calcul prioritaire capot fermé

Les seuils de charge CPU, GPU et disque ne bloquent plus l’admission nocturne capot fermé. Le PSI devient informatif également pour les recherches nocturnes légères. Les mesures restent requises, et les réserves RAM et disque, le plafond thermique, le secteur, le verrouillage et la priorité Kara restent inchangés. Demucs utilise six threads au lieu de deux dans ce mode, en conservant le modèle, les paramètres acoustiques et un seul calcul lourd à la fois. La charge peut atteindre 100 % sans que le pourcentage de charge interrompe une tâche. Cela ne garantit ni une occupation permanente des deux processeurs ni un gain de débit sur une machine à mémoire partagée. Les essais simulés couvrent les fortes charges et les réserves mémoire. Les performances physiques restent à mesurer.

### Douze threads Demucs

Le mode intensif nocturne capot fermé transmet explicitement douze threads à Demucs, qui applique cette valeur aux réglages OMP et MKL. Le mode nocturne capot ouvert conserve deux threads et les autres usages conservent six threads par défaut. Le GPU reste accessible à Turbo via Vulkan, sans seuil bloquant de charge GPU dans le mode capot fermé. Les contrôles de mémoire disponible et de température demeurent actifs. La charge GPU réellement atteinte dépend de l’algorithme ; elle n’est pas forcée par le gouverneur. Les tests de configuration passent ; le débit et la stabilité à douze threads restent à qualifier sur une chanson complète.
