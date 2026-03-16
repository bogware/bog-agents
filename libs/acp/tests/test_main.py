from __future__ import annotations


def test_import_main_module() -> None:
    from bog_agents_acp import __main__  # noqa: F401
