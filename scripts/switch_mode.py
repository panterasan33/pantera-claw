#!/usr/bin/env python3
"""
Switch between running Pantera locally vs on Railway.
Only one instance can poll Telegram at a time, so this script helps you switch cleanly.

Usage:
  python scripts/switch_mode.py          # Interactive menu
  python scripts/switch_mode.py local    # Run locally
  python scripts/switch_mode.py railway  # Deploy to Railway
"""
import os
import signal
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def kill_local():
    """Kill any running local Pantera process."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python main.py"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    print(f"  Stopped local process (PID {pid})")
                except (ProcessLookupError, ValueError):
                    pass
            return True
    except Exception:
        pass
    return False


def run_local():
    """Kill local, stop Railway deployment, start local."""
    print("\n🐆 Switching to LOCAL mode...")
    killed = kill_local()
    if not killed:
        print("  No local process was running")

    # Try to stop Railway deployment via CLI (railway down removes the deployment)
    if _railway_down():
        print("  Railway pantera-claw deployment stopped")
    else:
        print("  ⚠️  Another bot instance is still running (likely on Railway).")
        print("     To avoid conflicts, stop it first:")
        print("     • Run: railway down -y  (if CLI is linked)")
        print("     • Or: Railway dashboard → pantera-claw → Settings → remove deployment")

    print("\n  Starting Pantera locally... (Ctrl+C to stop)\n")
    os.chdir(PROJECT_ROOT)
    os.execv(sys.executable, [sys.executable, "main.py"])


def run_railway():
    """Kill local, deploy to Railway."""
    print("\n🚂 Switching to RAILWAY mode...")
    kill_local()

    print("\n  Deploying to Railway...")
    os.chdir(PROJECT_ROOT)
    result = subprocess.run(["railway", "up"], cwd=PROJECT_ROOT)
    if result.returncode != 0:
        print("\n  Railway CLI failed. Make sure you've run 'railway link' in this project.")
        print("  Or deploy manually: railway up")
        sys.exit(1)


def _railway_down() -> bool:
    """Stop Railway deployment via CLI. Returns True if successful."""
    try:
        result = subprocess.run(
            ["railway", "down", "-y", "-s", "pantera-claw"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def run_railway_off():
    """Stop Railway deployment only (run this before starting local if needed)."""
    print("\n🛑 Stopping Railway deployment...")
    if _railway_down():
        print("  Done. You can now run locally.")
    else:
        print("  Failed. Run 'railway login' and 'railway link' in this project first.")
        print("  Or stop manually: Railway dashboard → pantera-claw → remove deployment")

def main():
    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()
        if mode in ("local", "l"):
            run_local()
        elif mode in ("railway", "r", "deploy"):
            run_railway()
        elif mode in ("off", "stop", "railway-off"):
            run_railway_off()
        else:
            print(f"Unknown mode: {mode}")
            print("Use: local | railway | off")
            sys.exit(1)
        return

    # Interactive menu
    print("\n🐆 Pantera - Switch run mode")
    print("   (Only one instance can run the Telegram bot at a time)\n")
    print("  1) Run LOCALLY  (stops Railway, starts here)")
    print("  2) Run on RAILWAY  (stops local, deploys)")
    print("  3) Stop Railway only  (run before local if you see conflicts)")
    print("  4) Quit")
    print()
    choice = input("  Choice [1-4]: ").strip() or "1"

    if choice in ("1", "l", "local"):
        run_local()
    elif choice in ("2", "r", "railway"):
        run_railway()
    elif choice in ("3", "off"):
        run_railway_off()
    else:
        print("  Bye!")


if __name__ == "__main__":
    main()
