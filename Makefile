# Everyday commands for running chase on the OptiPlex.  `make help` lists them.
SHELL := /bin/bash
COMPOSE ?= docker compose
CARD ?= mic
SINCE ?=
PRESET ?=
CRON_SCHEDULE ?= */15 * * * *
LOG ?= $(HOME)/chase-auto-update.log
REPO_DIR := $(shell pwd)

.PHONY: help up down restart deploy logs logs-alloy ps status watch backlog episodes violations \
        replay test-mic update install-cron uninstall-cron cron-log test

help: ## List the commands
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  make %-15s %s\n", $$1, $$2}'

up: ## Build (if needed) and start listener + alloy
	$(COMPOSE) up -d --build

down: ## Stop everything (the listener flushes its audio buffer first)
	$(COMPOSE) down

restart: ## Restart the listener (e.g. after editing config/chase.yaml)
	$(COMPOSE) restart listener

deploy: update ## Pull from git and redeploy now (same as the cron job)

logs: ## Follow the listener's events
	$(COMPOSE) logs -f --tail 50 listener

logs-alloy: ## Follow Alloy (Grafana Cloud shipping) logs
	$(COMPOSE) logs -f --tail 50 alloy

ps: ## Container status
	$(COMPOSE) ps

status: ## Key metrics right now: barking, episodes, health
	@$(COMPOSE) exec -T listener curl -s localhost:9100/metrics | grep -E '^chase_(barking_windows_total|barks_total|barking_now|current_run_seconds|seconds_until_episode|episodes_total|episodes_in_window|violations_total|outbox_pending|heartbeat_timestamp_seconds|data_free_bytes|audio_errors_total)' | sort

watch: ## Live view of `make status` (Ctrl-C to quit)
	@watch -n 2 '$(MAKE) --no-print-directory status'

backlog: ## Newest backlog clips (one per barking burst)
	@$(COMPOSE) exec -T listener sh -c 'ls -1t /data/backlog/*/*.flac 2>/dev/null | head -20 || echo "no clips yet"'

episodes: ## Episodes written to the outbox
	@$(COMPOSE) exec -T listener sh -c 'ls -1 /data/outbox/episodes 2>/dev/null || echo "no episodes yet"'

violations: ## Violation manifests written to the outbox
	@$(COMPOSE) exec -T listener sh -c 'ls -1 /data/outbox/violations/*/ 2>/dev/null || echo "no violations yet"'

replay: ## Re-run the rules over logged barks. Options: SINCE=2026-11-01 PRESET=config/presets/x.yaml
	$(COMPOSE) run --rm --no-deps listener replay --config /config/chase.yaml --presets /config/presets \
	  $(if $(SINCE),--since $(SINCE)) $(if $(PRESET),--preset /config/$(patsubst config/%,%,$(PRESET)))

test-mic: ## Record 5 s from the mic and play it back (stops the listener meanwhile). CARD=mic
	$(COMPOSE) stop listener
	arecord -D plughw:CARD=$(CARD) -f S16_LE -r 48000 -c 1 -d 5 /tmp/chase-mic-test.wav && aplay /tmp/chase-mic-test.wav; \
	  $(COMPOSE) start listener

update: ## Pull the current branch and redeploy if it changed
	@./deploy/auto-update.sh

install-cron: ## Auto-update from git on a schedule (CRON_SCHEDULE='*/15 * * * *')
	@( crontab -l 2>/dev/null | grep -v 'chase-auto-update' ; \
	   echo '$(CRON_SCHEDULE) cd $(REPO_DIR) && ./deploy/auto-update.sh >> $(LOG) 2>&1 # chase-auto-update' ) | crontab -
	@echo "Installed: $(CRON_SCHEDULE) -> $(LOG)"; crontab -l | grep chase-auto-update

uninstall-cron: ## Remove the auto-update cron job
	@crontab -l 2>/dev/null | grep -v 'chase-auto-update' | crontab - ; echo "Removed chase-auto-update from crontab"

cron-log: ## Show recent auto-update runs
	@tail -n 30 $(LOG) 2>/dev/null || echo "no auto-update runs logged yet ($(LOG))"

test: ## Run the listener's tests (dev machine)
	cd listener && uv run pytest -q && uv run ruff check && uv run pyright
