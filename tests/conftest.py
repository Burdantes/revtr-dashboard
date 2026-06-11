import os
import sys
from pathlib import Path

import pytest

# Make app.py importable and avoid needing real GCP creds at import time.
APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")


@pytest.fixture()
def client():
    import app as appmod

    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()
