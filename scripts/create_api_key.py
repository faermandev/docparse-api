"""Create an API key and print it once (only its hash is stored).

Usage:
    uv run python -m scripts.create_api_key --name "acme-backend"
"""

import argparse

from app.db.session import SessionLocal
from app.services.api_keys import create_api_key


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an API key.")
    parser.add_argument("--name", required=True, help="Who or what the key is for")
    args = parser.parse_args()

    with SessionLocal() as db:
        api_key, plaintext = create_api_key(db, args.name)

    print(f"Created key {api_key.id} ({api_key.name}).")
    print("Store it now; it cannot be shown again:\n")
    print(plaintext)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
