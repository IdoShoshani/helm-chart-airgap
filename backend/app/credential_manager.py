"""Credential management for registry authentication.

Note: Passwords are base64-encoded for obfuscation only, not encryption.
For production, prefer environment variables or an external secret store.
"""
import base64
import json
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import settings


class CredentialManager:
    """Manages registry credentials storage and authentication."""
    
    def __init__(self):
        self.credentials_file = settings.DATA_DIR / "credentials" / "registries.json"
        self.credentials_file.parent.mkdir(parents=True, exist_ok=True)
        self.helm_bin = settings.HELM_BIN
        
        # Ensure file exists
        if not self.credentials_file.exists():
            self._save_credentials({"registries": []})
        # Sync to Docker config so crane (and other tools) can use the same credentials
        self._sync_docker_config()
    
    def _docker_config_path(self) -> Path:
        """Path to Docker config.json used by crane for registry auth."""
        return settings.DOCKER_CONFIG_DIR / "config.json"
    
    def _sync_docker_config(self):
        """Write Docker-style config.json from current registries so crane can find credentials."""
        data = self._load_credentials()
        registries = data.get("registries", [])
        auths = {}
        for cred in registries:
            server = cred.get("server", "").strip()
            if not server:
                continue
            try:
                password = self._decode_password(cred.get("password_encrypted", ""))
            except Exception:
                continue
            username = cred.get("username", "")
            if not username or not password:
                continue
            # Docker auth: base64(username:password)
            auth = base64.b64encode(f"{username}:{password}".encode()).decode()
            auths[server] = {"auth": auth}
            # Docker Hub: crane and Docker CLI often use this key
            if server == "docker.io":
                auths["https://index.docker.io/v1/"] = {"auth": auth}
        config = {"auths": auths}
        config_dir = settings.DOCKER_CONFIG_DIR
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = self._docker_config_path()
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)
        try:
            config_file.chmod(0o600)
        except Exception:
            pass
    
    def _load_credentials(self) -> Dict:
        """Load credentials from JSON file."""
        try:
            with open(self.credentials_file, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"registries": []}
    
    def _save_credentials(self, data: Dict):
        """Save credentials to JSON file."""
        # Set restrictive permissions
        with open(self.credentials_file, "w") as f:
            json.dump(data, f, indent=2)
        
        # Set file permissions to 600 (owner read/write only)
        try:
            self.credentials_file.chmod(0o600)
        except Exception:
            pass  # May fail on some systems
    
    def _encode_password(self, password: str) -> str:
        """Encode password (basic obfuscation)."""
        return base64.b64encode(password.encode()).decode()
    
    def _decode_password(self, encoded: str) -> str:
        """Decode password."""
        try:
            return base64.b64decode(encoded.encode()).decode()
        except Exception:
            return ""
    
    def sync_env_credentials(self):
        """Sync credentials from environment variables."""
        if not (settings.REGISTRY_USERNAME and settings.REGISTRY_PASSWORD and settings.REGISTRY_SERVER):
            return
        
        data = self._load_credentials()
        registries = data.get("registries", [])
        
        # Check if env credential already exists
        env_cred = next(
            (r for r in registries if r.get("source") == "env" and r.get("server") == settings.REGISTRY_SERVER),
            None
        )
        
        credential_data = {
            "id": env_cred["id"] if env_cred else str(uuid.uuid4()),
            "server": settings.REGISTRY_SERVER,
            "username": settings.REGISTRY_USERNAME,
            "password_encrypted": self._encode_password(settings.REGISTRY_PASSWORD),
            "source": "env",
            "created_at": env_cred.get("created_at") if env_cred else datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "last_used": None,
            "login_status": "unknown",
            "login_status_checked_at": None
        }
        
        if env_cred:
            # Update existing
            idx = registries.index(env_cred)
            registries[idx] = credential_data
        else:
            # Add new
            registries.append(credential_data)
        
        data["registries"] = registries
        self._save_credentials(data)
        self._sync_docker_config()
        
        # Try to login with helm registry login
        self._helm_registry_login(settings.REGISTRY_SERVER, settings.REGISTRY_USERNAME, settings.REGISTRY_PASSWORD)
    
    def _helm_registry_login(self, server: str, username: str, password: str) -> bool:
        """Perform helm registry login. Uses DOCKER_CONFIG so credentials are stored in the
        app's config (same location crane and helm pull use)."""
        try:
            login_cmd = [
                self.helm_bin, "registry", "login", server,
                "--username", username,
                "--password-stdin"
            ]
            process = subprocess.Popen(
                login_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._crane_env(),
            )
            stdout, stderr = process.communicate(input=password)
            return process.returncode == 0
        except Exception:
            return False

    def _crane_env(self) -> Dict[str, str]:
        """Environment for crane so it uses our Docker config (same as image pulls)."""
        env = os.environ.copy()
        env["DOCKER_CONFIG"] = str(settings.DOCKER_CONFIG_DIR)
        return env

    def _crane_verify_login(self, server: str) -> Tuple[bool, str]:
        """
        Verify credentials with crane (same path used for image pulls).
        Runs 'crane catalog <server>' with DOCKER_CONFIG set. Returns (success, message).
        """
        try:
            result = subprocess.run(
                [settings.CRANE_BIN, "catalog", server],
                env=self._crane_env(),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return True, "Login successful (verified with crane)"
            err = (result.stderr or result.stdout or "").lower()
            if "401" in err or "unauthorized" in err or "authentication" in err:
                return False, "Login failed - check username and password"
            # Registry may not support catalog API; fall back to helm check
            return False, result.stderr or result.stdout or "Verification failed"
        except subprocess.TimeoutExpired:
            return False, "Verification timed out"
        except FileNotFoundError:
            return False, "Crane not found - cannot verify"
        except Exception as e:
            return False, str(e)
    
    def add_credential(self, server: str, username: str, password: str, source: str = "web") -> str:
        """Add a new credential."""
        data = self._load_credentials()
        registries = data.get("registries", [])
        
        # Check if credential for this server already exists
        existing = next((r for r in registries if r.get("server") == server), None)
        if existing:
            raise ValueError(f"Credential for {server} already exists. Use update instead.")
        
        credential_data = {
            "id": str(uuid.uuid4()),
            "server": server,
            "username": username,
            "password_encrypted": self._encode_password(password),
            "source": source,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "last_used": None,
            "login_status": "unknown",
            "login_status_checked_at": None
        }
        
        registries.append(credential_data)
        data["registries"] = registries
        self._save_credentials(data)
        self._sync_docker_config()
        
        # Try to login
        if self._helm_registry_login(server, username, password):
            self.update_login_status(credential_data["id"], "success")
        else:
            self.update_login_status(credential_data["id"], "failed")
        
        return credential_data["id"]
    
    def update_credential(self, credential_id: str, server: Optional[str] = None,
                         username: Optional[str] = None, password: Optional[str] = None) -> bool:
        """Update an existing credential."""
        data = self._load_credentials()
        registries = data.get("registries", [])
        
        cred = next((r for r in registries if r.get("id") == credential_id), None)
        if not cred:
            return False
        
        # Don't allow updating env-sourced credentials
        if cred.get("source") == "env":
            raise ValueError("Cannot update credentials from environment variables. Update docker-compose.yml instead.")
        
        idx = registries.index(cred)
        
        if server:
            cred["server"] = server
        if username:
            cred["username"] = username
        if password:
            cred["password_encrypted"] = self._encode_password(password)
        
        cred["updated_at"] = datetime.now().isoformat()
        cred["login_status"] = "unknown"  # Reset status, needs retest
        
        registries[idx] = cred
        data["registries"] = registries
        self._save_credentials(data)
        self._sync_docker_config()
        
        # Try to login if password was updated
        if password:
            decoded_password = password
        else:
            decoded_password = self._decode_password(cred["password_encrypted"])
        
        if self._helm_registry_login(cred["server"], cred["username"], decoded_password):
            self.update_login_status(credential_id, "success")
        else:
            self.update_login_status(credential_id, "failed")
        
        return True
    
    def delete_credential(self, credential_id: str) -> bool:
        """Delete a credential."""
        data = self._load_credentials()
        registries = data.get("registries", [])
        
        cred = next((r for r in registries if r.get("id") == credential_id), None)
        if not cred:
            return False
        
        # Don't allow deleting env-sourced credentials
        if cred.get("source") == "env":
            raise ValueError("Cannot delete credentials from environment variables. Remove from docker-compose.yml instead.")
        
        registries = [r for r in registries if r.get("id") != credential_id]
        data["registries"] = registries
        self._save_credentials(data)
        self._sync_docker_config()
        
        return True
    
    def get_credential_for_server(self, server: str) -> Optional[Dict]:
        """Get credential for a specific server."""
        data = self._load_credentials()
        registries = data.get("registries", [])
        
        cred = next((r for r in registries if r.get("server") == server), None)
        if not cred:
            return None
        
        # Return credential with decoded password
        return {
            "id": cred["id"],
            "server": cred["server"],
            "username": cred["username"],
            "password": self._decode_password(cred["password_encrypted"]),
            "source": cred.get("source", "web"),
            "login_status": cred.get("login_status", "unknown")
        }
    
    def get_all_credentials(self, include_passwords: bool = False) -> List[Dict]:
        """Get all credentials (passwords masked by default)."""
        data = self._load_credentials()
        registries = data.get("registries", [])
        
        result = []
        for cred in registries:
            cred_dict = {
                "id": cred["id"],
                "server": cred["server"],
                "username": cred["username"],
                "source": cred.get("source", "web"),
                "created_at": cred.get("created_at"),
                "updated_at": cred.get("updated_at"),
                "last_used": cred.get("last_used"),
                "login_status": cred.get("login_status", "unknown"),
                "login_status_checked_at": cred.get("login_status_checked_at")
            }
            
            if include_passwords:
                cred_dict["password"] = self._decode_password(cred["password_encrypted"])
            else:
                # Mask password
                password_len = len(self._decode_password(cred["password_encrypted"]))
                cred_dict["password_masked"] = "*" * min(password_len, 20)
            
            result.append(cred_dict)
        
        return result
    
    def test_login(self, credential_id: str) -> Dict:
        """Test if credentials work (verified with crane when possible, same path as image pulls)."""
        data = self._load_credentials()
        registries = data.get("registries", [])
        
        cred = next((r for r in registries if r.get("id") == credential_id), None)
        if not cred:
            return {"success": False, "error": "Credential not found"}
        
        password = self._decode_password(cred["password_encrypted"])
        
        # Ensure Docker config is up to date so crane sees this credential
        self._sync_docker_config()
        
        # Prefer crane catalog (same auth path used for image pulls)
        success, message = self._crane_verify_login(cred["server"])
        
        # Fallback: if crane failed but not due to auth (e.g. registry doesn't support catalog), try helm
        auth_failure = not success and (
            "login failed" in message.lower() or "401" in message or "unauthorized" in message.lower()
        )
        if not success and not auth_failure:
            if self._helm_registry_login(cred["server"], cred["username"], password):
                success = True
                message = "Login successful (verified with Helm; crane catalog not supported by this registry)"
        
        if success:
            self._helm_registry_login(cred["server"], cred["username"], password)
        
        self.update_login_status(credential_id, "success" if success else "failed")
        
        return {
            "success": success,
            "message": message,
        }
    
    def update_login_status(self, credential_id: str, status: str):
        """Update login status for a credential."""
        data = self._load_credentials()
        registries = data.get("registries", [])
        
        cred = next((r for r in registries if r.get("id") == credential_id), None)
        if not cred:
            return
        
        idx = registries.index(cred)
        cred["login_status"] = status
        cred["login_status_checked_at"] = datetime.now().isoformat()
        
        registries[idx] = cred
        data["registries"] = registries
        self._save_credentials(data)
    
    def mark_credential_used(self, server: str):
        """Mark a credential as used (update last_used timestamp)."""
        data = self._load_credentials()
        registries = data.get("registries", [])
        
        cred = next((r for r in registries if r.get("server") == server), None)
        if not cred:
            return
        
        idx = registries.index(cred)
        cred["last_used"] = datetime.now().isoformat()
        
        registries[idx] = cred
        data["registries"] = registries
        self._save_credentials(data)
