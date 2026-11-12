"""RAM guard: processes only load what they run (Railway bills memory).

Each check runs in a fresh interpreter so that modules imported by other tests do not
leak into ``sys.modules``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

HEAVY_SDKS = ("openai", "anthropic")


def _loaded_after(code: str, modules: tuple[str, ...]) -> list[str]:
    script = f"import sys\n{code}\nprint(','.join(m for m in {modules!r} if m in sys.modules))\n"
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=60
    )
    return [m for m in out.stdout.strip().split(",") if m]


def test_gate_does_not_load_llm_sdks() -> None:
    assert _loaded_after("import tower.gate.app", HEAVY_SDKS) == []


@pytest.mark.parametrize("services", [["ping"], ["ping", "ingest"]])
def test_hangar_does_not_load_llm_sdks_nor_fastapi(services: list[str]) -> None:
    code = (
        "import tower.hangar.main\n"
        "from tower.core.services.registry import load_services\n"
        f"load_services({services!r}).validate(lease_seconds=300)\n"
    )
    assert _loaded_after(code, (*HEAVY_SDKS, "fastapi", "httpx")) == []


def test_core_llm_does_not_load_sdks() -> None:
    code = "import tower.core.llm.router, tower.core.llm.prompts, tower.core.adapters.factory"
    assert _loaded_after(code, (*HEAVY_SDKS, "httpx")) == []


KNOWLEDGE_ADAPTERS = (
    "tower.core.adapters.knowledge.wikipedia",
    "tower.core.adapters.knowledge.wikidata",
    "tower.core.adapters.knowledge.brave",
    "tower.core.adapters.knowledge.legifrance",
    "tower.core.adapters.knowledge.insee",
    "tower.core.adapters.knowledge.factcheck_tools",
    "tower.core.adapters.knowledge.factory",
)
FACTCHECK_SERVICES = ("tower.services.factcheck", "tower.services.investigate")


def test_gate_does_not_load_factcheck_adapters_nor_services() -> None:
    code = "import tower.gate.app\ntower.gate.app.create_app"
    assert _loaded_after(code, (*KNOWLEDGE_ADAPTERS, *FACTCHECK_SERVICES)) == []


@pytest.mark.parametrize("services", [["ping", "ingest"], ["ping", "ingest", "radar"]])
def test_hangar_without_factcheck_loads_none_of_it(services: list[str]) -> None:
    code = (
        "import tower.hangar.main\n"
        "from tower.core.services.registry import load_services\n"
        f"load_services({services!r}).validate(lease_seconds=300)\n"
    )
    modules = (
        *HEAVY_SDKS,
        *KNOWLEDGE_ADAPTERS,
        *FACTCHECK_SERVICES,
        "tower.core.factcheck.schemas",
    )
    assert _loaded_after(code, modules) == []


def test_hangar_with_factcheck_loads_no_sdk_nor_httpx_at_start() -> None:
    code = (
        "import tower.hangar.main\n"
        "from tower.core.services.registry import load_services\n"
        "load_services(['ping', 'ingest', 'radar', 'factcheck', 'investigate'])"
        ".validate(lease_seconds=300)\n"
    )
    assert _loaded_after(code, (*HEAVY_SDKS, "httpx", "fastapi", *KNOWLEDGE_ADAPTERS)) == []


# Lot 4: embeddings and E3 stay out of gate and of the replicas without fact-check.
FACTCHECK_V1 = (
    "tower.core.llm.embeddings",
    "tower.core.adapters.openai_embedder",
    "tower.core.adapters.fake_llm",
    "tower.core.factcheck.training",
    "tower.core.domain.factcheck.router",
)
"""``core/factcheck/candidates.py`` (SQL only, no SDK) is reachable from gate through the
claims use cases: it is light and deliberately not listed."""


def test_gate_does_not_load_embeddings_nor_matching() -> None:
    code = "import tower.gate.app\ntower.gate.app.create_app"
    assert _loaded_after(code, FACTCHECK_V1) == []


@pytest.mark.parametrize("services", [["ping", "ingest"], ["ping", "ingest", "radar"]])
def test_hangar_without_factcheck_loads_no_lot4_module(services: list[str]) -> None:
    code = (
        "import tower.hangar.main\n"
        "from tower.core.services.registry import load_services\n"
        f"load_services({services!r}).validate(lease_seconds=300)\n"
    )
    assert _loaded_after(code, FACTCHECK_V1) == []


def test_hangar_with_factcheck_loads_no_embedder_at_start() -> None:
    code = (
        "import tower.hangar.main\n"
        "from tower.core.services.registry import load_services\n"
        "load_services(['factcheck', 'investigate']).validate(lease_seconds=300)\n"
    )
    loaded = _loaded_after(
        code,
        (
            "tower.core.adapters.openai_embedder",
            "tower.core.adapters.fake_llm",
            "tower.core.factcheck.training",
        ),
    )
    assert loaded == []
