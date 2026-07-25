.PHONY: install test lint type format help

install:
	uv sync --group test

test:
	uv run --group test pytest tests/ -q --timeout=30

lint:
	uv run ruff check bog_agents_daemon/
	uv run ruff check tests/ --ignore=ANN,S,ARG
	$(MAKE) type

# V3-10: type-check the source package — the daemon was previously never
# type-checked. Tests use AsyncMock/MagicMock that ty cannot reason about, so
# they are gated by ruff + the suite itself (mirrors the SDK/CLI convention).
type:
	uv run --group test ty check bog_agents_daemon

format:
	uv run ruff format bog_agents_daemon/ tests/

help:
	@echo "install  - install dependencies"
	@echo "test     - run unit tests"
	@echo "lint     - run ruff linter + ty type check"
	@echo "type     - run ty type checker on the source package"
	@echo "format   - run ruff formatter"
