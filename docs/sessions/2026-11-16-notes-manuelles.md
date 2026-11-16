# 2026-11-16 — Notes manuelles sans IA

## Objectif
Permettre à l'équipe d'écrire elle-même un fact-check (affirmation, verdict, note de
contexte, sources, fiche) sans passer par les modèles.

## Réalisé
- Domaine `core/domain/factcheck/manual.py` : validation (`TWR-FCK-049`, champ nommé),
  contexte par défaut selon le verdict, clé d'idempotence.
- Cas d'usage `core/usecases/manual_notes.py` : brouillon complet (affirmation, groupe
  N4, verdict `draft`, note `human`, case, `draft.ready`) si le post est en base, sinon
  job `factcheck.manual` (lecture plateforme, ingestion `manual`, même brouillon).
- `assembly.open_draft_for_content` (ouverture de case sans enquête) ; `review_draft`
  garde l'origine `human` des versions écrites par l'équipe.
- Gate : `POST /v1/factcheck/manual`. Bot : commande `/note` (options + formulaire),
  textes fr/en.
- Docs : `fact-check.md` (Notes manuelles), `api.md`, `intercom.md`, `decisions.md`,
  `errors.md` régénéré.

## Décisions
- Voir `docs/decisions.md`, « Notes manuelles sans IA » : brouillon relu (jamais de
  publication directe), relecteur rédige / éditeur publie, post absent lu par la
  plateforme, traduction automatique des versions manquantes conservée.

## Vérifications
- `ruff format --check`, `ruff check`, `mypy` strict : verts.
- Tests sans base : verts (domaine, gestionnaire du bot, i18n, isolation des imports,
  catalogue d'erreurs, déclaration du service).
- **Non exécuté** : tous les tests qui demandent PostgreSQL (dont les nouveaux tests du
  cas d'usage, du job et de la route gate), `alembic check`, smoke test des processus.
  L'installation de PostgreSQL/pgvector a été refusée dans l'environnement de la
  session. Aucune migration ajoutée (aucun changement de schéma).
- Aucun appel réel (ni modèle, ni API X).

## Points ouverts / suite
- Lancer `uv run pytest -q tests/test_manual_notes.py` puis la suite complète sur une
  base réelle.
- Option possible : désactiver la traduction automatique pour les notes manuelles.
- Saisie de l'affirmation en anglais pour les posts anglophones (aujourd'hui : français).
