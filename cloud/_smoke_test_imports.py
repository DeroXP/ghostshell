"""Server-side smoke test — import the FastAPI app and walk its routes.

Doesn't connect to Postgres (the lifespan only runs when uvicorn serves).
Just verifies the app object builds cleanly and every router is wired.
"""
import sys

sys.path.insert(0, ".")

import main  # noqa: E402

print("FastAPI app imported OK")
print(f"title    = {main.app.title}")
print(f"version  = {main.app.version}")
print()
print("Routes:")
for r in main.app.routes:
    if not hasattr(r, "path"):
        continue
    methods = sorted(getattr(r, "methods", ["-"])) or ["-"]
    print(f"  {methods[0]:7} {r.path}")
