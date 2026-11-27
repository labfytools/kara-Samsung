# Kara sur Samsung

Ouvre **Kara** depuis **Applications** sur la télévision. La version 2.2.3 suit le morceau en cours dans Strawberry et affiche le LRC d’origine, ou les paroles simples. Les crédits lrc.red et Unison apparaissent sous l’artiste lorsque leur LRC est affiché. **Réessayer** relance la connexion au PC. Le réglage **Minutage** de Parole s’applique à la TV.

Kara demande à Samsung de suspendre l’économiseur d’écran tant que l’affichage est au premier plan, même pendant une pause. L’économiseur est rétabli à la sortie et en arrière-plan. La minuterie de mise en veille et la mise hors tension automatique restent des réglages système Samsung. [API AppCommon](https://developer.samsung.com/smarttv/develop/guides/fundamentals/setting-screensaver.html)

## Construire une mise à jour

1. Lance `bin/kara-tv-package CHEMIN` vers un nouveau dossier privé.
2. Construis puis signe le widget Samsung avec le profil `kara-tv-physique` et le certificat auteur de l’application. Le dossier préparé et le WGT signé contiennent la clé de l’écran.
3. Vérifie la version et l’identifiant `LabfyKara0.Karaoke`. Installe via SDB en Developer Mode puis lance l’application.
4. Vérifie avec Strawberry la lecture normale, la pause, un déplacement en avant et en arrière, le changement de titre et au moins un titre dont le LRC comporte des repères par mot.

[Connexion Samsung TV](https://developer.samsung.com/smarttv/develop/tools/additional-tools/vscode/connecting-to-tv-device.html) ·
[Certificats Samsung TV](https://developer.samsung.com/smarttv/develop/getting-started/setting-up-sdk/creating-certificates.html)
