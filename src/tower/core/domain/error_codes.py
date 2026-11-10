"""Catalogue des codes d'erreur de la Tower.

Format : ``TWR-<DOMAINE>-<NNN>``. Un code est **stable** : on ne le renumérote ni ne
le réutilise jamais (les clients, le bot et les recherches de logs s'y fient). Pour
retirer un code, le marquer ``deprecated`` sans le supprimer.

- ``http_status`` : statut renvoyé par gate quand l'erreur remonte jusqu'à l'API.
- ``severity`` : niveau de log et seuil d'alerte (≥ error : incident publié).
- ``store`` : l'occurrence est enregistrée dans ``error_reports`` (agrégée par empreinte).
  Les erreurs client (4xx) ne sont pas stockées : elles sont seulement journalisées.

La page ``docs/errors.md`` est générée depuis ce fichier (``tower errors catalog``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["info", "warning", "error", "critical"].index(self.value)


@dataclass(frozen=True, slots=True)
class ErrorCode:
    code: str
    title: str
    http_status: int = 500
    severity: Severity = Severity.ERROR
    store: bool = False
    retryable: bool = False
    hint: str = ""
    deprecated: bool = False

    def __str__(self) -> str:
        return self.code


CATALOG: dict[str, ErrorCode] = {}


def _define(code: str, title: str, **kwargs: object) -> ErrorCode:
    if code in CATALOG:
        raise ValueError(f"Code d'erreur dupliqué : {code}")
    entry = ErrorCode(code, title, **kwargs)  # type: ignore[arg-type]
    CATALOG[code] = entry
    return entry


_INFO, _WARN, _ERR, _CRIT = Severity.INFO, Severity.WARNING, Severity.ERROR, Severity.CRITICAL

# --- GEN : général --------------------------------------------------------------
GEN_INVALID_INPUT = _define("TWR-GEN-001", "Entrée invalide", http_status=422, severity=_INFO)
GEN_NOT_FOUND = _define("TWR-GEN-002", "Ressource introuvable", http_status=404, severity=_INFO)
GEN_CONFLICT = _define(
    "TWR-GEN-003",
    "Modification concurrente (version obsolète)",
    http_status=409,
    severity=_INFO,
    hint="Relire la ressource puis renvoyer avec la nouvelle version.",
)
GEN_RATE_LIMITED = _define("TWR-GEN-004", "Trop de requêtes", http_status=429, severity=_INFO)

# --- AUTH : authentification et autorisations ----------------------------------
AUTH_INVALID_TOKEN = _define(
    "TWR-AUTH-001", "Token de service invalide ou révoqué", http_status=401, severity=_WARN
)
AUTH_ACTING_WITHOUT_TOKEN = _define(
    "TWR-AUTH-002",
    "X-Acting-Discord-Id fourni sans token de service",
    http_status=401,
    severity=_WARN,
)
AUTH_BAD_ACTING_ID = _define(
    "TWR-AUTH-003", "X-Acting-Discord-Id invalide", http_status=401, severity=_WARN
)
AUTH_UNKNOWN_STAFF = _define(
    "TWR-AUTH-004",
    "Membre de l'équipe inconnu ou inactif",
    http_status=403,
    severity=_WARN,
    hint="Ajouter le membre : tower staff <discord_id> --roles reviewer",
)
AUTH_FORBIDDEN = _define("TWR-AUTH-005", "Permission manquante", http_status=403, severity=_INFO)

# --- DOS : dossiers ------------------------------------------------------------
DOS_NOT_FOUND = _define("TWR-DOS-001", "Dossier introuvable", http_status=404, severity=_INFO)
DOS_INVALID_TRANSITION = _define(
    "TWR-DOS-002", "Transition de statut interdite", http_status=409, severity=_INFO
)
DOS_BAD_PUBLICATION_URL = _define(
    "TWR-DOS-003",
    "Lien de publication X invalide",
    http_status=422,
    severity=_INFO,
    hint="Format attendu : https://x.com/<compte>/status/<id>",
)
DOS_PUBLICATION_URL_REQUIRED = _define(
    "TWR-DOS-004", "Lien de publication X obligatoire", http_status=422, severity=_INFO
)
DOS_PUBLICATION_URL_UNEXPECTED = _define(
    "TWR-DOS-005",
    "Lien de publication accepté uniquement pour le statut publié",
    http_status=422,
    severity=_INFO,
)
DOS_BAD_PRIORITY = _define(
    "TWR-DOS-006", "Priorité hors bornes (-100..100)", http_status=422, severity=_INFO
)

# --- EVT : bus d'événements ----------------------------------------------------
EVT_BAD_CURSOR = _define(
    "TWR-EVT-001", "Curseur d'événements invalide", http_status=422, severity=_INFO
)
EVT_CONSUMER_FAILED = _define(
    "TWR-EVT-002",
    "Échec d'un consommateur d'événements (lot rejoué)",
    severity=_ERR,
    store=True,
    retryable=True,
    hint="Le curseur n'avance pas tant que le lot échoue : corriger le handler.",
)

# --- JOB : file de travaux -----------------------------------------------------
JOB_UNKNOWN_KIND = _define(
    "TWR-JOB-001",
    "Aucun handler pour ce type de job",
    severity=_ERR,
    store=True,
    hint="Le service n'est pas activé dans TOWER_SERVICES de ce réplica.",
)
JOB_EXHAUSTED = _define(
    "TWR-JOB-002", "Job en échec après la dernière tentative", severity=_ERR, store=True
)
JOB_PERMANENT = _define(
    "TWR-JOB-003", "Job en échec définitif (PermanentError)", severity=_ERR, store=True
)
JOB_LEASE_EXPIRED = _define(
    "TWR-JOB-004",
    "Bail expiré : worker perdu (OOM, arrêt brutal)",
    severity=_WARN,
    store=True,
    retryable=True,
    hint="Vérifier la mémoire du réplica et drainingSeconds sur Railway.",
)
JOB_TIMEOUT = _define(
    "TWR-JOB-005",
    "Job interrompu : délai d'exécution dépassé",
    severity=_WARN,
    store=True,
    retryable=True,
)
JOB_ATTEMPT_FAILED = _define(
    "TWR-JOB-006",
    "Tentative de job échouée, relance programmée",
    severity=_WARN,
    store=True,
    retryable=True,
)
JOB_STATUS_UPDATE_FAILED = _define(
    "TWR-JOB-007",
    "Impossible d'enregistrer le résultat d'un job",
    severity=_ERR,
    store=True,
    hint="Le reaper remettra le job en file à l'expiration du bail.",
)

# --- DB : base de données ------------------------------------------------------
DB_UNAVAILABLE = _define(
    "TWR-DB-001",
    "Base de données indisponible",
    http_status=503,
    severity=_CRIT,
    store=True,
    retryable=True,
)
DB_LISTENER_DOWN = _define(
    "TWR-DB-002",
    "Connexion LISTEN perdue (reconnexion en cours)",
    severity=_WARN,
    store=True,
    retryable=True,
)

# --- MNT : maintenance ---------------------------------------------------------
MNT_TASK_FAILED = _define("TWR-MNT-001", "Tâche de maintenance en échec", severity=_ERR, store=True)

# --- INT : interne ----------------------------------------------------------------
INT_UNEXPECTED = _define(
    "TWR-INT-001",
    "Erreur interne inattendue",
    severity=_ERR,
    store=True,
    hint="Chercher l'empreinte dans error_reports ou request_id dans les logs Railway.",
)
INT_SERVICE_CRASHED = _define(
    "TWR-INT-002", "Tâche de fond d'un processus arrêtée sur erreur", severity=_CRIT, store=True
)


def get(code: str) -> ErrorCode:
    return CATALOG[code]
