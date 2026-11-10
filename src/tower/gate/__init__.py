"""gate : API HTTP unique de la Tower (site public, extension, bot, futurs clients).

Aucun client n'accède directement à la base : tout passe par ici. gate est sans état
et se met à l'échelle en ajoutant des réplicas Railway.
"""
