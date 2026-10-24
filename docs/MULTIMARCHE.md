# Extension multi-marchés — 24 octobre 2026

## Fonctionnement livré

Un seul portefeuille virtuel initial de 900 USDT, une seule position globale : Binance Spot ou CFD Capital.com démo. Le capital existant n'est pas réinitialisé. Le mode réel reste inaccessible et aucun point d'accès d'ordre de courtier n'existe dans le code. Le client Capital a une liste fermée de requêtes GET de données et de POST de session sur le seul serveur DEMO.

Scanner crypto existant (30 marchés par défaut, 40 maximum) + scanner CFD optionnel (20 par défaut, 40 maximum). Catalogue CFD équilibré entre actions sous-jacentes, ETF, indices, devises et matières premières. Seulement contrats sans échéance, cotés USD, multiplicateur unitaire, tailles/précisions/marges vérifiables. Les autres contrats, marchés fermés ou cotations retardées sont explicitement exclus. Les marchés dépendent du catalogue réellement accessible au compte. Ce ne sont pas des actions détenues en direct ni des futures de bourse.

Chaque clôture M5 est comparée à H1. Achats LONG ou SHORT autorisés seulement sur les configurations structurelles admissibles pour leur sens. Les shorts et le levier n'existent que pour les CFD. Les cryptos restent au comptant. Maximum 4 candidats IA par défaut, plus la position détenue si nécessaire. Sans candidat ni position, WAIT local sans appel IA. Si le flux OTC ne fournit pas de volume informatif, la confirmation CFD repose sur les prix : les volumes absents sont explicitement marqués, aucun volume n'est inventé. La crypto conserve son filtre de volume.

## Marge, risques et coûts virtuels

Risque maximal prévu 1 % du portefeuille, perte journalière 2 %, ratio net minimal 2R, aucune moyenne à la baisse. Levier CFD simulé 2 par défaut, configurable de 1 à 5, plafonné aussi par les exigences du contrat. La marge immobilise des liquidités ; aucun produit de vente fictif n'est crédité pour un short. Les déficits après gap restent visibles, sans mise à zéro artificielle.

Bid/ask démo Capital, glissement défavorable, commissions configurées et financement estimé. Le financement initial 0,10 % du notionnel par jour est une hypothèse conservatrice de simulation, pas le tarif exact du courtier. Le calcul de risque réserve un jour de financement ; sortie prévue au premier cours négociable frais après 24 h. Une fermeture de marché ou une panne peut retarder cette sortie et accroître le coût. Un stop ne garantit jamais la perte maximale en cas de gap.

Les PnL USD sont convertis avec le bid/ask observé publiquement sur USDT/USD Kraken (inversé). Aucun taux fixe USD=USDT. La réponse REST est datée à réception, ce n'est pas un horodatage d'événement de l'exchange. Un flux de conversion indisponible bloque l'exécution CFD. Les gains sont convertis au taux défavorable reçu et les pertes/frais au taux de coût. Les dernières valorisations connues et leur caractère périmé survivent au redémarrage.

## Connexion et secrets

Dans Paramètres, carte Capital.com démo : identifiant e-mail, clé API et mot de passe API personnalisé. Créer soi-même ces identifiants dans le compte du fournisseur, puis les saisir uniquement dans l'application HTTPS authentifiée. La clé Capital n'est pas techniquement une clé read-only ; la sécurité de cette intégration est imposée par l'hôte DEMO et les chemins autorisés. Ne pas transmettre les identifiants en chat.

Le serveur vérifie une lecture authentifiée avant de conserver les identifiants. Fichier atomique `DATA_DIR/capital-demo-credentials.json` en mode0600 sur le disque persistant, non exposé à l'API, jamais inclus dans le dépôt, les exports ou les journaux. Les sessions restent en mémoire et sont renouvelées après expiration. Le navigateur ne conserve pas les champs après soumission. Variables CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_API_PASSWORD facultatives pour déploiement administré ; les valeurs non vides ont priorité sur le fichier sauvegardé.

Activer ensuite « Scanner CFD », enregistrer puis Démarrer. La connexion n'active pas automatiquement les analyses ni l'argent réel. Le remplacement d'une connexion est refusé si une position CFD existe. Les ordres en attente sont annulés lors d'un changement de configuration.

## Reprise et persistance

Migration SQLite v2 additive et transactionnelle, sauvegarde automatique avant migration d'une ancienne base dans DATA_DIR/backups. Données, solde, paramètres et positions conservés. Le serveur redémarre en pause. Les protections surveillent toute position existante dès que des cotations fraîches reviennent. Après une interruption, M1 est réconcilié prudemment (enveloppe bid/ask, priorité stop). Une fenêtre incomplète bloque les prises de profit favorables supposées ; les stops courants, appels de marge et limite de durée restent surveillés. Les périodes de fermeture peuvent empêcher une reconstitution complète. Ne pas revenir à l'ancienne version Spot avec une position CFD ouverte.

## Vérifications réellement réalisées

- 193 tests réussis, 2 tests réseau optionnels ignorés dans la suite standard.
- Achat/vente Spot, LONG/SHORT CFD, stop/objectif dans les deux sens, frais, marges, levier, appel de marge, financement, FX, déficit, doublons, migration, reprise, indisponibilités, coffre de connexion0600 et contrôles auth/CSRF.
- Parcours intégré scanner → proposition IA simulée → risque indépendant → ouverture SHORT virtuelle → protection → clôture.
- Serveur HTTP réellement lancé localement : health200, interface/API/statique authentifiés200, accès anonyme401,900USDT et CFD non configurés bloqués.
- JavaScript : syntaxe et références DOM vérifiées.
- Endpoint public /time Capital démo accessible. Authentification fournisseur, catalogue de ce compte et cotations CFD authentifiées NON testés faute d'identifiants. Appel OpenAI authentifié NON testé dans cette session ; les réponses de tests sont des fixtures, pas des résultats de trading.
- Rendu visuel navigateur non vérifié : Chromium indisponible et téléchargement échoué dans l'environnement.

## Limites

Les cotations Capital sont celles d'un environnement de démonstration et peuvent différer des prix/exécutions réels. Une démo inactive peut expirer ; nos trades locaux ne sont pas des opérations du compte démo du fournisseur. Aucun abonnement de données ni service payant supplémentaire créé par cette extension. Les coûts existants Render/OpenAI continuent selon l'usage.

Les actions/ETF détenus directement, les futures de bourse, les contrats nonUSD ou nonunitaires et le trading réel ne sont pas implémentés. Une indisponibilité réseau ou une fermeture de marché laisse les stops virtuels sans garantie d'exécution. Ces essais ne démontrent aucune rentabilité.

Sources : https://open-api.capital.com/ ; https://github.com/capital-com-sv/capital-api-postman ; https://help.capital.com/hc/en-us/articles/4415179146386-How-to-generate-an-API-key ; https://help.capital.com/hc/en-us/articles/4403988777874-What-s-the-difference-between-a-demo-and-a-live-trading-account ; https://help.capital.com/hc/en-us/articles/4403947617298-How-long-can-I-use-a-demo-account-for ; https://docs.kraken.com/api/docs/rest-api/get-ticker-information/
