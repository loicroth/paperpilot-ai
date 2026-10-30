# Architecture de la V1

Un serveur Python FastAPI et une boucle asynchrone, une base SQLite et une interface HTML/CSS/JavaScript locale sans dépendances CDN. Aucune infrastructure supplémentaire n'est nécessaire.

## Flux d'une décision

1. Binance fournit `exchangeInfo`, `klines`, `bookTicker` et l'heure serveur depuis `https://data-api.binance.vision/api/v3/`.
2. L’heure serveur Binance détermine les bornes M5/H1 en millisecondes UTC. Le moteur conserve uniquement les bougies clôturées et calcule EMA, RSI, MACD, ATR, volumes et structure H1/M5. Il identifie les configurations autorisées.
3. À chaque nouvelle clôture M5, le moteur vérifie les coûts et la géométrie de tous les candidats techniques dans un délai borné avant de remplir la présélection IA. Un appel de gestion cible une position en rotation (schéma HOLD/SELL). Pour une entrée, l'IA choisit un identifiant de plan de marché ou WAIT ; le moteur compile les niveaux inchangés. Les appels partagent les plafonds API existants. Sans plan vérifiable, aucun appel d'entrée n'est facturé.
4. Une validation stricte vérifie la réponse. Le moteur de risque, indépendant du modèle, contrôle fraîcheur, admissibilité, nombre de positions, solde, frais, glissement, ratio net, stop, montant et pas de quantité Binance.
5. `PaperExecutionEngine` simule un achat sur ask et une vente sur bid, avec glissement défavorable. Les écritures du portefeuille et de l'historique sont persistantes et protègent contre les doublons.
6. La surveillance des cotations protège toutes les positions plus souvent que les analyses M5. Un rattrapage M1 par position après interruption signale les hypothèses ou l'incertitude ; en cas d'ambiguïté stop/objectif, le stop est prioritaire.

Les décisions et erreurs sont conservées pour inspection. L'interface affiche les données existantes uniquement ; elle ne remplit jamais un historique fictif.

## Confiance et limites

- Le modèle ne reçoit aucune capacité d'envoi d'ordres. Il propose des décisions ; les règles déterministes peuvent les refuser.
- Le budget API est réservé avant l'appel. Une erreur, un format incomplet, un refus, une absence de clé ou une incompatibilité de modèle produit WAIT.
- Un signal M5 consommé ne peut pas créer deux achats après rafraîchissement ou redémarrage.
- Le statut « ACTIVE » décrit l'état opérationnel ; il ne prédit pas une performance.
- Le risque habituel est de 1 %, plafonné à 1,4 % par trade et à 2 % cumulé par défaut. Ces budgets correspondent aux pertes estimées avec les hypothèses d'exécution ; les gaps ou interruptions peuvent les dépasser.
- Les niveaux sont extraits indépendamment du modèle. Le classique conserve le premier obstacle M5/H1. Le setup pullback du profil expanded_v1 protège sa zone M5 exacte et vise le premier obstacle H1, avec les niveaux M5 intermédiaires visibles pour l'analyse. Chaque setup possède sa borne économique ; aucune borne d'un autre setup n'est empruntée. Une impossibilité d'atteindre 2R net produit un refus, sans modification d'objectif pour fabriquer le ratio. Voir STRATEGY_PROFILE.md.
- Le risque cumulé est mesuré du cours observé jusqu'aux stops, sans compenser les pertes possibles par des gains latents. Des données de position absentes ou périmées bloquent les nouvelles entrées.
- La migration SQLite v3 sauvegarde la base existante puis remplace l'unicité globale par une unicité du symbole ouvert. Les anciennes positions et leur historique sont conservés.
- La perte quotidienne utilise le portefeuille de début de journée UTC ; un dépassement suspend les entrées jusqu'au jour suivant.
- Le spread REST est observé à réception : `bookTicker` ne fournit pas d'horodatage d'événement. La fraîcheur mesure l'observation serveur, pas une garantie de liquidité future.
- SQLite et le verrou d'instance imposent un seul moteur sur un même dossier de données. Ne lancez pas plusieurs workers Uvicorn.

## Sécurité

La clé OpenAI reste dans l'environnement du serveur et ne fait partie ni des réponses API, ni des exports. Le serveur écoute localement par défaut. Les mutations ont une protection CSRF et un déploiement non local exige un mot de passe ; HTTPS reste à fournir par l'hébergement.

`LiveExecutionEngine` existe uniquement comme frontière architecturale verrouillée. Aucun paramètre de cette V1 ne peut l'activer et aucune clé Binance n'est lue. Une future version réelle devra traiter les ordres protégés acceptés par Binance, les exécutions partielles, la réconciliation, les interruptions et l'arrêt d'urgence, puis être testée et faire l'objet d'une autorisation explicite.

