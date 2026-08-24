import os
import tempfile

os.environ.setdefault("AEGIS_DATA_DIR", tempfile.mkdtemp(prefix="aegis-test-"))
os.environ.setdefault("AEGIS_SEED", "1337")
