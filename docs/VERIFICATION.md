# Vérification de la livraison — 23 octobre 2026

**Résultat final : 122 tests réussis, 0 échec, 0 erreur, 0 test ignoré.**

Commande exécutée sous Python 3.12 / Linux :

```bash
PAPER_LIVE_TEST=1 python -m pytest -q --junitxml=docs/test-results.xml
```

Durée du dernier passage : 17,25 secondes. Le rapport machine figure dans `test-results.xml`.
Sans `PAPER_LIVE_TEST=1`, les deux vérifications réseau restent volontairement ignorées ; les 120 scénarios contrôlés s’exécutent sans clé ni connexion aux services.

| Périmètre | Tests réussis |
| --- | ---: |
| IA : schéma, réponses, refus, panne, clé absente | 32 |
| Moteur : cycles, comparaison des marchés, limites et concurrence | 15 |
| Risque, exécution virtuelle et SQLite | 30 |
| Indicateurs, filtrage et client Binance | 11 |
| Serveur réel + données Binance + verrou de seconde instance | 1 |
| Reprise après coupures, M1 et approximations prudentes | 10 |
| Interface HTTP, actions, sécurité et exports | 13 |
| Comptabilité, métriques, rollback et erreurs critiques | 10 |
| **Total** | **122** |

Deux tests ont réellement utilisé Binance : téléchargement des bougies M5/H1, bid/ask et contraintes de BTC/ETH/SOL ; puis démarrage d’un serveur HTTP, cotations des trois marchés, solde de 900 USDT, absence de transactions et refus d’une seconde instance. Aucun compte Binance privé n’a été utilisé.

Le diagnostic public réel a également réussi : `python -m app.diagnostics --live`. Le relevé joint `binance_verification.json` contient les données observées, pas des prévisions ni des trades. Il reste un relevé daté, et non une source de cotations pour le robot.

**Aucun appel OpenAI authentifié n’a été exécuté : aucune clé n’était disponible.** Le véritable adaptateur HTTP a été testé avec des réponses contrôlées, jusque dans un achat virtuel complet. Cela vérifie l’intégration locale, pas l’accès à votre projet OpenAI ni les résultats de trading.

Autres contrôles effectués : compilation Python, syntaxe JavaScript et Bash ; installation réelle des dépendances dans un environnement virtuel Linux neuf ; relancement sans réinstallation ; arrêt correct après échec d’installation. Pour cette vérification du lanceur, le sous-programme serveur était contrôlé ; le serveur réel a été vérifié séparément par le test réseau indiqué ci-dessus.

Non vérifiés dans cette session : lancement natif Windows/macOS, rendu visuel dans un navigateur (Chromium indisponible et téléchargement bloqué), fonctionnement durant plusieurs jours et rentabilité de la stratégie. Les fichiers HTML/CSS ont été vérifiés structurellement et les pages/actions HTTP testées.

## Tests reproductibles

`python -m pytest -q` vérifie avec des données et réponses HTTP contrôlées :

- Indicateurs techniques et absence de valeurs non finies.
- Réponses IA structurées, refus des réponses incomplètes ou contradictoires, absence de clé, erreurs HTTP et délais réseau.
- Achat virtuel, précision Binance, solde, commissions d'entrée et sortie.
- Stop-loss, take-profit et hypothèse conservatrice de rattrapage.
- Fonds insuffisants, risque, ratio, position unique et absence de doublon.
- Persistance des positions, paramètres et historique après réouverture.
- Routes du tableau de bord et actions autorisées, protection des accès et des secrets.

Ces scénarios utilisent volontairement des prix fictifs pour tester le logiciel. Ils ne deviennent jamais des trades du portefeuille utilisateur et ne prouvent aucune rentabilité.

## Contrôle des données réelles

`python -m app.diagnostics --live` tente de télécharger des bougies réellement clôturées de BTCUSDT, ETHUSDT et SOLUSDT depuis Binance. Le résultat dépend de l'accès réseau de la machine et de la disponibilité géographique du service. Une erreur doit apparaître explicitement ; le diagnostic ne remplace pas Binance par des cours inventés.

**Contrôle réel effectué avec succès le 23 octobre 2026 :** `exchangeInfo`, cotations bid/ask, heure serveur et bougies M5/H1 de BTCUSDT, ETHUSDT et SOLUSDT. Pour chaque paire, 149 bougies clôturées M5 et 149 bougies clôturées H1 ont été reçues ; les indicateurs ont été calculés. Le détail JSON est joint dans le dossier de vérification de la livraison. Aucun appel OpenAI authentifié ni aucune opération de portefeuille n’a été réalisé par ce diagnostic.

Dans le tableau de bord : vérifier les heures de cotation, les bougies M5/H1 de la décision et l'heure de dernière analyse après la prochaine clôture M5. Le lancement initial ne doit pas exécuter un historique de trades.

## Vérifications nécessitant la machine de l'utilisateur

- Appel OpenAI réellement authentifié avec une clé et une facturation actives.
- Accès Binance depuis la connexion de l'utilisateur.
- Installation native Windows ou macOS si l'environnement de construction n'utilise pas ces systèmes.
- Fonctionnement continu durant plusieurs jours, mises en veille, pannes réseau réelles et performance de la stratégie.

Une API indisponible ou une configuration manquante doit laisser l'interface consultable tout en bloquant les nouvelles opérations. Un statut d'erreur visible est préférable à une analyse fabriquée.
