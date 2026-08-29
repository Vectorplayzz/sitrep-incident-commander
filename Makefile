.PHONY: up down logs incident heal status reset ps

## Bring the victim stack up and start generating a healthy baseline.
up:
	docker compose up -d --build
	@echo "stack up. give it ~90s to build a baseline before triggering an incident."

down:
	docker compose down

## Wipe telemetry and start clean (do this before recording the demo).
reset:
	docker compose down -v

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

## Ship the bad deploy. This is the outage.
## Scenario 1: ship the bad deploy (code regression).
incident:
	docker compose exec -T checkout-api python -m chaos.main incident

## Ship the good deploy (manual undo; normally the agent does this via its gated tool).
heal:
	docker compose exec -T checkout-api python -m chaos.main heal

status:
	docker compose exec -T checkout-api python -m chaos.main status

## Scenario 2: demand outgrows capacity. No deploy. Fix is scale, not rollback.
surge:
	docker compose exec -T checkout-api python -m chaos.main surge

## Scenario 3: the upstream dependency degrades. checkout-api pages but is healthy.
degrade:
	docker compose exec -T checkout-api python -m chaos.main degrade-inventory

## Undo every scenario and return to the healthy baseline.
restore:
	docker compose exec -T checkout-api python -m chaos.main restore
