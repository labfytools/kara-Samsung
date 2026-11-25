# AGENTS.md — Kara / Parole

Ces règles s'appliquent à tout le dépôt. Elles concernent les agents de développement (Codex, etc.) et non l'agent de recherche ou de diagnostic exécuté par Parole.

## 1. Discipline Git obligatoire

- Les commits locaux sont autorisés sans demander de confirmation.
- **Ne jamais attendre la fin d'un chantier complet pour committer.** Découper le travail en évolutions cohérentes, testables et réversibles.
- Dès qu'un lot fonctionnel est implémenté et validé, effectuer son commit avant d'entreprendre le lot suivant.
- Un commit = une évolution identifiable. Inclure les tests et la documentation directement liés à cette évolution.
- Les commits de correction, de refactorisation ou de documentation doivent également avoir un périmètre clair.
- Ne pas créer de commits vides ou artificiels. Ne pas inventer un historique de validations qui n'ont pas eu lieu.
- Garder la branche de travail actuelle, sauf besoin explicite d'une branche de travail distincte. Ne jamais intégrer automatiquement dans `main`.

## 2. Avant de commencer

- Lire `git status --short --branch`, l'historique récent et les différences déjà présentes (`git diff` et `git diff --cached`).
- Identifier les changements préexistants, les fichiers non suivis et les travaux d'autres agents.
- Ne jamais écraser, annuler, déplacer, mettre en stash ou committer des changements étrangers au chantier sans accord.
- Si plusieurs agents travaillent dans le dépôt, éviter de modifier ou de mettre en scène les mêmes fichiers simultanément. En cas de conflit, conserver les travaux et signaler le blocage.
- Pour un chantier important, définir des lots de réalisation et leurs critères de validation avant de coder.

## 3. Convention de commits

Utiliser Conventional Commits avec un sujet explicite en français :

`type(périmètre): description concise de la modification`

Types usuels : `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `build`, `ci`, `chore`.

Exemples :

- `feat(parole): ajouter l'index persistant de la bibliothèque`
- `feat(ui): intégrer la console de maintenance`
- `feat(lyrics): rechercher automatiquement les paroles manquantes`
- `feat(lrc): générer les références synchronisées avec Turbo`
- `feat(albums): détecter les pistes manquantes`
- `feat(codex): intégrer l'agent de recherche approfondie`
- `feat(sources): ajouter le registre dynamique des fournisseurs`
- `fix(import): reprendre uniquement les pistes en échec`
- `feat(kara): préparer les chansons pendant l'inactivité`
- `fix(tizen): restaurer le démarrage sans salon actif`

Éviter les messages vagues tels que « update », « modifications », « secure push » ou « finalisation » sans périmètre. Pour un changement complexe, préciser dans le corps du commit la raison, les choix techniques et les limites.

## 4. Contrôles avant chaque commit

1. Exécuter les tests pertinents du lot et les vérifications de non-régression adaptées.
2. Documenter les tests non exécutés, ignorés ou impossibles à réaliser. Ne jamais les présenter comme réussis.
3. Examiner `git diff` et `git diff --check`.
4. Mettre en scène explicitement les fichiers ou portions de fichiers concernés. Ne pas utiliser aveuglément `git add -A`, `git add .` ou `git commit -a`.
5. Examiner `git diff --cached --stat`, `git diff --cached` et `git diff --cached --check`. Vérifier que le contenu mis en scène ne contient aucun travail étranger.
6. Contrôler l'absence de secrets, jetons, cookies, clés privées, certificats privés, captures BlackBox, données personnelles, fichiers musicaux, modèles IA, caches, journaux privés, fichiers temporaires ou artefacts volumineux non approuvés.
7. Effectuer le commit local.
8. Vérifier son hash, son périmètre et l'état du dépôt après le commit.

Ne pas masquer un échec de test. Si un lot n'est pas validable, le conserver distinct et signaler le blocage avant de déclarer la fonctionnalité terminée. Un commit de documentation seule peut être validé par relecture et contrôle du diff ; ne pas inventer de test logiciel.

## 5. Travaux déjà non committés

Lors de l'introduction de ces règles, ou à la reprise d'un chantier :

- Auditer les changements existants et identifier leurs propriétaires et périmètres.
- Pour les changements du chantier que l'agent est autorisé à committer, reconstituer des lots fonctionnels cohérents à partir des différences réellement présentes.
- Valider et committer chaque lot séparément lorsque les dépendances le permettent.
- Si des changements sont techniquement indissociables, conserver un commit cohérent et expliquer son périmètre dans son message.
- Ne jamais réécrire les commits publiés, ni fabriquer des étapes fictives pour donner l'apparence d'un historique progressif.

## 6. Traçabilité

- Mettre à jour le CHANGELOG pour les évolutions significatives (créer un CHANGELOG.md si nécessaire) et la documentation concernée.
- Présenter dans les rapports intermédiaires et finaux les hashes courts, messages et périmètres des commits réalisés, les tests réellement effectués et les limitations restantes.
- Indiquer clairement l'état de la branche, les éventuelles modifications restantes et les validations matérielles encore nécessaires.
- Ne pas présenter un test sur fixture ou en simulation comme une validation physique.
- Conserver les consignes Git et la documentation à jour dans le dépôt.

## 7. Autorisations et interdictions

Autorisés sans confirmation supplémentaire :

- Inspection du dépôt, de ses branches et de son historique.
- Mise en scène ciblée des changements du chantier.
- Commits locaux selon les règles ci-dessus.
- Création de branches de travail non destructives lorsque nécessaire, sans perturber une session concurrente.

Accord explicite de l'utilisateur requis avant :

- Tout push, y compris sur une branche de travail ou un miroir.
- Toute fusion vers `main` ou une branche d'intégration publiée.
- Tout rebase ou changement d'historique existant.
- Tout reset destructif, nettoyage destructif, suppression de branche ou force push.
- Toute publication de release ou de tag distant.

Ne jamais contourner une protection Git. Ne pas modifier l'historique déjà publié. Ne pas utiliser les dépôts distants comme sauvegarde automatique sans accord.

## 8. Frontière entre développement et agent Parole

L'autorisation de committer concerne les agents chargés du développement du projet, dans leur périmètre de travail.

L'agent Codex intégré à Parole pour les recherches Internet et le diagnostic des importations :

- Ne dispose d'aucune autorisation de commit, de push ou de modification du code de production.
- Ne peut ni appliquer librement des correctifs exécutables ni modifier la configuration système.
- Peut proposer un correctif ou un adaptateur dans un espace isolé, avec des tests et un rapport.
- Doit attendre la validation explicite de l'utilisateur avant l'intégration et l'activation de tout nouveau code exécutable.

Préserver les données, les regroupements manuels d'artistes, les importations en cours et les fonctionnalités de Kara. Ne pas interrompre la production pour valider un changement sans accord lorsque cela perturberait une opération active.
