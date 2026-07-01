from .db import Base, engine
from . import models  # noqa: F401
from .schema_upgrade import run_schema_upgrades


def init_db() -> None:
    run_schema_upgrades(engine)
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
    print("FinDataHub database initialized.")
