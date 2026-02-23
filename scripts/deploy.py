#!/usr/bin/env python3
"""Deploy Pantera to Railway."""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

def main():
    print("\n🚂 Deploying to Railway...\n")
    result = subprocess.run(["railway", "up"], cwd=PROJECT_ROOT)
    if result.returncode != 0:
        print("\n  Railway CLI failed. Deploy instead by:")
        print("  • Pushing to GitHub (if connected): git push")
        print("  • Railway dashboard → pantera-claw → Redeploy")
        sys.exit(1)

if __name__ == "__main__":
    main()
