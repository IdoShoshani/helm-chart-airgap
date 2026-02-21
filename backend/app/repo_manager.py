"""Manage Helm repositories: add, update, search charts, get versions."""
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import settings


REPOS_FILE = settings.DATA_DIR / "repos.json"
HELM_BIN = settings.HELM_BIN
HELM_TIMEOUT = settings.HELM_TIMEOUT


def _load_repos() -> dict:
    """Load repos from JSON file."""
    data = {"repos": [], "updated_at": None}
    if REPOS_FILE.exists():
        try:
            with open(REPOS_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            pass
    if "repos" not in data:
        data["repos"] = []
    return data


def _save_repos(data: dict) -> None:
    """Save repos to JSON file."""
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPOS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def list_repos() -> List[dict]:
    """Return list of added repos (name, url)."""
    data = _load_repos()
    return data.get("repos", [])


def get_last_updated() -> Optional[str]:
    """Return ISO timestamp of last repo update, or None."""
    data = _load_repos()
    return data.get("updated_at")


def add_repo(name: str, url: str) -> None:
    """Add a Helm repo. Name must be valid (alphanumeric, dash, underscore)."""
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise ValueError("Repo name must contain only letters, numbers, dashes, and underscores")
    if not url.strip().startswith(("http://", "https://")):
        raise ValueError("Repository URL must start with http:// or https://")
    data = _load_repos()
    names = {r["name"] for r in data["repos"]}
    if name in names:
        raise ValueError(f"Repository '{name}' already exists")
    # Add to helm
    subprocess.run(
        [HELM_BIN, "repo", "add", name, url.strip()],
        capture_output=True,
        text=True,
        timeout=HELM_TIMEOUT,
        check=True,
    )
    data["repos"].append({"name": name, "url": url.strip()})
    _save_repos(data)


def remove_repo(name: str) -> None:
    """Remove a Helm repo."""
    data = _load_repos()
    data["repos"] = [r for r in data["repos"] if r["name"] != name]
    _save_repos(data)
    try:
        subprocess.run(
            [HELM_BIN, "repo", "remove", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        pass


def update_repos() -> None:
    """Run helm repo update for all added repos. Updates updated_at."""
    data = _load_repos()
    if not data["repos"]:
        return
    for r in data["repos"]:
        try:
            subprocess.run(
                [HELM_BIN, "repo", "update", r["name"]],
                capture_output=True,
                text=True,
                timeout=HELM_TIMEOUT,
                check=False,
            )
        except Exception:
            pass
    data["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _save_repos(data)


def sync_helm_repos_from_storage() -> None:
    """Re-add all stored repos to helm (e.g. on container start). Idempotent."""
    for r in list_repos():
        try:
            subprocess.run(
                [HELM_BIN, "repo", "add", r["name"], r["url"]],
                capture_output=True,
                text=True,
                timeout=HELM_TIMEOUT,
                check=False,
            )
        except Exception:
            pass


def search_charts(query: str) -> List[dict]:
    """
    Search charts across all added repos. Returns list of {repo, chart, version, app_version}.
    query can be empty to list all, or a search term (helm search supports keyword).
    """
    if not list_repos():
        return []
    try:
        # helm search repo [keyword] --output json
        cmd = [HELM_BIN, "search", "repo", "--output", "json"]
        if query and query.strip():
            cmd.insert(3, query.strip())
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=HELM_TIMEOUT,
        )
        if result.returncode != 0 or not result.stdout:
            return []
        out = json.loads(result.stdout)
        # Helm 3 output is a list of objects with name, version, app_version
        rows = out if isinstance(out, list) else []
        charts = []
        for row in rows:
            name = row.get("name", "")
            if "/" not in name:
                continue
            repo, chart = name.split("/", 1)
            charts.append({
                "repo": repo,
                "chart": chart,
                "repo_url": get_repo_url(repo) or "",
                "version": row.get("version", ""),
                "app_version": row.get("app_version", ""),
            })
        return charts
    except Exception:
        return []


def get_chart_versions(repo: str, chart: str) -> List[str]:
    """Return list of available versions for repo/chart (newest first)."""
    try:
        result = subprocess.run(
            [HELM_BIN, "search", "repo", f"{repo}/{chart}", "--versions", "--output", "json"],
            capture_output=True,
            text=True,
            timeout=HELM_TIMEOUT,
        )
        if result.returncode != 0 or not result.stdout:
            return []
        out = json.loads(result.stdout)
        rows = out if isinstance(out, list) else []
        versions = [r.get("version", "") for r in rows if r.get("version")]
        return versions
    except Exception:
        return []


def get_repo_url(repo_name: str) -> Optional[str]:
    """Return URL for a repo by name."""
    for r in list_repos():
        if r["name"] == repo_name:
            return r["url"]
    return None
