.PHONY: local railway switch off help

help:
	@echo "Pantera - Switch between local and Railway"
	@echo ""
	@echo "  make local   - Run locally (stop Railway, start here)"
	@echo "  make railway - Deploy to Railway (stop local, deploy)"
	@echo "  make off     - Stop Railway deployment only"
	@echo "  make switch  - Interactive menu"
	@echo ""

local:
	python scripts/switch_mode.py local

railway:
	python scripts/switch_mode.py railway

off:
	python scripts/switch_mode.py off

switch:
	python scripts/switch_mode.py
