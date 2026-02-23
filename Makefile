.PHONY: deploy help

help:
	@echo "Pantera - Deploy to Railway"
	@echo ""
	@echo "  make deploy  - Deploy to Railway (push to GitHub or use railway up)"
	@echo ""

deploy:
	@railway up 2>/dev/null || (echo ""; echo "Deploy via: git push (if GitHub connected) or Railway dashboard → Redeploy"; echo "")
