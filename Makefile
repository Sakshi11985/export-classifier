# Convenience targets. Everything here is also documented as plain commands in README.md.
.PHONY: env up down scale test eval eval-baseline ingest load stack

env:            ## create .env from the documented example (never overwrites)
	@test -f .env || cp .env.example .env
up: env         ## docker compose up (build + start everything)
	docker compose up --build
scale: env      ## three agent workers (A7)
	docker compose up --build --scale worker=3
down:
	docker compose down
ingest:         ## (re)build the index inside the running stack; idempotent
	docker compose run --rm ingest
reembed:        ## re-embed everything without rebuilding the image or volume
	docker compose run --rm ingest python -m app.corpus.ingest --reembed
test:           ## full test-suite on the host (needs redis-server and a venv: see README 'Run the tests')
	python -m pytest -q
eval:           ## evaluation set against the full agent
	PYTHONPATH=. python -m app.evaluation.run_eval --label after
eval-baseline:  ## same set, single pass, no verifier (the 'before' of the before/after comparison)
	PYTHONPATH=. python -m app.evaluation.run_eval --label before --strategy baseline_single_pass --no-verifier
	PYTHONPATH=. python -m app.evaluation.run_eval --compare before after
load:           ## locust at several concurrency levels against http://localhost:8000
	scripts/run_loadtest.sh run "1 5 10 20 40" 60s
stack:          ## no-Docker local stack used for development (redis, chroma, stub, api, workers)
	scripts/local_stack.sh start 1
