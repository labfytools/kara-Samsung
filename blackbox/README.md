# BlackBox

Parole peut enregistrer depuis **BlackBox Studio** un diagnostic de la lecture Strawberry, du minutage LRC et des réponses de l’écran. BlackBox conserve ces diagnostics et les captures historiques dans `$XDG_STATE_HOME/kara/blackbox`, ou sous `~/.local/state/kara/blackbox`.

- `bin/kara-blackbox list` liste les sessions.
- `bin/kara-blackbox validate IDENTIFIANT` vérifie l’intégrité du manifeste et des fichiers.
- `bin/kara-blackbox timeline IDENTIFIANT` affiche le journal.
- `bin/kara-blackbox report IDENTIFIANT` affiche le bilan disponible.

Le microphone et son retour passent directement par l’EDENWOOD. Une validation auditive se fait sur l’enceinte. Pour relire les diagnostics historiques des anciens moteurs, utiliser la branche locale `archive/kara-multijoueur-2.1.0`.
