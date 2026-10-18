.PHONY: install lint fmt types test check web run scan paper pnl

install:
	uv sync
	cd web && npm install

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff check --fix src tests
	uv run ruff format src tests

types:
	uv run mypy

test:
	uv run pytest -q

web:
	cd web && npm run check && npm run build

check: lint types test
	cd web && npm run check

run:
	uv run pmtrader run

scan:
	uv run pmtrader scan

pnl:
	uv run pmtrader pnl --period today
