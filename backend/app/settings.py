"""Application settings and configuration."""
import os
from pathlib import Path

# Base paths
BASE_DIR = Path(__file__).parent.parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
JOBS_DIR = DATA_DIR / "jobs"
BUNDLES_DIR = DATA_DIR / "bundles"
CREDENTIALS_DIR = DATA_DIR / "credentials"
# Directory containing config.json for Docker/crane auth (crane reads DOCKER_CONFIG)
DOCKER_CONFIG_DIR = DATA_DIR / "docker"

# Ensure directories exist
JOBS_DIR.mkdir(parents=True, exist_ok=True)
BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)

# Flask settings
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.getenv("FLASK_PORT", "8000"))
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

# Helm settings
HELM_TIMEOUT = int(os.getenv("HELM_TIMEOUT", "300"))  # seconds
HELM_RETRY_LIMIT = int(os.getenv("HELM_RETRY_LIMIT", "3"))

# Image pull settings
IMAGE_PULL_TIMEOUT = int(os.getenv("IMAGE_PULL_TIMEOUT", "600"))  # seconds
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "100"))
MAX_BUNDLE_SIZE_GB = int(os.getenv("MAX_BUNDLE_SIZE_GB", "50"))
IMAGE_PULL_PARALLEL = max(1, int(os.getenv("IMAGE_PULL_PARALLEL", "5")))  # concurrent pulls

# Registry credentials (optional)
REGISTRY_USERNAME = os.getenv("REGISTRY_USERNAME", "")
REGISTRY_PASSWORD = os.getenv("REGISTRY_PASSWORD", "")
REGISTRY_SERVER = os.getenv("REGISTRY_SERVER", "")

# Tool paths
HELM_BIN = os.getenv("HELM_BIN", "/usr/local/bin/helm")
CRANE_BIN = os.getenv("CRANE_BIN", "/usr/local/bin/crane")

# Repo list refresh interval (hours). 0 = no automatic refresh.
REPO_UPDATE_INTERVAL_HOURS = float(os.getenv("REPO_UPDATE_INTERVAL_HOURS", "6"))
