# Architecture de la V1

Un serveur Python FastAPI et une boucle asynchrone, une base SQLite et une interface HTML/CSS/JavaScript locale sans dépendances CDN. Aucune infrastructure supplémentaire n'est nécessaire.

## Flux d'une décision

1. Binance fournit `exchangeInfo`, `klines`, `bookTicker` et l'heure serveur depuis `https://data-api.binance.vision/api/v3/`.
2. L’heure serveur Binance détermine les bornes M5/H1 en millisecondes UTC. Le moteur conserve uniquement les bougies clôturées et calcule EMA, RSI, MACD, ATR, volumes et structure H1/M5. Il identifie les configurations autorisées.
3. À chaque nouvelle clôture M5, un seul appel OpenAI compare les marchés et le portefeuille. Une réponse structurée contient BUY, SELL, HOLD ou WAIT, les niveaux, la confirmation et les risques.
4. Une validation stricte vérifie la réponse. Le moteur de risque, indépendant du modèle, contrôle fraîcheur, admissibilité, nombre de positions, solde, frais, glissement, ratio net, stop, montant et pas de quantité Binance.
5. `PaperExecutionEngine` simule un achat sur ask et une vente sur bid, avec glissement défavorable. Les écritures du portefeuille et de l'historique sont persistantes et protègent contre les doublons.
6. La surveillance des cotations protège la position plus souvent que les analyses M5. Un rattrapage M1 après interruption signale les hypothèses ou l'incertitude ; en cas d'ambiguïté stop/objectif, le stop est prioritaire.

Les décisions et erreurs sont conservées pour inspection. L'interface affiche les données existantes uniquement ; elle ne remplit jamais un historique fictif.

## Confiance et limites

- Le modèle ne reçoit aucune capacité d'envoi d'ordres. Il propose des décisions ; les règles déterministes peuvent les refuser.
- Le budget API est réservé avant l'appel. Une erreur, un format incomplet, un refus, une absence de clé ou une incompatibilité de modèle produit WAIT.
- Un signal M5 consommé ne peut pas créer deux achats après rafraîchissement ou redémarrage.
- Le statut « ACTIVE » décrit l'état opérationnel ; il ne prédit pas une performance.
- Le risque de 1 % est un plafond de perte théorique avec les hypothèses d'exécution retenues. Les gaps ou interruptions peuvent le dépasser.
- La perte quotidienne utilise le portefeuille de début de journée UTC ; un dépassement suspend les entrées jusqu'au jour suivant.
- Le spread REST est observé à réception : `bookTicker` ne fournit pas d'horodatage d'événement. La fraîcheur mesure l'observation serveur, pas une garantie de liquidité future.
- SQLite et le verrou d'instance imposent un seul moteur sur un même dossier de données. Ne lancez pas plusieurs workers Uvicorn.

## Sécurité

La clé OpenAI reste dans l'environnement du serveur et ne fait partie ni des réponses API, ni des exports. Le serveur écoute localement par défaut. Les mutations ont une protection CSRF et un déploiement non local exige un mot de passe ; HTTPS reste à fournir par l'hébergement.

`LiveExecutionEngine` existe uniquement comme frontière architecturale verrouillée. Aucun paramètre de cette V1 ne peut l'activer et aucune clé Binance n'est lue. Une future version réelle devra traiter les ordres protégés acceptés par Binance, les exécutions partielles, la réconciliation, les interruptions et l'arrêt d'urgence, puis être testée et faire l'objet d'une autorisation explicite.
