# Nurix Voice Campaign

Outbound voice campaign microservice. Local-only via `docker compose`.

Full setup + demo guide lands in P5 once the stack is complete.

## Quick start (scaffold)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

make up            # builds + starts postgres + app, waits for /health
curl localhost:8001/health    # -> {"status":"ok"}
make down
```
