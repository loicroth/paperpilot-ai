# Niveaux structurels et portefeuille — 26 octobre 2026

Cette version reste une simulation sur cours observés. Elle ne démontre pas une rentabilité et ne transmet aucun ordre de courtier.

## Entrées

- Les quatre configurations LONG exigent maintenant un contexte H1 aligné (EMA21 au-dessus d'EMA50, EMA21 non descendante, clôture au-dessus). Les configurations SHORT CFD reflètent ces conditions. Les retournements précoces sur H1 neutre sont exclus.
- Les références de prix viennent des bougies M5/H1 clôturées : pivots confirmés avec deux bougies de chaque côté et bornes de la consolidation précédant le signal. Chaque référence conserve son horizon et son heure de confirmation.
- Le SL doit dépasser le support pertinent pour un LONG, ou la résistance pertinente pour un SHORT, d'au moins 0,15 ATR M5. Il ne peut pas être éloigné arbitrairement du pivot. Un garde-fou extrême refuse une distance supérieure à 25 % du prix.
- Le TP doit rester avant le premier obstacle connu dans le sens du trade, avec 0,1 ATR de marge. Sans référence exploitable, le moteur attend. Le TP n'est pas prolongé pour fabriquer un ratio.
- La quantité, les frais, le glissement, le risque et le ratio sont recalculés avec la cotation fraîche finale. Le ratio net minimal reste 2R ; des niveaux cohérents mais un ratio insuffisant produisent un refus.
- L'ancienne borne maximale de 4 ATR pour le stop est supprimée. Un stop structurel plus large est possible ; il réduit la quantité à budget de perte identique.

## Risque et capital

Le risque habituel reste 1 %. L'IA peut proposer un autre budget, jusqu'au plafond de 1,4 % du capital par trade, avec une justification observable. Le pourcentage de distance du stop et le pourcentage de capital risqué sont deux mesures différentes.

Trois positions maximum par défaut, réglable de 1 à 6. Une seule position par symbole : aucun renforcement, moyennage ou achat/short simultané sur le même instrument. À chaque entrée, le capital Spot ou la marge CFD, frais inclus, est limité à `equity / max_positions` pour conserver des liquidités pour les autres places.

Le risque cumulé restant aux stops est plafonné à 2 % du capital par défaut (réglable jusqu'à 4 %). La limite journalière de 2 % reste un garde supplémentaire : le risque des positions ouvertes est déduit du budget journalier restant. Une cotation manquante, trop ancienne ou un stop déjà dépassé empêche une nouvelle entrée. Les gains latents ne compensent pas des pertes futures possibles dans ce calcul.

Ces risques sont estimés avec les hypothèses de simulation. Les gaps, fermetures ou interruptions peuvent augmenter une perte au-delà des budgets calculés.

## Gestion et persistance

Chaque position possède sa protection SL/TP. La gestion IA cible une position en rotation à chaque clôture M5, avec un schéma limité à HOLD/SELL. L'appel d'entrée est indépendant et exclut les symboles détenus. Les métadonnées et cotations sont recontrôlées avant une écriture atomique, avec un nouveau contrôle des places, du solde et du risque courant.

Le plafond existant de 288 appels et 2 millions de tokens par jour UTC n'est pas augmenté. Un cycle peut utiliser deux appels ; la gestion réserve son budget en premier. Une limite API atteinte bloque les nouvelles décisions IA mais les protections mécaniques restent indépendantes.

La migration SQLite v3 sauvegarde la base avant de remplacer l'unicité globale par une unicité du symbole ouvert. Les positions existantes, leur solde et leur historique sont conservés. Les anciennes positions conservent les niveaux enregistrés à leur entrée. L'invalidation structurelle reste un garde avant ouverture ; après ouverture, les sorties sont SL/TP, décision SELL ou protections CFD (marge/durée).

Le tableau de bord affiche toutes les positions et le risque cumulé restant. Les journaux privés `PAPER_TRADE_AUDIT` enregistrent les niveaux et résultats à l'ouverture/fermeture pour faciliter un audit précis, sans clés ni identifiants de connexion.

Les tests automatisés couvrent les niveaux, budgets, allocations, migrations, positions simultanées, flux indisponibles et protections ciblées. Ce sont des scénarios contrôlés, pas des résultats de trading.
