# Sorties structurées en simulation — expanded_v2

## Problème et portée

La gestion précédente permettait une clôture à partir d'une justification libre de l'IA. Le gestionnaire recevait aussi des critères de nouvelle entrée, sans le plan d'ouverture complet. La disparition d'un candidat ou un repli ordinaire pouvaient ainsi être invoqués pour sortir sans invalidation définie. Une justification baissière ne constitue pas en elle-même une raison de clôturer une vente à découvert.

V2 conserve les critères d'entrée de v1, le choix du plan par l'IA, le stop structurel, l'objectif observé, 2R net minimal et tous les plafonds de risque. Aucune entrée n'est présentée comme validée par une étude de rentabilité. Aucun stop n'est supprimé, élargi après ouverture ou rapproché pour augmenter un ratio. La quantité reste adaptée au risque monétaire. Le mode réel demeure verrouillé.

## Politique gelée à l'ouverture

La politique dépend du profil enregistré à l'ouverture de la position, pas du réglage courant. Les positions des anciennes cohortes conservent leur gestion ; aucun résultat historique n'est réattribué. La migration de mise en service change une seule fois le profil v1 actif vers v2 et conserve un choix classique explicite. Elle ne touche ni capital, ni historique, ni mode marche/pause/arrêt, ni risque. Un choix ultérieur de v1 est conservé au redémarrage.

1. Les protections sur cotations fraîches restent prioritaires : stop, marge de maintenance et objectif. Les gaps et coûts restent visibles ; le risque estimé n'est pas une garantie de perte maximale en cas de gap.
2. Sans protection déclenchée, une sortie d'invalidation exige une H1 entièrement clôturée après l'ouverture : pour LONG, clôture < EMA21 < EMA50 et EMA21 descendante ; pour SHORT, clôture > EMA21 > EMA50 et EMA21 montante. Les égalités, une simple perte latente, une bougie M5 opposée ou l'absence de nouveau signal ne suffisent pas.
3. Le calcul est local, sans appel IA de gestion. Les données H1 doivent être finies, cohérentes, chronologiques, clôturées et à jour. En cas d'absence ou d'incohérence, HOLD et protection SL/TP indépendante. Une clôture réelle en simulation exige toujours une nouvelle cotation négociable, une identité de position inchangée et la réconciliation existante. La condition est contrôlée de nouveau avant la mutation.
4. Le plan d'origine est transmis sous une forme bornée : sens, setup, prix, invalidation et références structurelles. Aucune copie récursive du contexte historique. Les décisions identifient la politique locale ; une invalidation H1 clôturée porte le motif `H1_INVALIDATION_V2`.

La gestion H1 reste évaluée en rotation à raison d'une position par cycle M5 : une position peut donc attendre plusieurs cycles lorsque plusieurs trades sont ouverts. Les protections rapides sur cotation et la clôture quotidienne restent indépendantes de cette rotation et de l'IA. Une décision locale ne masque pas un échec de connexion ou de quota API.

## Fin de journée CFD

Les nouvelles entrées CFD v2 sont admises du lundi au vendredi UTC, avant 20:30 UTC. La vérification est faite par le moteur de risque et de nouveau à l'ouverture virtuelle pour empêcher un plan préparé auparavant de contourner l'échéance. La clôture quotidienne est visée à **20:45 UTC**, soit 21:45 en Suisse en hiver et 22:45 en été. La date d'ouverture UTC détermine l'échéance, même après un redémarrage ou un passage à minuit.

La règle utilise le premier cours frais négociable disponible à partir de cette échéance. Une suspension, un marché déjà fermé, un jour férié ou une panne peuvent retarder la sortie. Aucun cours de clôture passé n'est fabriqué. Il ne s'agit pas d'un calendrier de bourse ni d'une garantie de sortie avant le financement de chaque instrument. Les Crypto Spot conservent leur politique existante et ne sont pas soumises à ce cutoff CFD.

Les spreads, commissions et glissements restent comptés. Le financement CFD du simulateur reste une estimation conservatrice au prorata du temps, avec une réserve d'un jour à l'entrée ; le rollover exact du courtier n'est pas reproduit. Le passage en intrajournalier ne remet donc pas artificiellement les frais à zéro.

## Évaluation et limites

Comparer séparément les résultats v1/v2 et Crypto/CFD : résultat net, gain et perte moyens, résultat moyen en R, facteur de profit, drawdown et motifs de sortie. Des trades simultanés ou répétés sur des indices américains corrélés ne sont pas des observations indépendantes. Le jalon de 50 clôtures reste un point de contrôle et non une preuve statistique ou un permis de passer en réel.

Les tests techniques prouvent le respect des règles et de la comptabilité sur fixtures, pas un avantage de marché. Cette correction ne démontre pas que les positions précédemment coupées par l'IA auraient gagné. Il faut des données chronologiques hors échantillon et une observation prospective nette de coûts avant de conclure sur la rentabilité.

Une étude publiée d'Opening Range Breakout sur actions américaines utilise un stop et une clôture de séance, mais s'appuie sur le volume réel des actions et un historique quotidien. Les volumes de ticks des CFD ne permettent pas de la reproduire fidèlement. V2 n'est pas une réplication de cette étude et n'est pas décrite comme une « stratégie professionnelle gagnante ».

Références primaires consultées :

- CME, placement logique du stop et dimensionnement : https://www.cmegroup.com/education/courses/trade-and-risk-management/proper-position-size
- CME, critères explicites d'entrée et de sortie : https://www.cmegroup.com/education/courses/building-a-trade-plan/trading-strategies-in-your-trade-plan
- Zarattini, Barbon, Aziz (2024), étude ORB et ses données : https://www.alexandria.unisg.ch/server/api/core/bitstreams/3c2989c4-688d-4d78-8a71-f02690990d51/content
- Capital.com, financement et heure propre aux instruments : https://help.capital.com/hc/en-us/articles/360016641939-What-is-the-overnight-funding-fee
