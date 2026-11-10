"""Cas d'usage : la seule porte d'entrée pour modifier l'état métier.

Chaque cas d'usage reçoit une session (transaction ouverte par l'appelant) et un
acteur, vérifie les permissions, applique les règles du domaine, écrit l'audit et
publie les événements dans la même transaction.
"""
