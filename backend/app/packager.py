"""Core packaging logic for Helm charts and container images."""
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

from . import settings


class PackagingError(Exception):
    """Custom exception for packaging errors."""
    pass


class JobCancelledError(PackagingError):
    """Exception raised when a job is cancelled by user."""
    pass


class HelmPackager:
    """Handles packaging of Helm charts for air-gap environments."""
    
    def __init__(self, credential_manager=None):
        self.helm_bin = settings.HELM_BIN
        self.crane_bin = settings.CRANE_BIN
        self.jobs_dir = settings.JOBS_DIR
        self.bundles_dir = settings.BUNDLES_DIR
        self.credential_manager = credential_manager
        
        # Verify tools exist
        self._verify_tools()
        
        # Set up authentication if credentials are provided
        self._setup_registry_auth()
    
    def _verify_tools(self):
        """Verify that required tools are available."""
        if not shutil.which(self.helm_bin):
            raise PackagingError(f"Helm binary not found at {self.helm_bin}")
        if not shutil.which(self.crane_bin):
            raise PackagingError(f"Crane binary not found at {self.crane_bin}")
    
    def _setup_registry_auth(self):
        """Set up registry authentication if credentials are provided."""
        # This is now handled by credential_manager.sync_env_credentials() on startup
        # Keeping this method for backward compatibility
        pass
    
    def _crane_env(self) -> Dict[str, str]:
        """Environment for crane subprocesses so it finds registry auth in our Docker config."""
        env = os.environ.copy()
        env["DOCKER_CONFIG"] = str(settings.DOCKER_CONFIG_DIR)
        return env
    
    def _ensure_registry_login(self, server: str, username: str, password: str, job_dir: Path):
        """Ensure we're logged into a registry."""
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
                text=True
            )
            stdout, stderr = process.communicate(input=password)
            if process.returncode == 0:
                self._log(job_dir, f"Successfully logged into {server}")
            else:
                self._log(job_dir, f"Warning: Failed to login to {server}: {stderr}")
        except Exception as e:
            self._log(job_dir, f"Warning: Error logging into {server}: {e}")
    
    def _log(self, job_dir: Path, message: str):
        """Append a log message to the job log file."""
        log_file = job_dir / "log.txt"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
    
    def _write_progress(self, job_dir: Path, step: str, percent: float, message: str, details: Optional[dict] = None):
        """Write progress to progress.json for UI polling."""
        progress_file = job_dir / "progress.json"
        data = {
            "step": step,
            "percent": round(min(100, max(0, percent)), 1),
            "message": message,
            "details": details or {},
            "updated_at": datetime.now().isoformat()
        }
        try:
            with open(progress_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
    
    def _run_command(self, cmd: List[str], job_dir: Path, check: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command and log output. Checks for cancel.flag periodically and stops if set."""
        self._log(job_dir, f"Running: {' '.join(cmd)}")
        timeout_seconds = settings.HELM_TIMEOUT if "helm" in cmd[0] else settings.IMAGE_PULL_TIMEOUT
        poll_interval = 2  # Check cancel every 2 seconds
        start = datetime.now()
        run_env = self._crane_env() if cmd and (cmd[0] == self.crane_bin or self.crane_bin in str(cmd[0])) else None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(job_dir),
                env=run_env,
            )
            while True:
                # Wait up to poll_interval seconds for process to exit
                try:
                    proc.wait(timeout=poll_interval)
                    break
                except subprocess.TimeoutExpired:
                    pass
                # Check for user cancellation (so job stops within ~2 seconds of clicking Cancel)
                if self._check_cancelled(job_dir):
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    proc.communicate()  # drain pipes to avoid zombie
                    self._log(job_dir, "Command stopped by user (cancel requested)")
                    raise JobCancelledError("Job was cancelled by user")
                # Check overall timeout
                elapsed = (datetime.now() - start).total_seconds()
                if elapsed >= timeout_seconds:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    proc.communicate()
                    self._log(job_dir, f"ERROR: Command timed out after {timeout_seconds}s: {' '.join(cmd)}")
                    raise PackagingError(
                        f"Operation timed out after {timeout_seconds} seconds. "
                        f"This may indicate network issues or the chart/images are very large. "
                        f"Try increasing timeout settings in your configuration."
                    )
            # Read any remaining output
            out, err = proc.communicate()
            result = subprocess.CompletedProcess(cmd, proc.returncode, stdout=out or "", stderr=err or "")
            if result.stdout:
                stdout_preview = result.stdout[-1000:] if len(result.stdout) > 1000 else result.stdout
                self._log(job_dir, f"STDOUT: {stdout_preview}")
            if result.stderr:
                stderr_preview = result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr
                self._log(job_dir, f"STDERR: {stderr_preview}")
            if check and result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
            return result
        except JobCancelledError:
            raise
        except subprocess.CalledProcessError as e:
            # Get the actual error output
            error_output = (e.stderr or e.stdout or "Unknown error").strip()
            
            # Log the full error for debugging
            self._log(job_dir, f"ERROR: Command failed with exit code {e.returncode}")
            if e.stderr:
                self._log(job_dir, f"STDERR (full): {e.stderr}")
            if e.stdout:
                self._log(job_dir, f"STDOUT (full): {e.stdout}")
            
            # Build a more helpful error message
            error_msg = error_output
            
            # Add context based on error type
            if "not found" in error_output.lower() or "404" in error_output.lower():
                if "helm" in cmd[0] and "oci://" in " ".join(cmd):
                    error_msg = (
                        f"Chart not found at {oci_chart if 'oci_chart' in locals() else 'OCI location'}.\n"
                        f"Helm error: {error_output}\n\n"
                        f"Possible causes:\n"
                        f"1. Chart doesn't exist at this location\n"
                        f"2. Authentication required (try: helm registry login <registry>)\n"
                        f"3. Incorrect chart URL or version\n"
                        f"4. Registry requires authentication (check if it's a private registry)"
                    )
                elif "matching not found" in error_output.lower() or "no chart name found" in error_output.lower():
                    error_msg = (
                        "Chart name not found in this repository.\n"
                        f"Helm error: {error_output}\n\n"
                        "The chart name must be the actual chart name in the repo, not the repo name. "
                        "For repo 'https://prometheus-community.github.io/helm-charts', use chart name "
                        "'kube-prometheus-stack' or 'prometheus', not 'prometheus-community'. "
                        "To list charts: run 'helm search repo <repo-alias>' after adding the repo."
                    )
                else:
                    error_msg = f"Resource not found. Error: {error_output}"
            elif "unauthorized" in error_output.lower() or "authentication" in error_output.lower() or "401" in error_output.lower():
                error_msg = (
                    f"Authentication failed.\n"
                    f"Helm error: {error_output}\n\n"
                    f"To fix:\n"
                    f"1. Run: helm registry login <registry-host> (recommended)\n"
                    f"2. Set REGISTRY_USERNAME/PASSWORD/SERVER env vars in docker-compose.yml\n"
                    f"3. For GHCR: Use a Personal Access Token with 'read:packages' permission"
                )
            elif "docker-credential" in error_output.lower() or "executable file not found" in error_output.lower():
                error_msg = (
                    f"Docker credential helper issue detected.\n"
                    f"Helm error: {error_output}\n\n"
                    f"This happens when Docker config.json references macOS credential helpers.\n\n"
                    f"Solutions:\n"
                    f"1. RECOMMENDED: Use 'helm registry login <registry>' instead of Docker login\n"
                    f"   Then mount: ~/.config/helm:/home/appuser/.config/helm:ro\n"
                    f"2. Set REGISTRY_USERNAME/PASSWORD/SERVER env vars (auto-login on startup)\n"
                    f"3. Remove credential helper from Docker config before mounting\n"
                )
            elif "connection" in error_output.lower() or "network" in error_output.lower() or "timeout" in error_output.lower():
                error_msg = f"Network error. Check your internet connection and repository accessibility.\nError: {error_output}"
            elif "403" in error_output.lower() or "forbidden" in error_output.lower():
                error_msg = (
                    f"Access forbidden.\n"
                    f"Helm error: {error_output}\n\n"
                    f"This usually means:\n"
                    f"1. You don't have permission to access this chart\n"
                    f"2. Authentication is required\n"
                    f"3. The chart is private and you need to login first"
                )
            else:
                # Use the actual error message
                error_msg = f"Command failed: {error_output}"
            
            raise PackagingError(error_msg)
    
    def _create_job_dir(self) -> tuple[str, Path]:
        """Create a new job directory and return job ID and path."""
        job_id = str(uuid.uuid4())
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_id, job_dir
    
    def _check_cancelled(self, job_dir: Path) -> bool:
        """Check if job has been cancelled via cancel.flag file."""
        cancel_flag = job_dir / "cancel.flag"
        return cancel_flag.exists()
    
    def _mark_cancelled(self, job_dir: Path):
        """Mark job as cancelled by creating cancel.flag file."""
        cancel_flag = job_dir / "cancel.flag"
        cancel_flag.touch()
    
    def _save_job_metadata(self, job_dir: Path, metadata: dict):
        """Save job metadata to JSON file."""
        job_json = job_dir / "job.json"
        with open(job_json, "w") as f:
            json.dump(metadata, f, indent=2)
    
    def _fetch_chart_from_repo(self, repo_url: str, chart_name: str, chart_version: Optional[str], job_dir: Path) -> Path:
        """Fetch Helm chart from a repository."""
        self._log(job_dir, f"Fetching chart from repository: {repo_url}/{chart_name}")
        
        # Generate a unique repo name
        repo_name = f"repo_{uuid.uuid4().hex[:8]}"
        
        # Add repository
        add_cmd = [self.helm_bin, "repo", "add", repo_name, repo_url]
        self._run_command(add_cmd, job_dir)
        
        try:
            # Update repo
            update_cmd = [self.helm_bin, "repo", "update", repo_name]
            self._run_command(update_cmd, job_dir)
            
            # Pull chart
            pull_cmd = [self.helm_bin, "pull", f"{repo_name}/{chart_name}"]
            if chart_version:
                pull_cmd.extend(["--version", chart_version])
            pull_cmd.extend(["--destination", str(job_dir)])
            
            self._run_command(pull_cmd, job_dir)
            
            # Find the downloaded chart file
            chart_files = list(job_dir.glob(f"{chart_name}-*.tgz"))
            if not chart_files:
                raise PackagingError(f"Chart file not found after pull: {chart_name}")
            
            chart_file = chart_files[0]
            self._log(job_dir, f"Chart downloaded: {chart_file.name}")
            
            return chart_file
            
        finally:
            # Clean up repo
            try:
                remove_cmd = [self.helm_bin, "repo", "remove", repo_name]
                self._run_command(remove_cmd, job_dir, check=False)
            except:
                pass
    
    def _fetch_chart_from_oci(self, oci_chart: str, chart_version: Optional[str], job_dir: Path) -> Path:
        """Fetch Helm chart from OCI registry."""
        self._log(job_dir, f"Fetching chart from OCI: {oci_chart}")
        
        if not oci_chart.startswith("oci://"):
            raise PackagingError("OCI chart URL must start with 'oci://'")
        
        # Extract registry server from OCI URL
        registry_server = oci_chart.replace("oci://", "").split("/")[0]
        
        # Check if we have credentials for this registry
        if self.credential_manager:
            cred = self.credential_manager.get_credential_for_server(registry_server)
            if cred:
                self._log(job_dir, f"Using credentials for {registry_server}")
                # Mark credential as used
                self.credential_manager.mark_credential_used(registry_server)
                # Ensure we're logged in
                self._ensure_registry_login(registry_server, cred["username"], cred["password"], job_dir)
        
        # Build pull command
        pull_cmd = [self.helm_bin, "pull", oci_chart, "--destination", str(job_dir)]
        
        # Add version if specified
        if chart_version:
            pull_cmd.extend(["--version", chart_version])
            self._log(job_dir, f"Pulling version: {chart_version}")
        
        # Pull chart - capture the actual error if it fails
        try:
            self._run_command(pull_cmd, job_dir)
        except PackagingError as e:
            # Re-raise with more context about OCI charts
            raise PackagingError(
                f"Failed to pull OCI chart: {oci_chart}\n"
                f"{str(e)}\n\n"
                f"Troubleshooting:\n"
                f"1. Verify the chart exists: Try accessing the registry directly\n"
                f"2. Check authentication: Run 'helm registry login <registry-host>'\n"
                f"3. Verify URL format: For GHCR, format is oci://ghcr.io/owner/charts/chart-name\n"
                f"4. Check if version exists: Specify a valid version tag"
            )
        
        # Find the downloaded chart file
        chart_files = list(job_dir.glob("*.tgz"))
        if not chart_files:
            # Check if there are any files at all
            all_files = list(job_dir.glob("*"))
            if all_files:
                self._log(job_dir, f"Found files in job directory: {[f.name for f in all_files]}")
            
            raise PackagingError(
                f"Chart file not found after pull: {oci_chart}.\n"
                f"Helm pull completed but no .tgz file was created.\n\n"
                f"This could mean:\n"
                f"1. The chart doesn't exist at this location\n"
                f"2. The version specified doesn't exist\n"
                f"3. Authentication failed silently\n"
                f"4. The registry returned an error that wasn't caught\n\n"
                f"Check the logs above for the actual Helm error message."
            )
        
        chart_file = chart_files[0]
        self._log(job_dir, f"Chart downloaded: {chart_file.name}")
        
        return chart_file
    
    def _extract_chart(self, chart_file: Path, job_dir: Path) -> Path:
        """Extract chart archive and return path to chart directory."""
        extract_dir = job_dir / "chart_extracted"
        extract_dir.mkdir(exist_ok=True)
        
        self._log(job_dir, f"Extracting chart: {chart_file.name}")
        
        with tarfile.open(chart_file, "r:gz") as tar:
            tar.extractall(extract_dir)
        
        # Find the chart directory (usually named chart-version)
        chart_dirs = [d for d in extract_dir.iterdir() if d.is_dir()]
        if not chart_dirs:
            raise PackagingError("No chart directory found in archive")
        
        chart_dir = chart_dirs[0]
        self._log(job_dir, f"Chart extracted to: {chart_dir.name}")
        
        # Parse Chart.yaml to find dependencies and add required repositories
        chart_yaml_path = chart_dir / "Chart.yaml"
        if chart_yaml_path.exists():
            self._add_dependency_repositories(chart_yaml_path, job_dir)
        
        # Build dependencies
        self._log(job_dir, "Building chart dependencies")
        dep_cmd = [self.helm_bin, "dependency", "build", str(chart_dir)]
        self._run_command(dep_cmd, job_dir)
        
        return chart_dir
    
    def _add_dependency_repositories(self, chart_yaml_path: Path, job_dir: Path):
        """Parse Chart.yaml and add any required Helm repositories for dependencies."""
        try:
            with open(chart_yaml_path, "r") as f:
                chart_data = yaml.safe_load(f)
            
            dependencies = chart_data.get("dependencies", [])
            if not dependencies:
                self._log(job_dir, "No dependencies found in Chart.yaml")
                return
            
            self._log(job_dir, f"Found {len(dependencies)} dependencies in Chart.yaml")
            
            # Track added repositories to avoid duplicates
            added_repos = {}
            
            for dep in dependencies:
                repo_url = dep.get("repository")
                if not repo_url:
                    continue
                
                # Skip OCI dependencies (they start with oci://)
                if repo_url.startswith("oci://"):
                    self._log(job_dir, f"Skipping OCI dependency: {repo_url}")
                    continue
                
                # Check if repository is already added
                if repo_url in added_repos:
                    continue
                
                # Generate a unique repo name
                repo_name = f"dep_repo_{uuid.uuid4().hex[:8]}"
                
                try:
                    self._log(job_dir, f"Adding dependency repository: {repo_url}")
                    add_cmd = [self.helm_bin, "repo", "add", repo_name, repo_url]
                    self._run_command(add_cmd, job_dir)
                    
                    # Update the repository
                    update_cmd = [self.helm_bin, "repo", "update", repo_name]
                    self._run_command(update_cmd, job_dir)
                    
                    added_repos[repo_url] = repo_name
                    self._log(job_dir, f"Successfully added repository: {repo_url} as {repo_name}")
                    
                except PackagingError as e:
                    self._log(job_dir, f"WARNING: Failed to add repository {repo_url}: {e}")
                    # Continue with other repositories
                    continue
            
            if added_repos:
                self._log(job_dir, f"Added {len(added_repos)} dependency repositories")
            
        except Exception as e:
            self._log(job_dir, f"WARNING: Failed to parse Chart.yaml for dependencies: {e}")
            # Don't fail the whole process, just log the warning
    
    def _render_chart_templates(self, chart_dir: Path, values_yaml: Optional[str], job_dir: Path) -> Path:
        """Render Helm chart templates and return path to rendered manifests."""
        render_dir = job_dir / "rendered"
        render_dir.mkdir(exist_ok=True)
        
        self._log(job_dir, "Rendering Helm chart templates")
        
        # Write values to temp file if provided
        values_file = None
        if values_yaml:
            values_file = job_dir / "values.yaml"
            with open(values_file, "w") as f:
                f.write(values_yaml)
        
        # Render templates
        template_cmd = [self.helm_bin, "template", "chart", str(chart_dir)]
        if values_file:
            template_cmd.extend(["-f", str(values_file)])
        template_cmd.extend(["--output-dir", str(render_dir)])
        
        self._run_command(template_cmd, job_dir)
        
        return render_dir
    
    def _parse_images_from_yaml(self, yaml_content: str) -> Set[str]:
        """Parse container images from YAML content."""
        images = set()
        
        # Try to parse as YAML
        try:
            docs = yaml.safe_load_all(yaml_content)
            for doc in docs:
                if not doc or not isinstance(doc, dict):
                    continue
                
                # Recursively search for image fields
                self._extract_images_from_dict(doc, images)
        except yaml.YAMLError:
            # If YAML parsing fails, try regex fallback
            self._extract_images_with_regex(yaml_content, images)
        
        return images
    
    def _extract_images_from_dict(self, obj: dict, images: Set[str], path: str = ""):
        """Recursively extract image references from a dictionary."""
        if isinstance(obj, dict):
            # Check for direct 'image' field
            if "image" in obj and isinstance(obj["image"], str):
                image = obj["image"].strip()
                if image:
                    images.add(image)
            
            # Check for repository + tag pattern
            if "repository" in obj and isinstance(obj["repository"], str):
                repo = obj["repository"].strip()
                tag = obj.get("tag", "latest")
                if isinstance(tag, str) and repo:
                    image = f"{repo}:{tag}" if tag else repo
                    images.add(image)
            
            # Recursively process nested dictionaries
            for key, value in obj.items():
                self._extract_images_from_dict(value, images, f"{path}.{key}" if path else key)
        
        elif isinstance(obj, list):
            for item in obj:
                self._extract_images_from_dict(item, images, path)
    
    def _extract_images_with_regex(self, content: str, images: Set[str]):
        """Fallback regex-based image extraction."""
        # Pattern for image: field
        image_pattern = r'image:\s*["\']?([^\s"\']+)["\']?'
        matches = re.findall(image_pattern, content, re.IGNORECASE)
        images.update(matches)
        
        # Pattern for repository: + tag: combination
        repo_pattern = r'repository:\s*["\']?([^\s"\']+)["\']?'
        tag_pattern = r'tag:\s*["\']?([^\s"\']+)["\']?'
        repos = re.findall(repo_pattern, content, re.IGNORECASE)
        tags = re.findall(tag_pattern, content, re.IGNORECASE)
        
        for repo in repos:
            if tags:
                for tag in tags:
                    images.add(f"{repo}:{tag}")
            else:
                images.add(f"{repo}:latest")
    
    def _normalize_image(self, image: str) -> str:
        """Normalize image reference to ensure it has a tag."""
        image = image.strip()
        if not image:
            return None
        
        # Remove any quotes
        image = image.strip('"\'')
        
        # Check if image already has a tag
        if ":" in image:
            # Check if it's a port number (e.g., registry:5000/image)
            parts = image.split("/")
            last_part = parts[-1]
            if ":" in last_part:
                # It's a tag
                return image
        
        # No tag found, add :latest
        return f"{image}:latest"
    
    def _discover_images(self, render_dir: Path, job_dir: Path) -> List[str]:
        """Discover all container images from rendered manifests."""
        self._log(job_dir, "Discovering container images from rendered manifests")
        
        images = set()
        
        # Walk through all YAML files
        for yaml_file in render_dir.rglob("*.yaml"):
            with open(yaml_file, "r") as f:
                content = f.read()
                file_images = self._parse_images_from_yaml(content)
                images.update(file_images)
        
        for yaml_file in render_dir.rglob("*.yml"):
            with open(yaml_file, "r") as f:
                content = f.read()
                file_images = self._parse_images_from_yaml(content)
                images.update(file_images)
        
        # Also check the original chart values
        chart_dir = job_dir / "chart_extracted"
        if chart_dir.exists():
            values_files = list(chart_dir.rglob("values.yaml")) + list(chart_dir.rglob("values.yml"))
            for values_file in values_files:
                with open(values_file, "r") as f:
                    content = f.read()
                    file_images = self._parse_images_from_yaml(content)
                    images.update(file_images)
        
        # Normalize all images (ensure they have tags)
        normalized_images = set()
        for img in images:
            normalized = self._normalize_image(img)
            if normalized:
                normalized_images.add(normalized)
        
        images_list = sorted(list(normalized_images))
        self._log(job_dir, f"Found {len(images_list)} image references (after normalization)")
        
        # Save images list
        images_json = job_dir / "images.json"
        with open(images_json, "w") as f:
            json.dump(images_list, f, indent=2)
        
        return images_list
    
    def _get_image_digest(self, image: str, job_dir: Path) -> Optional[str]:
        """Resolve image reference to its digest (e.g. sha256:abc...). Returns None on failure."""
        try:
            digest_cmd = [self.crane_bin, "digest", image]
            result = subprocess.run(
                digest_cmd,
                capture_output=True,
                text=True,
                timeout=settings.IMAGE_PULL_TIMEOUT,
                cwd=str(job_dir),
                env=self._crane_env(),
            )
            if result.returncode != 0:
                return None
            digest = (result.stdout or "").strip()
            if digest and (digest.startswith("sha256:") or digest.startswith("sha512:")):
                return digest
            return None
        except Exception:
            return None
    
    def _download_one_image(self, ref: str, digest: Optional[str], images_dir: Path, job_dir: Path) -> Tuple[str, bool, Optional[str]]:
        """Pull one image; return (ref, success, error_message). Used by parallel worker."""
        if digest:
            safe_name = digest.replace(":", "_").replace("/", "_")
            image_file = images_dir / f"{safe_name}.tar"
        else:
            safe_name = ref.replace("/", "_").replace(":", "_").replace("@", "_")
            image_file = images_dir / f"{safe_name}.tar"
        try:
            pull_cmd = [self.crane_bin, "pull", ref, str(image_file)]
            self._run_command(pull_cmd, job_dir)
            if image_file.exists() and image_file.stat().st_size > 0:
                self._log(job_dir, f"✓ Image saved: {image_file.name} ({image_file.stat().st_size / 1024 / 1024:.2f} MB)")
                return (ref, True, None)
            return (ref, False, "Image file was not created or is empty")
        except PackagingError as e:
            return (ref, False, str(e))
        except Exception as e:
            return (ref, False, str(e))

    def _download_images(self, images: List[str], job_dir: Path):
        """Download container images using crane. Parallel pulls, fail on first error, per-image status."""
        if not images:
            self._log(job_dir, "No images to download")
            return
        
        images_dir = job_dir / "images"
        images_dir.mkdir(exist_ok=True)
        progress_lock = threading.Lock()
        
        # Resolve digests and deduplicate
        ref_to_digest: Dict[str, Optional[str]] = {}
        digest_to_ref: Dict[str, str] = {}
        self._log(job_dir, "Resolving image digests to skip duplicate downloads...")
        for idx, image in enumerate(images, 1):
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            self._write_progress(
                job_dir, "download_images", 32 + (8 * idx / max(len(images), 1)),
                f"Resolving digest {idx}/{len(images)}: {image[:50]}...",
                {"current": idx, "total": len(images), "phase": "digest"}
            )
            digest = self._get_image_digest(image, job_dir)
            ref_to_digest[image] = digest
            if digest and digest not in digest_to_ref:
                digest_to_ref[digest] = image
        
        downloads: List[Tuple[str, Optional[str]]] = []
        for digest, ref in digest_to_ref.items():
            downloads.append((ref, digest))
        for image in images:
            if ref_to_digest.get(image) is None:
                downloads.append((image, None))
        unique_count = len(downloads)
        if unique_count < len(images):
            self._log(job_dir, f"Deduplicated: {len(images)} references -> {unique_count} unique images (by digest)")
        
        if unique_count > settings.MAX_IMAGES:
            raise PackagingError(
                f"Too many unique images ({unique_count}). Maximum allowed: {settings.MAX_IMAGES}. "
                f"Increase MAX_IMAGES in configuration if needed."
            )
        
        self._log(job_dir, f"Downloading {unique_count} unique container images (up to {settings.IMAGE_PULL_PARALLEL} in parallel)")
        
        # Per-image status for UI: list of {ref, status} where status = pending | in_progress | success | failed
        image_status_list: List[Dict[str, str]] = [{"ref": ref, "status": "pending"} for ref, _ in downloads]
        
        def write_progress_with_images(percent: float, message: str, done: int, failed: int):
            with progress_lock:
                details = {
                    "current": done + failed,
                    "total": unique_count,
                    "images": list(image_status_list),
                    "successful": done,
                    "failed": failed,
                }
                self._write_progress(job_dir, "download_images", percent, message, details)
        
        write_progress_with_images(40, f"Downloading 0/{unique_count} images", 0, 0)
        
        max_workers = min(settings.IMAGE_PULL_PARALLEL, unique_count)
        done_count = 0
        first_failure_ref: Optional[str] = None
        first_failure_msg: Optional[str] = None
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_ref = {
                executor.submit(self._download_one_image, ref, digest, images_dir, job_dir): ref
                for ref, digest in downloads
            }
            for future in as_completed(future_to_ref):
                if self._check_cancelled(job_dir):
                    raise JobCancelledError("Job was cancelled by user")
                ref = future_to_ref[future]
                try:
                    result_ref, success, error_msg = future.result()
                except Exception as e:
                    success, error_msg = False, str(e)
                # Find index for this ref and update status
                for i, item in enumerate(image_status_list):
                    if item["ref"] == ref:
                        item["status"] = "success" if success else "failed"
                        if not success:
                            first_failure_ref = ref
                            first_failure_msg = error_msg or "Unknown error"
                        break
                if success:
                    done_count += 1
                else:
                    # Fail fast: stop and raise on first failure
                    pct = 40 + (50 * (done_count + 1) / unique_count) if unique_count else 90
                    write_progress_with_images(min(90, pct), f"Failed: {ref}", done_count, 1)
                    self._log(job_dir, f"✗ Failed to download {ref}: {first_failure_msg}")
                    error_detail = first_failure_msg or "Download failed"
                    if "unauthorized" in error_detail.lower() or "401" in error_detail:
                        error_detail = f"Authentication failed for {ref}. Check registry credentials."
                    elif "not found" in error_detail.lower() or "404" in error_detail:
                        error_detail = f"Image not found: {ref}. Verify name and tag."
                    elif "rate" in error_detail.lower() or "429" in error_detail:
                        error_detail = f"Rate limit for {ref}. Wait a few minutes and retry."
                    raise PackagingError(
                        f"Image download failed (job stops on first failure).\n\n"
                        f"Failed image: {ref}\n"
                        f"Error: {error_detail}\n\n"
                        f"Fix the issue and retry the job."
                    )
                pct = 40 + (50 * (done_count) / unique_count) if unique_count else 90
                write_progress_with_images(min(90, pct), f"Downloading {done_count}/{unique_count} images", done_count, 0)
        
        self._write_progress(job_dir, "download_images", 90, "Images download complete", {
            "successful": done_count,
            "failed": 0,
            "images": image_status_list,
        })
        self._log(job_dir, f"Image download summary: {done_count} successful")
    
    def _create_bundle_readme(self, job_dir: Path, chart_name: str, chart_version: str, images: List[str]) -> str:
        """Create README with instructions for air-gap deployment."""
        readme_content = f"""# Helm Chart Air-Gap Bundle

## Chart Information
- **Chart Name**: {chart_name}
- **Chart Version**: {chart_version}
- **Packaged At**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")}
- **Container Image References**: {len(images)} (duplicate refs that resolve to the same image are stored once)

## Bundle Contents
- `chart/` - Helm chart package and dependencies
- `images/` - Container images as tar archives (one `.tar` file per unique image by digest; duplicate references in the chart are downloaded once)
- `metadata.json` - Bundle metadata and image list
- `README.md` - This file

## Deployment Instructions

### Step 1: Transfer Bundle to Air-Gapped Environment

Copy this entire bundle to your air-gapped environment using a USB drive, network transfer, or other secure method.

### Step 2: Extract the Bundle

```bash
# Extract the bundle
tar -xzf bundle-{chart_name}-{chart_version}-*.tar.gz
cd bundle-{chart_name}-{chart_version}-*/
```

### Step 3: Load Container Images

Load all container images into your container runtime:

**Using Docker:**
```bash
# Load all images
for img in images/*.tar; do
  echo "Loading $img..."
  docker load < "$img"
done
```

**Using Podman:**
```bash
# Load all images
for img in images/*.tar; do
  echo "Loading $img..."
  podman load < "$img"
done
```

### Step 4: Tag and Push Images to Private Registry (if needed)

If your air-gapped environment uses a private registry, tag and push each image:

```bash
# Example for each image:
docker tag <original-image-name>:<tag> <your-registry>/<image-name>:<tag>
docker push <your-registry>/<image-name>:<tag>
```

**Note**: You may need to update your Helm values.yaml to reference the new registry paths.

### Step 5: Install the Helm Chart

```bash
# Extract the chart
cd chart/
tar -xzf {chart_name}-{chart_version}.tgz
cd {chart_name}/

# Install the chart
helm install <your-release-name> . \\
  --namespace <your-namespace> \\
  --create-namespace

# Or with custom values:
helm install <your-release-name> . \\
  --namespace <your-namespace> \\
  --create-namespace \\
  -f values.yaml
```

### Step 6: Verify Installation

```bash
# Check the release
helm list -n <your-namespace>

# Check pods
kubectl get pods -n <your-namespace>

# View logs
kubectl logs -n <your-namespace> -l app=<your-app-label>
```

## Container Images Included

The chart references the following container images. When multiple references resolve to the same image (same digest), only one copy is stored in `images/`.

"""
        for idx, image in enumerate(images, 1):
            readme_content += f"{idx}. `{image}`\n"
        
        readme_content += f"""
## Important Notes

1. **Image Registry**: Ensure your Kubernetes cluster can access the container images. You may need to:
   - Push images to a private registry accessible from your cluster
   - Pre-load images on all cluster nodes
   - Configure image pull secrets if using a private registry

2. **Image References**: If you push images to a different registry, update your Helm values.yaml to reflect the new image paths.

3. **Network Policies**: Ensure any required network policies allow access to your image registry.

4. **Resource Requirements**: Verify that your cluster has sufficient resources (CPU, memory, storage) for the chart.

5. **Dependencies**: Some charts may require additional resources or configurations. Review the chart's README or values.yaml for specific requirements.

## Troubleshooting

- **Image pull errors**: Verify images are loaded or accessible from your registry
- **Chart installation fails**: Check Helm and Kubernetes versions compatibility
- **Missing dependencies**: Review the chart's requirements.yaml for any additional dependencies

## Support

For issues with this bundle, refer to:
- The original chart's documentation
- Helm documentation: https://helm.sh/docs/
- Kubernetes documentation: https://kubernetes.io/docs/

---
Generated by Helm Airgap Packager
"""
        
        return readme_content
    
    def _build_bundle(self, job_dir: Path, chart_file: Path, chart_name: str, chart_version: str, 
                     images: List[str], bundle_name: Optional[str], job_id: str) -> str:
        """Build the final bundle tarball."""
        self._log(job_dir, "Building final bundle")
        
        # Generate bundle filename
        if not bundle_name:
            safe_chart_name = chart_name.replace("/", "-")
            bundle_name = f"bundle-{safe_chart_name}-{chart_version}-{job_id[:8]}.tar.gz"
        else:
            if not bundle_name.endswith(".tar.gz"):
                bundle_name += ".tar.gz"
        
        bundle_path = self.bundles_dir / bundle_name
        
        # Create bundle structure
        bundle_temp = job_dir / "bundle_temp"
        bundle_temp.mkdir(exist_ok=True)
        
        # Copy chart
        chart_dest = bundle_temp / "chart"
        chart_dest.mkdir(exist_ok=True)
        shutil.copy2(chart_file, chart_dest / chart_file.name)
        
        # Copy images if they exist
        images_dir = job_dir / "images"
        if images_dir.exists() and any(images_dir.iterdir()):
            images_dest = bundle_temp / "images"
            shutil.copytree(images_dir, images_dest, dirs_exist_ok=True)
        
        # Create README
        readme_content = self._create_bundle_readme(job_dir, chart_name, chart_version, images)
        readme_path = bundle_temp / "README.md"
        with open(readme_path, "w") as f:
            f.write(readme_content)
        
        # Create metadata
        metadata = {
            "chart_name": chart_name,
            "chart_version": chart_version,
            "images": images,
            "images_count": len(images),
            "created_at": datetime.now().isoformat(),
            "job_id": job_id
        }
        metadata_path = bundle_temp / "metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)
        
        # Create tarball
        self._log(job_dir, f"Creating bundle archive: {bundle_name}")
        with tarfile.open(bundle_path, "w:gz") as tar:
            tar.add(bundle_temp, arcname=".", recursive=True)
        
        self._log(job_dir, f"Bundle created: {bundle_path}")
        
        return bundle_name
    
    def package_from_repo(self, repo_url: str, chart_name: str, chart_version: Optional[str] = None,
                         values_yaml: Optional[str] = None, bundle_name: Optional[str] = None,
                         include_images: bool = True, _job_id: Optional[str] = None,
                         _job_dir: Optional[Path] = None) -> str:
        """Package a Helm chart from a repository."""
        if _job_id is not None and _job_dir is not None:
            job_id, job_dir = _job_id, _job_dir
        else:
            job_id, job_dir = self._create_job_dir()
        
        try:
            self._log(job_dir, f"Starting packaging job: {job_id}")
            self._write_progress(job_dir, "fetch_chart", 0, "Fetching chart from repository", {"chart": chart_name})
            
            # Initialize metadata (save early so jobs list shows chart and created time for in-progress jobs)
            # Store all parameters for retry functionality
            metadata = {
                "job_id": job_id,
                "status": "in_progress",
                "source_type": "repo",
                "repo_url": repo_url,
                "chart_name": chart_name,
                "chart_version": chart_version or "latest",
                "values_yaml": values_yaml,
                "bundle_name": bundle_name,
                "include_images": include_images,
                "created_at": datetime.now().isoformat()
            }
            self._save_job_metadata(job_dir, metadata)
            
            # Step 1: Fetch chart
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            chart_file = self._fetch_chart_from_repo(repo_url, chart_name, chart_version, job_dir)
            self._write_progress(job_dir, "fetch_chart", 15, "Chart downloaded")
            
            # Step 2: Extract chart
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            self._write_progress(job_dir, "extract_chart", 20, "Extracting chart and building dependencies")
            chart_dir = self._extract_chart(chart_file, job_dir)
            
            # Step 3: Render templates
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            self._write_progress(job_dir, "render_templates", 28, "Rendering Helm templates")
            render_dir = self._render_chart_templates(chart_dir, values_yaml, job_dir)
            
            # Step 4: Discover images
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            self._write_progress(job_dir, "discover_images", 32, "Discovering container images")
            images = self._discover_images(render_dir, job_dir)
            metadata["images"] = images
            metadata["images_count"] = len(images)
            
            # Step 5: Download images
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            if include_images and images:
                self._download_images(images, job_dir)
            else:
                self._write_progress(job_dir, "download_images", 90, "Skipping images (none or disabled)")
            
            # Step 6: Build bundle
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            self._write_progress(job_dir, "build_bundle", 92, "Building bundle archive")
            bundle_filename = self._build_bundle(
                job_dir, chart_file, chart_name, chart_version or "latest",
                images, bundle_name, job_id
            )
            
            self._write_progress(job_dir, "done", 100, "Packaging complete", {"bundle": bundle_filename})
            # Update metadata
            metadata["status"] = "success"
            metadata["bundle_filename"] = bundle_filename
            metadata["completed_at"] = datetime.now().isoformat()
            
            self._save_job_metadata(job_dir, metadata)
            self._log(job_dir, "Packaging completed successfully")
            
            return job_id
            
        except JobCancelledError as e:
            self._write_progress(job_dir, "cancelled", 0, "Job cancelled by user", {})
            metadata["status"] = "cancelled"
            metadata["error_message"] = str(e)
            metadata["cancelled_at"] = datetime.now().isoformat()
            self._save_job_metadata(job_dir, metadata)
            self._log(job_dir, "Job cancelled by user")
            raise
        except Exception as e:
            self._write_progress(job_dir, "error", 0, str(e), {"error": str(e)})
            # Update metadata with error (preserve original parameters for retry)
            metadata["status"] = "error"
            metadata["error_message"] = str(e)
            if "created_at" not in metadata:
                metadata["created_at"] = datetime.now().isoformat()
            metadata["failed_at"] = datetime.now().isoformat()
            self._save_job_metadata(job_dir, metadata)
            self._log(job_dir, f"Packaging failed: {e}")
            raise
    
    def package_from_oci(self, oci_chart: str, chart_version: Optional[str] = None,
                        values_yaml: Optional[str] = None, bundle_name: Optional[str] = None,
                        include_images: bool = True, _job_id: Optional[str] = None,
                        _job_dir: Optional[Path] = None) -> str:
        """Package a Helm chart from an OCI registry."""
        if _job_id is not None and _job_dir is not None:
            job_id, job_dir = _job_id, _job_dir
        else:
            job_id, job_dir = self._create_job_dir()
        
        try:
            self._log(job_dir, f"Starting packaging job: {job_id}")
            self._write_progress(job_dir, "fetch_chart", 0, "Fetching chart from OCI registry", {"chart": oci_chart})
            
            # Extract chart name from OCI URL (remove oci:// prefix and get last part)
            chart_path = oci_chart.replace("oci://", "")
            chart_name = chart_path.split("/")[-1] if "/" in chart_path else chart_path
            # Remove version tag if present in URL (e.g., oci://registry/chart:1.0.0)
            if ":" in chart_name:
                chart_name = chart_name.split(":")[0]
            
            # Initialize metadata (store all parameters for retry)
            metadata = {
                "job_id": job_id,
                "status": "in_progress",
                "source_type": "oci",
                "oci_chart": oci_chart,
                "chart_name": chart_name,
                "chart_version": chart_version or "latest",
                "values_yaml": values_yaml,
                "bundle_name": bundle_name,
                "include_images": include_images,
                "created_at": datetime.now().isoformat()
            }
            
            # Step 1: Fetch chart
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            chart_file = self._fetch_chart_from_oci(oci_chart, chart_version, job_dir)
            self._write_progress(job_dir, "fetch_chart", 15, "Chart downloaded")
            
            # Extract version from filename if not specified
            if not chart_version and "-" in chart_file.stem:
                parts = chart_file.stem.rsplit("-", 1)
                if len(parts) == 2:
                    # Try to parse as version (should be semver-like)
                    potential_version = parts[1]
                    if re.match(r'^[\d]+\.[\d]+', potential_version):
                        chart_version = potential_version
                        chart_name = parts[0]
            
            metadata["chart_name"] = chart_name
            metadata["chart_version"] = chart_version or "latest"
            self._save_job_metadata(job_dir, metadata)
            
            # Step 2: Extract chart
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            self._write_progress(job_dir, "extract_chart", 20, "Extracting chart and building dependencies")
            chart_dir = self._extract_chart(chart_file, job_dir)
            
            # Step 3: Render templates
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            self._write_progress(job_dir, "render_templates", 28, "Rendering Helm templates")
            render_dir = self._render_chart_templates(chart_dir, values_yaml, job_dir)
            
            # Step 4: Discover images
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            self._write_progress(job_dir, "discover_images", 32, "Discovering container images")
            images = self._discover_images(render_dir, job_dir)
            metadata["images"] = images
            metadata["images_count"] = len(images)
            
            # Step 5: Download images
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            if include_images and images:
                self._download_images(images, job_dir)
            else:
                self._write_progress(job_dir, "download_images", 90, "Skipping images (none or disabled)")
            
            # Step 6: Build bundle
            if self._check_cancelled(job_dir):
                raise JobCancelledError("Job was cancelled by user")
            self._write_progress(job_dir, "build_bundle", 92, "Building bundle archive")
            bundle_filename = self._build_bundle(
                job_dir, chart_file, chart_name, chart_version,
                images, bundle_name, job_id
            )
            
            self._write_progress(job_dir, "done", 100, "Packaging complete", {"bundle": bundle_filename})
            # Update metadata
            metadata["status"] = "success"
            metadata["bundle_filename"] = bundle_filename
            metadata["completed_at"] = datetime.now().isoformat()
            
            self._save_job_metadata(job_dir, metadata)
            self._log(job_dir, "Packaging completed successfully")
            
            return job_id
            
        except JobCancelledError as e:
            self._write_progress(job_dir, "cancelled", 0, "Job cancelled by user", {})
            metadata["status"] = "cancelled"
            metadata["error_message"] = str(e)
            metadata["cancelled_at"] = datetime.now().isoformat()
            self._save_job_metadata(job_dir, metadata)
            self._log(job_dir, "Job cancelled by user")
            raise
        except Exception as e:
            self._write_progress(job_dir, "error", 0, str(e), {"error": str(e)})
            # Update metadata with error (preserve original parameters for retry)
            metadata["status"] = "error"
            metadata["error_message"] = str(e)
            if "created_at" not in metadata:
                metadata["created_at"] = datetime.now().isoformat()
            metadata["failed_at"] = datetime.now().isoformat()
            self._save_job_metadata(job_dir, metadata)
            self._log(job_dir, f"Packaging failed: {e}")
            raise
