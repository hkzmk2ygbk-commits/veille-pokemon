# Veille stock Pokémon 30e anniversaire

Contrôle horaire de 41 pages produits. Une notification est envoyée lors du passage à « disponible » ou « précommande ».

## Installation (GitHub Actions, gratuit)
1. Installer l'app **ntfy** (iOS/Android) et s'abonner à un topic secret, par exemple `pokemon-veille-7f3k9x2q`.
2. Créer un dépôt GitHub (public = minutes illimitées) et y pousser ce dossier, `.github/` compris.
3. Dans Settings > Secrets and variables > Actions, créer le secret `NTFY_TOPIC` avec le nom de ton topic.
4. Dans l'onglet Actions, lancer « Veille stock Pokémon » avec **Run workflow** pour tester.
5. Le lancement se fait ensuite automatiquement toutes les heures. Le tableau récapitulatif de chaque exécution est dans l'onglet Actions.

Option Telegram : ajouter aussi les secrets `TELEGRAM_TOKEN` et `TELEGRAM_CHAT_ID`.

## En local (meilleur taux de passage anti-bot)
    pip install -r requirements.txt
    NTFY_TOPIC=ton-topic python3 monitor.py            # exécution réelle
    python3 monitor.py --dry-run                       # test sans notification
    # crontab -e :
    7 * * * * cd /chemin/veille-pokemon && NTFY_TOPIC=ton-topic python3 monitor.py >> veille.log 2>&1

## Statuts
DISPO · PRECOMMANDE · INDISPO · INCONNU · BLOQUE (anti-bot) · ERREUR
