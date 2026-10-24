# Reprise autonome et vérification opérationnelle — 24 octobre 2026

Le mode choisi par l'utilisateur est conservé après redémarrage : running, paused ou stopped. La reprise de running ne suffit jamais à ouvrir un ordre : une nouvelle bougie postérieure au démarrage, des cotations fraîches, la réconciliation, une réponse IA valide et les contrôles de risque restent obligatoires. Une erreur critique persiste la pause. Un arrêt manuel reste arrêté.

START_PAPER_ONCE est une commande d'administration explicite, consommée une fois dans SQLite. Une nouvelle valeur demande un démarrage paper au prochain lancement. La même valeur ne redémarre jamais un robot arrêté manuellement ensuite. Elle n'autorise aucun ordre réel.

Au démarrage, si une clé OpenAI est présente, une vérification authentifiée et structurée utilise une entrée sans marché et n'autorise que WAIT. Ce n'est pas une décision de trading, elle n'est pas ajoutée à l'historique et ne crée aucun ordre. L'appel est décompté du plafond quotidien API et consomme des tokens facturables. Les erreurs d'authentification/quota sont affichées explicitement, sans repli sur une stratégie artificielle. Aucun secret n'est journalisé.

Une ligne PAPER_STATE est émise chaque minute dans les journaux privés : mode, données disponibles, état IA, nombre de marchés examinés/candidats, date d'analyse et connexion CFD présente. Aucun solde, identifiant ou clé API n'est inclus. PAPER_AI_CHECK donne le résultat du contrôle de connexion.

Le scan attend au maximum30 secondes et s'arrête avant la fin de la fenêtre de fraîcheur. Les marchés prêts peuvent être évalués pendant qu'un marché lent est ignoré pour ce cycle. Les tâches restantes sont annulées. Les seuils de fraîcheur des ordres ne sont pas relâchés. Avec les CFD activés, la sélection IA est équilibrée entre familles prêtes pour ne pas comparer directement le volume OTC et celui d'une bourse crypto. Cela ne constitue pas une probabilité de gain ni une garantie d'opération.

202 tests réussis,2 tests réseau optionnels ignorés. Cas nouveaux : persistance des trois modes, commande ponctuelle et arrêt manuel, diagnostic sans secret, marché lent, représentation des familles, vérification IA sans ordre et erreur de quota. Le test de doublon après reprise conserve sa vérification d'unicité.

Les sources actions/CFD doivent toujours être connectées par le propriétaire du compte. Cette correction ne crée pas un accès aux données manquant, ne transforme pas des données différées en données actuelles et ne garantit aucun trade gagnant.
