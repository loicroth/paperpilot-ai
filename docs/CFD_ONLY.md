# Passage au portefeuille CFD uniquement

Cette version retire les marchés crypto du robot et du bilan actif. Elle conserve les trades CFD, les positions CFD encore ouvertes, leurs protections, leurs frais historiques et leurs résultats. Elle ne remet pas les compteurs CFD à zéro et n’active aucune exécution réelle.

## Périmètre de trading

Le scanner utilise uniquement les contrats CFD démo Capital.com autorisés : actions, ETF, indices, devises et matières premières. Les cryptomonnaies sont exclues, y compris un éventuel contrat crypto fourni sous forme de CFD. Les futures de bourse et les actions détenues directement ne sont pas intégrés.

Le périmètre est imposé par le serveur. Les anciens réglages Spot, une requête de paramètres modifiée ou un ancien formulaire ne peuvent pas réactiver ce marché. Les contrôles finaux du moteur et de l’exécution refusent les instruments hors périmètre.

La connexion Capital.com démo reste nécessaire. Les sens LONG et SHORT, les profils de stratégie, les limites par position et par portefeuille, les règles de stop et de cible restent disponibles. Aucun achat ni aucune clôture réelle chez le fournisseur ne découle de cette migration.

## Retrait de l’historique crypto

Au premier démarrage de cette version, une migration unique prépare une sauvegarde privée de la base avant toute suppression. Les anciens trades et décisions crypto ainsi que les relevés mixtes sont retirés de la base active. La sauvegarde ne peut pas être téléchargée depuis le tableau de bord.

La trésorerie virtuelle est corrigée de façon comptable : les résultats nets des clôtures crypto sont annulés, et le coût d’origine d’une éventuelle position crypto ouverte est restitué. Une position crypto n’est pas transformée en un trade CFD ni en une clôture gagnante. Les marges et les résultats CFD sont conservés. Le traitement est idempotent : un redémarrage ne recrédite pas une seconde fois les mêmes montants.

Les plafonds, la consommation API et les protections CFD ne sont pas réinitialisés. La protection de perte journalière CFD est conservée de façon prudente si les anciennes données mélangées ne permettent pas une reconstruction exacte.

## Lire les nouveaux compteurs

Le nombre de clôtures, les gains et pertes, le taux de réussite, les résultats par actif et par jour ainsi que les CSV portent sur les CFD conservés. Les cohortes continuent d’utiliser le profil enregistré à l’entrée ; un ancien trade CFD n’est pas réattribué au profil actuel.

Les anciens relevés de capital mélangeaient les effets CFD et crypto. Ils ne permettent pas de reconstruire honnêtement la valeur intrajournalière d’un portefeuille CFD isolé. La courbe de capital et son drawdown repartent donc des nouveaux relevés après le retrait, avec la date indiquée dans le tableau de bord. Les clôtures CFD antérieures restent incluses dans les résultats réalisés et les cohortes. Le drawdown calculé seulement sur des clôtures dans une cohorte est une mesure distincte du drawdown de la valeur totale du portefeuille.

L’unité comptable reste USDT pour préserver la continuité des CFD existants. Les prix de contrats restent en USD et la conversion USD/USDT est conservée. Le comparatif de conservation BTC a été retiré.

Ce bilan CFD exclut volontairement les résultats crypto retirés. Il ne constitue pas une affirmation sur le profit global de l’expérience passée. Il ne démontre ni la rentabilité future, ni une incompatibilité générale de la stratégie avec les cryptomonnaies. La performance sur CFD doit être évaluée sur des résultats suffisants, frais et pertes inclus.
