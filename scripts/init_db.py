"""Create all database tables from the ORM models (idempotent)."""

from db.base import Base, engine
from db import models  # noqa: F401  (registers models on Base.metadata)


def main():
    Base.metadata.create_all(engine)
    tables = sorted(Base.metadata.tables.keys())
    print(f"Created / verified {len(tables)} tables:")
    for t in tables:
        print(f"  - {t}")


if __name__ == "__main__":
    main()
