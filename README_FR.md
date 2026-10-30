# Diagnostic des entrées et coûts CFD (28 octobre 2026)

Un retest M5 clôturé peut désormais fournir son support de stop, sans attendre la confirmation d'un pivot deux bougies plus tard. La référence protège à la fois le niveau cassé et l'extrême observé du retest ; le cas vendeur est symétrique. La marge ATR, le premier obstacle cible, les coûts, le ratio net minimal et les plafonds restent vérifiés. Aucun stop d'une position déjà ouverte n'est déplacé par cette extraction de références. Les journaux privés `PAPER_PLAN_REVIEW` incluent aussi les plans refusés, leur ratio net et leurs références, pour distinguer une insuffisance géométrique d'un refus IA ou d'un problème d'exécution.

La faisabilité est vérifiée pour tous les candidats techniques avant la limite de présélection IA. Les plans compatibles sont prioritaires ; un marché de moindre volume ne peut plus être masqué uniquement par des candidats impossibles. Le classement par volume et la diversité des familles restent appliqués entre plans compatibles. Les calculs de marges ATR utilisent des décimales avant l'arrondi au tick pour éviter de faux refus au voisinage de 2R.

Pour une entrée, l'IA choisit l'identifiant d'un plan fourni ou WAIT. Elle ne génère plus les prix d'entrée, stop, objectif et invalidation : ces niveaux viennent des observations et sont compilés par le moteur. La cotation finale, la structure, les coûts, les quantités et tous les plafonds sont encore vérifiés avant ouverture. Aucun ordre n'est forcé et aucun objectif n'est déplacé pour atteindre le ratio. Une décision d'entrée au-delà de 65 secondes après la clôture M5 est refusée sur l'horloge locale, même si sa cotation reste récente.

Le scanner distingue maintenant marchés vus, données réellement analysables, signaux techniques et faisabilité nette des candidats évalués. Chaque refus CFD indique le sens LONG/SHORT ou la donnée manquante. Les scans entièrement indisponibles restent enregistrés. Les journaux privés `PAPER_SCAN_AUDIT` et `PAPER_FEASIBILITY_AUDIT` rendent ces contrôles inspectables sans exposer les connexions.

Le catalogue remplace les contrats incompatibles au lieu de leur laisser occuper une place. La commission CFD est séparée de la commission Spot : 0 % par défaut conformément à la [tarification Capital.com](https://capital.com/en-int/ways-to-trade/fees-and-charges). Le spread observé et le glissement configuré restent comptés. Les taux directionnels `instrument.overnightFee` de l'[API fournisseur](https://open-api.capital.com/) sont utilisés lorsqu'ils sont vérifiables ; sinon l'estimation de repli reste appliquée. Les crédits éventuels sont ignorés. La réserve d'un jour et la sortie après 24 h sont conservées. Le financement est une estimation prorata, pas une reproduction du calendrier exact des débits du courtier. Les frais des positions historiques restent ceux de leur ouverture.

Les plafonds de risque et le ratio net minimal 2R ne sont pas abaissés pour obtenir des ordres. Une borne de faisabilité affichée n'est ni un ordre, ni une probabilité de gain.

# Niveaux de marché et portefeuille multi-positions (26 octobre 2026)

Les nouvelles entrées utilisent des stops justifiés par les pivots/supports/résistances M5/H1 et des objectifs avant le premier obstacle connu. Aucun objectif n'est construit pour fabriquer 2R : si les niveaux observés ne permettent pas le ratio net, l'entrée est refusée. Trois positions simultanées par défaut, risque habituel 1 %, maximum 1,4 % par trade et 2 % de risque cumulé aux stops. Les quantités diminuent lorsque le stop est plus éloigné. Le capital ou la marge par position est limité à une part du portefeuille (`equity / max_positions`) pour laisser des liquidités aux autres signaux. [Détails et limites](docs/STRUCTURE_PORTFOLIO.md).

# Extension multi-marchés

La version déployée conserve le portefeuille900USDT et son historique. Elle ajoute la simulation CFD LONG/SHORT (actions sous-jacentes,ETF,indices,forex,matières premières) avec levier plafonné, marge et financement estimé. Les actions détenues directement et les futures de bourse ne sont pas pris en charge.

Pour les CFD : Paramètres → Capital.com démo → connecter les trois identifiants API démo → activer le scanner CFD → enregistrer → Démarrer. Ne communiquez pas les secrets dans le chat. Aucun ordre courtier n'est envoyé. La source démo et les contrats accessibles doivent encore être validés avec votre compte.

Voir [docs/MULTIMARCHE.md](docs/MULTIMARCHE.md) pour les protections, coûts simulés, limites et tests. Le reste du guide couvre l'installation locale et la crypto Spot.

# PaperPilot AI — Binance Spot, portefeuille virtuel

Application locale Python : vrais cours Binance, décisions OpenAI, portefeuille fictif de **900 USDT**. Aucune clé Binance, aucun ordre réel. Le moteur réel est verrouillé dans cette version.

## Démarrer en quelques minutes

1. Installez **Python 3.11 ou plus récent** depuis [python.org](https://www.python.org/downloads/). Sur Windows, cochez **Add Python to PATH**.
2. Décompressez complètement le ZIP dans un dossier personnel. Copiez `.env.example` sous le nom `.env` dans ce dossier et renseignez `OPENAI_API_KEY` avec votre clé API OpenAI. Ne partagez jamais ce fichier. Sur Windows, activez l’affichage des extensions pour éviter `.env.txt`. Sur Mac, **Cmd+Maj+.** affiche les fichiers commençant par un point ; modifiez `.env` en texte brut, pas en document RTF.
3. Windows : double-cliquez sur **start_windows.bat**. Mac/Linux : ouvrez un terminal dans le dossier puis lancez **`bash start_mac_linux.sh`**. Le premier lancement installe automatiquement les dépendances.
4. Ouvrez **http://localhost:8000**. Le robot démarre à l'arrêt. Vérifiez les statuts des données et de l'IA, puis cliquez sur **Démarrer la simulation**.

Sans clé, le tableau de bord reste accessible et affiche **IA non configurée**. Aucun trade n'est inventé. Pour ajouter ou remplacer la clé : modifiez `.env`, puis redémarrez le serveur. **L'API OpenAI est facturée séparément de l'abonnement ChatGPT.** Créez votre clé et un budget projet sur [OpenAI Platform](https://platform.openai.com/).

Gardez l'ordinateur allumé, connecté à Internet et le programme ouvert. Une mise en veille interrompt les cotations et les protections virtuelles.

## Première session avec 900 USDT virtuels

- Gardez les paramètres initiaux : BTC/USDT, ETH/USDT, SOL/USDT ; risque habituel 1 %, maximum 1,4 % par trade ; trois positions au maximum, avec 2 % de risque cumulé ; perte journalière maximale 2 % ; ratio net estimé minimal 2:1.
- Cliquez sur **Démarrer la simulation** et attendez une **nouvelle clôture M5**. Les bougies historiques initialisent les indicateurs ; elles ne génèrent aucun trade rétroactif.
- Vérifiez l'heure de la dernière cotation, les spreads et la dernière analyse. Ouvrez une décision pour lire les indicateurs, l'explication IA et le motif d'un éventuel refus.
- **WAIT/ATTENDRE est normal.** Le robot peut rester plusieurs heures sans acheter. Un appel IA refusé ou des données trop anciennes bloque les entrées.
- Après une clôture, consultez le résultat net, les frais et le détail du trade, puis exportez les CSV. Aucun résultat simulé ne constitue une preuve de rentabilité future.

La première session conserve ses données automatiquement. Après redémarrage, le portefeuille, la position, les décisions et les paramètres sont restaurés. La reprise vérifie les données manquantes avant de reprendre les entrées. Les clôtures M5/H1 sont déterminées avec l’heure serveur Binance ; l’horloge locale doit rester correctement réglée.

## Commandes et protections

| Commande | Effet |
| --- | --- |
| Démarrer | Active les analyses des nouvelles clôtures M5 et les entrées admissibles. |
| Pause | Suspend les nouvelles entrées, conserve les positions et leur protection. |
| Arrêter la simulation | Arrête les analyses, conserve les données ; les protections de position continuent tant que le serveur tourne. |
| Fermer la fenêtre / Ctrl+C | Arrête le programme : aucun stop virtuel ne peut fonctionner pendant cet arrêt. |
| Réinitialiser | Avec la confirmation explicite `REINITIALISER`, remet le portefeuille à 900 USDT et efface aussi toute position virtuelle ouverte. Une archive SQLite est créée dans `data/backups/avant_reset_…` ; exportez également vos CSV avant. |

Un stop virtuel n'est jamais un ordre hébergé chez Binance. Une panne, une absence de cotations ou une mise en veille peut augmenter la perte simulée au-delà du risque prévu. Le rattrapage M1 applique une hypothèse prudente quand l'ordre des touches stop/objectif est inconnu et le signale.

## Paramètres

Le modèle défini dans `.env` initialise une nouvelle base ; ensuite le choix enregistré dans le tableau de bord prévaut. Les paramètres de risque, marchés et modèle se modifient dans le tableau de bord et sont conservés dans SQLite. Les plafonds de sécurité ne peuvent pas être dépassés : risque par trade 1,4 %, six positions maximum (trois par défaut), risque cumulé maximal réglable à 4 % (2 % par défaut), perte quotidienne 2 %, ratio net minimal 2. Les frais supposés sont **0,1 % à l'achat et à la vente**, avec **5 points de base de glissement défavorable** et le spread bid/ask observé. Ajustez les frais si nécessaire : ils sont une hypothèse de simulation, pas la tarification de votre compte.

La taille d’une entrée est également réduite au risque journalier estimé restant, après déduction du risque des positions ouvertes. Une erreur critique interrompt les analyses jusqu’à un nouveau clic sur Démarrer. Le budget initial autorise au maximum **288 appels OpenAI et 2 000 000 tokens par jour UTC**, avec un maximum de **1 800 tokens de sortie par appel**. Le moteur réserve le budget avant un appel, y compris en cas d'erreur. Il compare les candidats dans un appel d’entrée et peut gérer une position en rotation dans un appel distinct ; les deux partagent ces mêmes plafonds. La gestion réserve le budget en premier et les SL/TP restent surveillés sans IA. Ce plafond de consommation n'est pas un plafond en dollars ; configurez également les limites et alertes du projet OpenAI. Le modèle est configurable ; une incompatibilité avec Structured Outputs bloque l'analyse et apparaît dans le tableau de bord.

Les cotations REST `bookTicker` ne portent pas d’horodatage d’événement : l’heure affichée correspond à leur réception selon l'horloge locale. L'heure Binance détermine les clôtures des bougies.

Les marchés ajoutés doivent être des paires Spot cotées en USDT disponibles chez Binance. Les filtres de quantité et de montant sont téléchargés depuis `exchangeInfo` et vérifiés avant chaque achat virtuel. Le contrôle notionnel paper utilise le prix simulé ; Binance peut utiliser un prix moyen et d’autres contrôles pour les ordres réels. Le portefeuille ne fait ni vente à découvert, ni levier, ni renforcement d'une position.

## Dépannage

- **IA non configurée** : contrôlez que `.env` existe et que `OPENAI_API_KEY` est renseigné, puis redémarrez. Ne collez pas la clé dans le navigateur.
- **Authentification / quota OpenAI** : vérifiez le projet, la clé et sa facturation dans OpenAI Platform. Aucun achat ne doit être créé sur une erreur API.
- **Données indisponibles** : vérifiez Internet, l'heure de l'ordinateur et l'accessibilité de Binance dans votre pays. Aucun prix de substitution n'est fabriqué.
- **Installation impossible** : l'installation des paquets nécessite Internet. Sous Linux, installez `python3-venv` si le message l'indique.
- **Port 8000 occupé** : fermez l'autre instance ou modifiez `PORT` dans `.env`. Une seule instance peut gérer le même dossier de données.
- **Aucun trade** : lisez les dernières décisions et les refus. La stratégie est volontairement sélective.

## Vérification locale

Dans le dossier de l'application, avec le Python de l'environnement virtuel :

```bash
# Mac / Linux
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m app.diagnostics --live
```

Sur Windows, remplacez `.venv/bin/python` par `.venv\Scripts\python.exe`. Les tests automatiques utilisent des scénarios contrôlés. Le diagnostic `--live` tente les endpoints publics réels de Binance et affiche une réussite ou une erreur pour chaque marché. L'accès OpenAI réel demande votre clé et du crédit API ; les tests sans clé vérifient le blocage et les réponses HTTP simulées.

Les vérifications effectivement exécutées à la livraison sont précisées dans [docs/VERIFICATION.md](docs/VERIFICATION.md).

## Sauvegarde et hébergement permanent

Les données se trouvent dans `data/` par défaut (`DATA_DIR` dans `.env`). Pour une copie cohérente, arrêtez le serveur puis sauvegardez tout le dossier de données. Conservez `.env` séparément et de manière privée. Pour restaurer, remettez le dossier de données avant de lancer l'application.

Un VPS permanent est possible ultérieurement : **un seul processus**, dossier de données persistant, sauvegardes, horloge synchronisée, reverse proxy **HTTPS**, mot de passe fort `DASHBOARD_PASSWORD`, identifiant HTTP `paperpilot`, pare-feu. Les en-têtes du proxy sont acceptés uniquement depuis la boucle locale. Le lancement refuse une écoute non locale sans mot de passe. N'ouvrez jamais simplement le port 8000 sur Internet. L'installation locale par défaut n'expose que l'adresse de bouclage ; un téléphone ne peut pas y accéder sans configuration réseau protégée.

## Sources techniques

- [Binance — données publiques sans authentification](https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only)
- [Binance — filtres de trading](https://developers.binance.com/docs/binance-spot-api-docs/filters)
- [OpenAI — Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Architecture et garanties](docs/ARCHITECTURE.md)

