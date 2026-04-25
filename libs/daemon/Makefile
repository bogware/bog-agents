.PHONY: install test lint format help

install:
	uv sync --group test

test:
	uv run --group test pytest tests/ -q --timeout=30

lint:
	uv run ruff check bog_agents_daemon/
	uv run ruff check tests/ --ignore=ANN,S,ARG || true

format:
	uv run ruff format bog_agents_daemon/ tests/

help:
	@echo "install  - install dependencies"
	@echo "test     - run unit tests"
	@echo "lint     - run ruff linter"
	@echo "format   - run ruff formatter"
