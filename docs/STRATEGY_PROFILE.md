# Profils de simulation — 30 octobre 2026

`expanded_v1` ajoute au profil classique une reprise après repli en M5, avec objectif sur la structure H1. Il est activé pour la nouvelle phase de paper trading demandée. Le choix est enregistré une fois en base, affiché dans les paramètres et conservé dans les décisions et les trades. Un redémarrage conserve le portefeuille, les positions, les limites et le mode marche/pause/arrêt.

## Reprise après repli

La dernière H1 clôturée doit être au-dessus de son EMA21, elle-même au-dessus de l'EMA50 ; l'EMA21 monte. Dans les trois M5 précédant le signal, il faut un contact avec la zone EMA21 précédente comprise entre −0,5 et +0,3 ATR, ainsi qu'un repli réel (bougie rouge ou clôture en baisse). La M5 signal clôture dans le sens de la reprise, au-delà du haut précédent, de l'EMA9 et de l'EMA21. Le momentum MACD s'améliore. RSI 40–76, ATR M5 0,03–3 %, extension maximale 2,5 ATR et volume au moins 0,8 fois sa moyenne restent vérifiés. Le traitement des volumes CFD manquants reste celui du scanner existant.

Le cas vendeur CFD est strictement symétrique. Le Spot reste acheteur uniquement. Aucune bougie en formation ne confirme une entrée.

Le stop protège le minimum des six bougies M5 comprenant le signal (maximum pour SHORT), puis applique la marge de 0,15 ATR et la distance minimale de bruit de 0,35 ATR. Un ancien pivot plus proche ne peut pas remplacer discrètement cette référence de zone.

L'objectif précède le premier niveau H1 opposé observé, extrait d'au plus 150 H1 déjà clôturées. Il n'est pas calculé comme un multiple de risque. Les niveaux intermédiaires M5 restent visibles pour l'analyse, mais ce profil de continuation ne les utilise pas tous comme plafonds automatiques d'objectif. Sans niveau H1 exploitable, ou sans 2R net, le plan est refusé. La classification des niveaux utilise le bid/ask observé du côté de sortie ; un glissement simulé ne peut pas faire sauter un obstacle proche pour choisir le suivant.

Chaque setup a sa propre borne économique. Une reprise ne peut pas emprunter la borne plus favorable d'un autre setup. L'IA choisit un identifiant de plan ou WAIT. La cotation finale, la structure, la taille et les limites sont revérifiées avant toute ouverture.

## Limites conservées

Risque habituel configuré 1 %, plafond 1,4 % par trade, plafond cumulé configuré 2 %, trois positions par défaut et perte journalière maximale 2 %. La quantité baisse si le stop s'éloigne. Frais, spread, glissement, financement, réserve d'un jour et durée CFD maximale restent ceux du moteur existant. Aucun ordre réel n'est activé.

`classic` reste disponible dans les paramètres pour reprendre les règles classiques. Les positions déjà ouvertes conservent leur plan et le profil enregistré à l'ouverture.

## Validation et limites de l'expérience

Les tests couvrent les références causales LONG/SHORT, les objectifs H1, les obstacles M5 visibles, les données futures/incohérentes, les frais, les limites de risque et une ouverture complète par l'adaptateur IA, le moteur de risque et SQLite.

Une comparaison sur 40 cryptos et 45 h 40 de bougies publiques donne 4 à 5 observations admissibles au classique et 6 à 7 avec ce profil, selon des scénarios de spread total de 0 ou 5 bps. Les deux observations supplémentaires concernent PUMP, déjà détenu par le robot à ces moments-là : elles ne démontrent donc pas deux exécutions supplémentaires. La fenêtre supplémentaire de 3 h 55 ne contient aucun plan admissible pour les deux profils.

Ces scénarios reconstruisent bid/ask autour des clôtures ; ce ne sont pas des cotations historiques exécutables. Ils évaluent les plans avec un portefeuille initialement vide, sans mesurer PnL, taux de réussite, concurrence des positions ou décisions IA réelles. Le jeu de développement a servi à la conception et ne constitue pas un test aveugle. Les données ne permettent aucune conclusion historique sur les CFD.

Ce profil est une expérience prospective en simulation. L'augmentation des exécutions effectives et la rentabilité restent à mesurer sur les nouveaux trades, sans confondre nombre de signaux et bénéfice.
