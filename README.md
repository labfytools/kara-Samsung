# Kara Samsung

Projet de karaoké pour **Arch Linux / Sway**, Strawberry, navigateurs mobiles et **TV Samsung Tizen**. Le code a été regroupé le 23 novembre 2026 depuis les lanceurs locaux, l’interface web et les sources Tizen.

## Organisation

| Chemin | Contenu | Versionné ? |
| --- | --- | --- |
| `bin/` | Application Strawberry, serveur de paroles/TV, sessions de jeu, micro HTTPS AudioWorklet et utilitaires Python | Oui |
| `tizen/app/` | Source de l’application Tizen Samsung (`config.xml`, HTML, CSS, JS, icône) | Oui |
| `native/src/` | Source C de l’alignement DTW | Oui |
| `tests/` | Tests de langage / scripts de vérification | Oui |
| `.runtime/` | SDK et émulateur Tizen, environnements Python, modèles, artefacts signés, sauvegardes, certificats | **Non : ignoré par Git** |

## Points d’entrée

- `~/.local/bin/paroles-strawberry` : interface GTK4 avec Strawberry, TV WebSocket sur le port **8765**.
- `~/.local/bin/kara` : import et bibliothèque de musique.
- `~/.local/bin/kara-tizen-tv` : émulateur Samsung via Podman.
- `~/.local/bin/kara-tv-test` : test d’interface TV.
- `python3 -u bin/_kara_micro_test.py` : serveur HTTPS de test micro sur le port **8766**.

Les anciens lanceurs dans `~/.local/bin` sont des liens symboliques vers ce dépôt, afin de préserver les raccourcis et la session courante. Le dossier historique `~/.local/share/kara` pointe vers `.runtime/kara-data` ; son contenu n’est pas versé dans Git.

## Contrôle rapide

```bash
cd ~/Documents/Development/kara-Samsung
python3 -m compileall -q bin
node --check bin/_kara_micro_worklet.js
node --check tizen/app/app.js
git status --short
```

Les dépendances système (GTK4/PyGObject, aiohttp, numpy, scipy, PipeWire, Strawberry, Tizen SDK, Podman, etc.) ne sont pas fournies par ce dépôt. Des environnements installés localement existent dans `.runtime/` mais ne sont pas portables tels quels.

## Sécurité et données

Les tokens, certificats et clés du micro HTTPS sont conservés dans `~/.local/state/kara/micro-test/` ; ne jamais les commiter. Les certificats de signature Tizen, les paquets signés, les modèles et données d’exécution sont exclus du dépôt. Ne pas versionner de musiques ni d’enregistrements vocaux.

## État

Le retour vocal normal utilise des blocs de 20 ms. Une calibration sans retransmission directe du microphone est disponible dans l’interface de test. Le diagnostic horodaté Worklet → WebSocket → Arch est en cours de validation réelle.

**Publication :** dépôt Git local uniquement. Aucun dépôt distant ni push n’a été créé automatiquement.
