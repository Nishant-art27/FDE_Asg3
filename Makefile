PY ?= python3.11
VENV = .venv
BIN = $(VENV)/bin

.PHONY: setup verify start api eval eval-public test clean

setup:            ## create venv + install deps
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -r requirements.txt
	@[ -f .env ] || cp .env.example .env
	@echo "Now put your GROQ_API_KEY in .env"

verify:           ## starter pre-flight (no model needed)
	$(BIN)/python verify_setup.py

start:            ## one-command start: mock vendor-risk API + UI
	$(BIN)/python run_local.py

api:              ## mock vendor-risk API only
	$(BIN)/python run_local.py --no-ui

eval:             ## same 10 cases through both architectures -> evals/results_comparison.csv + docs/evaluation_results.md
	$(BIN)/python evals/compare_architectures.py --cases all

eval-public:      ## the starter's public harness, both architectures (needs the API: make api)
	$(BIN)/python evals/run_public_evals.py --architecture single
	$(BIN)/python evals/run_public_evals.py --architecture staged

test:             ## unit + offline end-to-end tests (no model calls)
	$(BIN)/python -m pytest -q

clean:
	rm -rf .pytest_cache $(shell find . -name __pycache__ -not -path "./.venv/*")
