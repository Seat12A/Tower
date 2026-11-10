"""hangar : worker générique qui exécute les services enfichables.

Un réplica = une boucle asyncio, une connexion LISTEN, un petit pool SQL. On scale
en ajoutant des réplicas (même file) ou des services Railway dédiés (``TOWER_QUEUES``).
"""
