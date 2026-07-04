"""P4: db.base must fail loud when DATABASE_URL is unset.

The old behavior silently defaulted to localhost — a container missing the env
var would happily write to the wrong (or no) database. Local dev and prod both
provide DATABASE_URL (.env / docker-compose / conftest), so an unset value is
always a deployment mistake and must be a hard, explicit error.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_missing_database_url_raises_at_import(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    # cwd=tmp_path so load_dotenv() cannot find the repo's .env fallback.
    r = subprocess.run(
        [sys.executable, "-c", "import db.base"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode != 0, "import db.base must fail without DATABASE_URL"
    assert "DATABASE_URL" in r.stderr


def test_database_url_from_env_works(tmp_path):
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["DATABASE_URL"] = "postgresql+psycopg://ipo:ipo@localhost:5432/ipo_test"
    r = subprocess.run(
        [sys.executable, "-c", "import db.base; print(db.base.DATABASE_URL)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "ipo_test" in r.stdout
