# BlackBox — diagnostics Kara

Ce dossier est le point d'accès aux captures et aux analyses BlackBox de Kara.

- `sessions/` : lien symbolique local vers la racine privée utilisée par le moteur BlackBox (`$XDG_STATE_HOME/kara/blackbox`, ou `~/.local/state/kara/blackbox` par défaut). Toutes les sessions existantes et futures y sont accessibles sans duplication.
- `reports/` : espace local pour les rapports d'analyse, extraits et diagnostics complémentaires.

Les deux emplacements sont exclus de Git. Les captures originales restent dans leur emplacement XDG privé, avec leurs permissions existantes. Ne pas les déplacer, modifier ou supprimer automatiquement. Ne jamais publier d'audio, de journaux privés ou de données personnelles.

## Consultation

Depuis la racine du projet :

```sh
bin/kara-blackbox list
bin/kara-blackbox summary IDENTIFIANT_SESSION
bin/kara-blackbox validate IDENTIFIANT_SESSION
bin/kara-blackbox incidents IDENTIFIANT_SESSION
```

Remplacer `IDENTIFIANT_SESSION` par l'identifiant renvoyé par `list`. Voir `bin/kara-blackbox --help` pour les autres commandes. Les rapports et fichiers dérivés doivent rester locaux dans `blackbox/reports/` (ou dans un autre emplacement privé).

Les fenêtres de rejeu lisent des fichiers WAV ordinaires dans le dossier `audio/` de leur session. Ce dossier et ses fichiers doivent être présents directement dans la capture.

## Après un nouveau clone

Les données locales ne sont pas clonées avec le dépôt. Une fois la racine BlackBox privée créée par Kara, recréer le lien et le dossier d'analyse si nécessaire :

```sh
ln -s "${XDG_STATE_HOME:-$HOME/.local/state}/kara/blackbox" blackbox/sessions
mkdir -m 700 -p blackbox/reports
```

Ne pas remplacer un chemin existant sans en vérifier la nature et le contenu.

Documentation technique : [Kara V1 — BlackBox](../docs/kara-v1.md) et [Kara V2](../docs/kara-v2.md).
