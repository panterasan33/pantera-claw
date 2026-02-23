.PHONY: deploy push run help

help:
	@echo "Pantera - Deploy to Railway"
	@echo ""
	@echo "  make run    - Run Pantera (bot + web UI at http://localhost:3000)"
	@echo "  make push   - Push to GitHub (uses GITHUB_PAT from secrets.env)"
	@echo "  make deploy - Deploy to Railway (git push triggers deploy if connected)"
	@echo ""

run:
	python main.py

push:
	@./scripts/push.sh

deploy: push
	@echo "Pushed. Railway will deploy if GitHub is connected."
