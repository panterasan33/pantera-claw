#!/usr/bin/env python3
"""
Fetch pgvector database config from Railway API and print values for .config/secrets.env.
Requires CURSOR_RAILWAY_TOKEN in .config/secrets.env (project token from Railway).
"""
import json
import os
import sys
from pathlib import Path

# Load secrets
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
env_path = Path(__file__).parent.parent / ".config" / "secrets.env"
load_dotenv(env_path)
TOKEN = os.environ.get("CURSOR_RAILWAY_TOKEN")
if not TOKEN:
    print("Error: CURSOR_RAILWAY_TOKEN not found in .config/secrets.env")
    print("Tried path:", env_path.resolve())
    sys.exit(1)

# Project token gives us project + environment
import httpx
r = httpx.post(
    "https://backboard.railway.com/graphql/v2",
    json={"query": 'query { projectToken { projectId environmentId } }'},
    headers={"Project-Access-Token": TOKEN, "Content-Type": "application/json"},
    timeout=10,
)
r.raise_for_status()
data = r.json()
project_id = data["data"]["projectToken"]["projectId"]
env_id = data["data"]["projectToken"]["environmentId"]

# Get services
r = httpx.post(
    "https://backboard.railway.com/graphql/v2",
    json={"query": 'query { project(id: "' + project_id + '") { services { edges { node { id name } } } } }'},
    headers={"Project-Access-Token": TOKEN, "Content-Type": "application/json"},
    timeout=10,
)
r.raise_for_status()
data = r.json()
services = data["data"]["project"]["services"]["edges"]
pgvector = next((e["node"] for e in services if "pgvector" in e["node"]["name"].lower()), None)
if not pgvector:
    print("No pgvector service found. Services:", [e["node"]["name"] for e in services])
    sys.exit(1)

# Get variables
r = httpx.post(
    "https://backboard.railway.com/graphql/v2",
    json={
        "query": "query($e: String!, $p: String!, $s: String!) { variables(environmentId: $e, projectId: $p, serviceId: $s) }",
        "variables": {"e": env_id, "p": project_id, "s": pgvector["id"]},
    },
    headers={"Project-Access-Token": TOKEN, "Content-Type": "application/json"},
    timeout=10,
)
r.raise_for_status()
vars = r.json()["data"]["variables"]

# Check for TCP proxy
tcp_domain = vars.get("RAILWAY_TCP_PROXY_DOMAIN")
tcp_port = vars.get("RAILWAY_TCP_PROXY_PORT")
pg_host = vars.get("PGHOST") or tcp_domain
pg_port = vars.get("PGPORT") or tcp_port
public_domain = vars.get("RAILWAY_PUBLIC_DOMAIN")

print("Railway pgvector variables (for local dev):")
print()
if tcp_domain and tcp_port:
    print("TCP Proxy is ENABLED. Add to .config/secrets.env:")
    print(f"  PGHOST={tcp_domain}")
    print(f"  PGPORT={tcp_port}")
else:
    print("TCP Proxy is NOT enabled. To connect from local:")
    print("  1. Go to pgvector service > Settings > Networking")
    print("  2. Enable TCP Proxy, enter port 5432")
    print("  3. Run this script again to get PGHOST and PGPORT")
    if public_domain:
        print(f"  (RAILWAY_PUBLIC_DOMAIN={public_domain} is for HTTP, not Postgres TCP)")
print()
print("Other vars (already in secrets.env):")
print(f"  PGUSER={vars.get('PGUSER', 'postgres')}")
print(f"  PGDATABASE={vars.get('PGDATABASE', 'railway')}")
print(f"  PGPASSWORD=***")
