# Suivi prospectif des stratégies

Le tableau de bord et le journal privé `PAPER_PERFORMANCE` affichent les résultats du profil enregistré à l'ouverture de chaque trade, en séparant Crypto Spot et CFD démo. Les données opérationnelles et les résultats personnels restent dans SQLite et les journaux privés, pas dans le dépôt source.

## Premier bilan à 50 clôtures

Le compteur vise un premier point de contrôle à 50 trades clôturés du profil courant. Ce seuil n'est ni une preuve statistique ni une validation de rentabilité. Il ne modifie pas les décisions ou les limites du robot et ne déclenche aucun ordre. Le nombre de jours d'entrée UTC est affiché, car plusieurs trades corrélés d'une seule séance ne constituent pas autant de situations de marché indépendantes.

Les anciens trades sans profil restent dans le bilan global et sont exclus du compteur du profil courant. Une position ouverte n'est pas une clôture. Changer le profil actif ne réattribue pas l'historique. Les réglages doivent rester stables pendant l'expérience ; toute nouvelle version de stratégie doit recevoir un profil distinct pour éviter de mélanger ses résultats. Le profil est une étiquette de version, pas une empreinte automatique de tous les paramètres.

Les métriques incluent résultat net, gain/perte moyens, facteur de profit, résultat moyen en R, coûts, plus longue série perdante et drawdown des résultats clôturés en USDT. Les frais et le financement sont déjà inclus dans le résultat net et ne sont pas soustraits deux fois. Le drawdown de cohorte se limite aux résultats clôturés : il ne remplace pas le drawdown global incluant les positions ouvertes. Le facteur de profit reste nul/indisponible en JSON s'il n'existe aucune perte, sans produire une valeur Infinity non standard.

`PAPER_PERFORMANCE` est émis au démarrage puis quand le nombre de clôtures, la dernière clôture ou le profil changent. Il ne requiert aucun appel IA ni accès public supplémentaire. Le tableau de bord se met à jour via son API authentifiée existante.

## Diagnostic des refus API

Un code HTTP 429 ne signifie pas toujours une limite temporaire de débit. Le client distingue les codes documentés de crédit épuisé, plafond de dépenses du projet/de l'organisation, quota d'utilisation et limitation de débit. Un refus non reconnu reste explicitement indéterminé. Le message brut du fournisseur et ses identifiants ne sont jamais copiés dans les diagnostics.

Référence vérifiée : https://developers.openai.com/api/docs/guides/error-codes

Cette modification conserve WAIT sur erreur. Elle ne change ni modèle ni budget, n'ajoute aucun retry et ne relève aucun plafond. L'état d'erreur peut rester visible entre deux appels tandis que les scans sans candidat continuent : il ne prouve pas un refus à chaque minute. Un crédit épuisé ou un plafond de facturation ne se résout pas en changeant la stratégie ; l'accès API doit être rétabli dans le compte concerné.

## Validation et invariants

Tests sur les cohortes, profils historiques/inconnus, positions ouvertes, seuil 49/50, chronologie du drawdown réalisé, coûts nets et dénominateur des ratios R. Tests HTTP du tableau de bord et erreurs API 429 avec codes connus, corps malformés et texte fournisseur confidentiel.

Aucun changement aux critères d'entrée, règles de gestion, niveaux SL/TP, dimensionnement, plafonds de risque, historique ou mode simulation. Les tests techniques ne constituent pas une preuve de rentabilité.
