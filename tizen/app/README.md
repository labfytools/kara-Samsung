# Kara sur Samsung

Ouvre **Kara** depuis **Applications** sur la télévision. Le client 2.1.0
attend le serveur HTTPS d’ArchASP puis affiche le QR de jeu et le PIN de
l’organisateur avec le morceau joué dans Strawberry. **Réessayer** relance immédiatement la connexion.

## Construire une mise à jour

1. Prépare un nouveau dossier privé avec `bin/kara-tv-package CHEMIN`.
   Ce dossier contient les sources à signer et la configuration locale de
   l’écran. Le paquet signé doit rester dans un espace privé.
2. Signe le dossier avec Tizen Studio et le profil Samsung TV
   `kara-tv-physique`, qui utilise le certificat auteur d’origine et le
   certificat de distribution associé au téléviseur.
3. Vérifie que le WGT porte la version attendue et l’identifiant
   `LabfyKara0.Karaoke`. Installe la mise à jour via SDB sur la télévision
   en Developer Mode. L’adresse du PC côté Developer Mode doit correspondre
   à l’adresse LAN actuelle d’ArchASP.
4. Pour vérifier l’autonomie, ouvre Kara avec la télécommande depuis le menu
   **Applications**. Vérifie l’accueil, le QR HTTPS et le PIN, puis relance
   l’application pendant une partie pour vérifier qu’elle retrouve son état.

[Connexion Samsung TV](https://developer.samsung.com/smarttv/develop/tools/additional-tools/vscode/connecting-to-tv-device.html) ·
[Certificats Samsung TV](https://developer.samsung.com/smarttv/develop/getting-started/setting-up-sdk/creating-certificates.html)
