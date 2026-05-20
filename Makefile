# Match the CI matrix in .github/workflows/ci.yml.
PYTHON_VERSIONS := 3.12 3.13 3.14

# Project tooling overrides (defaults assume mypy + plain ruff format).
TYPECHECK     := pyrefly check
UV_SYNC_FLAGS := --dev

include Makefile.uv
