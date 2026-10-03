# SOSSEN Direct

Intégration Home Assistant **locale** pour les micro-onduleurs SOSSEN (2in1 600/800/1000 W, 4in1 2400 W).

Contrairement aux autres intégrations, **aucune clé à chercher, aucune IP à taper** :

1. Tu te connectes une fois avec ton app **Smart Life / PowerHome** (QR code, comme l'intégration Tuya officielle). L'intégration y récupère la liste de tes onduleurs et leurs clés locales.
2. Elle trouve toute seule l'adresse de chaque onduleur sur ton réseau (annonces UDP Tuya, puis balayage du réseau local en secours).
3. Les mesures arrivent ensuite **en direct sur ton réseau** (protocole Tuya 3.5, flux poussé toutes les ~5 s). Le cloud ne sert qu'à relire les clés au démarrage.

## Installation (HACS)

1. HACS → ⋮ → Dépôts personnalisés → `https://github.com/ritonbrunis-lab/sossen-direct`, catégorie *Intégration*.
2. Installe **SOSSEN Direct**, puis redémarre Home Assistant.
3. Paramètres → Appareils et services → Ajouter → **SOSSEN Direct**.
4. Saisis ton **code utilisateur** Smart Life (app → Moi → ⚙️ → Compte et sécurité → Code utilisateur), puis scanne le QR code avec l'app.

Fais la configuration **en journée** : la nuit, l'onduleur s'éteint complètement. Les valeurs apparaissent 2 à 3 minutes après le démarrage.

## À savoir

- **Une seule connexion locale par onduleur.** Désactive toute autre intégration qui s'y connecte en local (`sossen`, LocalTuya, Tuya Local). L'intégration Tuya officielle (cloud) ne gêne pas.
- Si un onduleur n'est jamais trouvé : Options de l'intégration → indique son IP à la main.

## Entités par onduleur

Puissance AC et DC (totale et par panneau), tensions, courants, fréquence, température, énergie totale (compatible tableau Énergie), état (production / alarme / arrêt), rendement de conversion, et **limite de puissance** réglable (500 W min.).

## Crédits

Le décodage du protocole SOSSEN et la gestion du flux poussé viennent de [caveman2024/sossen-ha](https://github.com/caveman2024/sossen-ha) (MIT).
