set shell := ["bash", "-uc"]

default:
    @just --list

sync:
    uv sync --locked --group dev

unit:
    uv run --locked python -W error::ResourceWarning -m unittest discover -s scripts -p 'test_*.py'

lint:
    uv run --locked ruff check --preview scripts

fmt-check:
    uv run --locked ruff format --check scripts

typecheck:
    uv run --locked basedpyright scripts

dead-code:
    uv run --locked vulture

check: unit lint fmt-check typecheck dead-code
