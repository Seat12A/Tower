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
