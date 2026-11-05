# PaperPilot AI — simulation CFD uniquement

PaperPilot analyse les CFD démo Capital.com sur actions, ETF, indices, devises et matières premières, selon les contrats disponibles et vérifiés. Il simule des positions LONG et, si autorisé, SHORT. Aucun ordre n’est envoyé à un courtier. Les actions détenues directement et les futures de bourse ne sont pas intégrés.

La cryptomonnaie a été retirée du scanner et du portefeuille actif. Les clôtures CFD et leurs résultats restent conservés : les compteurs ne sont pas remis à zéro. Le bilan affiché porte désormais sur les CFD uniquement ; il ne représente pas le résultat historique de l’expérience incluant les cryptomonnaies. Voir [le détail du retrait](docs/CFD_ONLY.md).

## Utiliser le robot

1. Dans **Paramètres → Connexion CFD démo**, connectez l’e-mail du compte Capital.com démo, la clé API démo et son mot de passe API personnalisé. Ce dernier est le mot de passe créé pour l’API. Ne transmettez pas ces secrets dans le chat.
2. Vérifiez le statut de connexion, les contrats validés et la présence de la clé OpenAI côté serveur. Le scanner CFD est le seul scanner activé.
3. Enregistrez les paramètres de risque, puis cliquez sur **Démarrer**. Le robot attend les nouvelles clôtures M5 et vérifie le contexte H1 ; il ne crée aucun trade rétroactif.
4. Consultez **Recherche d’occasions**, les propositions IA et les motifs de refus. Un signal ou une proposition n’est pas une exécution. Les positions effectivement ouvertes apparaissent dans le portefeuille.
5. Suivez les clôtures et exportez les CSV. Attendre reste une décision possible : aucune fréquence de trades ni aucun gain n’est garanti.

Les cotations proviennent de l’environnement démo du fournisseur. Les transactions restent locales et simulées ; elles ne reproduisent pas une exécution de courtier.

## Capital, risque et stratégie

L’unité comptable existante reste **USDT**, avec un capital initial de 900 USDT virtuels. Les contrats CFD retenus sont cotés en USD ; les conversions USD/USDT restent comptées. Cette unité comptable ne signifie pas que des cryptomonnaies sont négociées.

Par défaut, le portefeuille autorise trois positions simultanées, avec 1 % de risque habituel, au plus 1,4 % par trade et 2 % de risque cumulé estimé aux stops. La quantité dépend de la distance du stop structurel. Le ratio net minimal reste de 2:1 ; un objectif n’est pas éloigné artificiellement pour l’atteindre. Les gaps peuvent dépasser le risque estimé.

Les profils Classique, v1 et v2 restent sélectionnables. Leurs résultats sont attribués au profil enregistré lors de chaque entrée. V2 conserve les entrées structurelles et vise une sortie des CFD à 20:45 UTC en semaine, avec nouvelles entrées avant 20:30 UTC ; les protections et l’invalidation H1 confirmée restent prioritaires. Une fermeture de marché peut retarder la sortie à la première cotation fraîche négociable. Les autres profils conservent leurs règles existantes.

Les prix d’entrée, stops et objectifs proviennent des observations M5/H1. Le moteur vérifie la structure, le spread, les coûts, la marge et tous les plafonds au cours final. L’IA sélectionne un plan admissible ou attend ; elle ne doit pas inventer les niveaux de prix. Le levier n’augmente pas les plafonds de risque.

Le spread, le glissement, les commissions configurées et le financement estimé sont pris en compte. Les taux fournisseur vérifiables sont prioritaires ; une estimation de repli est utilisée sinon. Cette simulation n’est pas une reproduction exacte du calendrier de débit du courtier.

Un premier bilan à 50 clôtures est un point de revue, pas une validation statistique. Le retrait des cryptomonnaies ne démontre ni que la stratégie fonctionne exclusivement sur CFD, ni qu’elle est rentable sur CFD. Voir [la stratégie v2](docs/STRATEGY_V2.md), [les niveaux structurels et le risque](docs/STRUCTURE_PORTFOLIO.md) et [les cohortes de résultats](docs/STRATEGY_REVIEW.md).

## Installation locale

1. Installez Python 3.11 ou plus récent.
2. Copiez `.env.example` vers `.env`, puis renseignez la clé OpenAI côté serveur. Sur Render, utilisez la variable `OPENAI_API_KEY` du service. L’API OpenAI est facturée séparément de l’abonnement ChatGPT.
3. Sur Windows, lancez `start_windows.bat`. Sur Mac/Linux, lancez `bash start_mac_linux.sh`. Le lanceur installe les dépendances au premier démarrage.
4. Ouvrez `http://localhost:8000`. Le robot commence à l’arrêt. Configurez Capital.com démo dans les paramètres et vérifiez les statuts avant de démarrer.

Les données du portefeuille sont persistées. La reprise conserve les CFD ouverts, leurs protections, les paramètres et les résultats. Le serveur doit rester allumé, connecté et alimenté en données : aucun stop virtuel ne fonctionne lorsque le programme est arrêté. L’hébergement et les appels OpenAI peuvent être facturés séparément.

| Commande | Effet |
| --- | --- |
| Démarrer | Analyse les nouvelles clôtures et autorise les entrées admissibles. |
| Pause entrées | Bloque les nouvelles entrées et conserve la protection des positions. |
| Arrêter les analyses | Arrête les analyses ; les protections existantes continuent tant que le serveur fonctionne. |
| Réinitialiser le portefeuille | Action distincte avec confirmation : efface toute la simulation et rétablit 900 USDT virtuels. Elle n’est pas utilisée pour retirer les cryptomonnaies. |

Les documents plus anciens décrivent l’évolution du projet et peuvent mentionner le scanner Spot historique. Le périmètre actif de cette version est défini ici et dans [CFD_ONLY.md](docs/CFD_ONLY.md).
