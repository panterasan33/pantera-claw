.PHONY: deploy push help

help:
	@echo "Pantera - Deploy to Railway"
	@echo ""
	@echo "  make push   - Push to GitHub (uses GITHUB_PAT from secrets.env)"
	@echo "  make deploy - Deploy to Railway (git push triggers deploy if connected)"
	@echo ""

push:
	@./scripts/push.sh

deploy: push
	@echo "Pushed. Railway will deploy if GitHub is connected."
