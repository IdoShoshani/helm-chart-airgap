"""Core packaging logic for Helm charts and container images."""
import json
import os
import re
import shutil
import subprocess
import tarfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set

import yaml

from . import settings


class PackagingError(Exception):
    """Custom exception for packaging errors."""
    pass


class HelmPackager:
    """Handles packaging of Helm charts for air-gap environments."""
    
    def __init__(self):
        self.helm_bin = settings.HELM_BIN
        self.crane_bin = settings.CRANE_BIN
        self.jobs_dir = settings.JOBS_DIR
        self.bundles_dir = settings.BUNDLES_DIR
        
        # Verify tools exist
        self._verify_tools()
    
    def _verify_tools(self):
        """Verify that required tools are available."""
        if not shutil.which(self.helm_bin):
            raise PackagingError(f"Helm binary not found at {self.helm_bin}")
        if not shutil.which(self.crane_bin):
            raise PackagingError(f"Crane binary not found at {self.crane_bin}")
    
    def _log(self, job_dir: Path, message: str):
        """Append a log message to the job log file."""
        log_file = job_dir / "log.txt"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
    
    def _run_command(self, cmd: List[str], job_dir: Path, check: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command and log output."""
        self._log(job_dir, f"Running: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=settings.HELM_TIMEOUT if "helm" in cmd[0] else settings.IMAGE_PULL_TIMEOUT,
                check=check
            )
            if result.stdout:
                # Limit stdout logging to last 1000 chars to avoid huge logs
                stdout_preview = result.stdout[-1000:] if len(result.stdout) > 1000 else result.stdout
                self._log(job_dir, f"STDOUT: {stdout_preview}")
            if result.stderr:
                # Limit stderr logging to last 1000 chars
                stderr_preview = result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr
                self._log(job_dir, f"STDERR: {stderr_preview}")
            return result
        except subprocess.TimeoutExpired:
            timeout_seconds = settings.HELM_TIMEOUT if "helm" in cmd[0] else settings.IMAGE_PULL_TIMEOUT
            self._log(job_dir, f"ERROR: Command timed out after {timeout_seconds}s: {' '.join(cmd)}")
            raise PackagingError(
                f"Operation timed out after {timeout_seconds} seconds. "
                f"This may indicate network issues or the chart/images are very large. "
                f"Try increasing timeout settings in your configuration."
            )
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr or e.stdout or "Unknown error"
            # Extract meaningful error message
            if "not found" in error_msg.lower():
                error_msg = f"Resource not found. Check that the chart/image exists and is accessible."
            elif "unauthorized" in error_msg.lower() or "authentication" in error_msg.lower():
                error_msg = f"Authentication failed. Check your registry credentials."
            elif "connection" in error_msg.lower() or "network" in error_msg.lower():
                error_msg = f"Network error. Check your internet connection and repository accessibility."
            
            self._log(job_dir, f"ERROR: Command failed with exit code {e.returncode}: {error_msg}")
            raise PackagingError(f"Failed to execute: {' '.join(cmd)}\nError: {error_msg}")
    
    def _create_job_dir(self) -> tuple[str, Path]:
        """Create a new job directory and return job ID and path."""
        job_id = str(uuid.uuid4())
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_id, job_dir
    
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
    
    def _fetch_chart_from_oci(self, oci_chart: str, job_dir: Path) -> Path:
        """Fetch Helm chart from OCI registry."""
        self._log(job_dir, f"Fetching chart from OCI: {oci_chart}")
        
        if not oci_chart.startswith("oci://"):
            raise PackagingError("OCI chart URL must start with 'oci://'")
        
        # Pull chart
        pull_cmd = [self.helm_bin, "pull", oci_chart, "--destination", str(job_dir)]
        self._run_command(pull_cmd, job_dir)
        
        # Find the downloaded chart file
        chart_files = list(job_dir.glob("*.tgz"))
        if not chart_files:
            raise PackagingError(f"Chart file not found after pull: {oci_chart}")
        
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
        
        # Build dependencies
        self._log(job_dir, "Building chart dependencies")
        dep_cmd = [self.helm_bin, "dependency", "build", str(chart_dir)]
        self._run_command(dep_cmd, job_dir)
        
        return chart_dir
    
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
        
        images_list = sorted(list(images))
        self._log(job_dir, f"Found {len(images_list)} unique images")
        
        # Save images list
        images_json = job_dir / "images.json"
        with open(images_json, "w") as f:
            json.dump(images_list, f, indent=2)
        
        return images_list
    
    def _download_images(self, images: List[str], job_dir: Path):
        """Download container images using crane."""
        if not images:
            self._log(job_dir, "No images to download")
            return
        
        images_dir = job_dir / "images"
        images_dir.mkdir(exist_ok=True)
        
        self._log(job_dir, f"Downloading {len(images)} container images")
        
        # Check max images limit
        if len(images) > settings.MAX_IMAGES:
            raise PackagingError(
                f"Too many images ({len(images)}). Maximum allowed: {settings.MAX_IMAGES}. "
                f"Increase MAX_IMAGES in configuration if needed."
            )
        
        failed_images = []
        successful_images = []
        
        for idx, image in enumerate(images, 1):
            self._log(job_dir, f"Downloading image {idx}/{len(images)}: {image}")
            
            # Sanitize image name for filename
            safe_name = image.replace("/", "_").replace(":", "_")
            image_file = images_dir / f"{safe_name}.tar"
            
            try:
                # Use crane pull to download and save directly
                # crane pull handles authentication via environment variables or docker config
                pull_cmd = [self.crane_bin, "pull", image]
                save_cmd = [self.crane_bin, "save", image, "-o", str(image_file)]
                
                # Pull first (this handles authentication)
                self._run_command(pull_cmd, job_dir)
                
                # Then save to tar file
                self._run_command(save_cmd, job_dir)
                
                if image_file.exists() and image_file.stat().st_size > 0:
                    successful_images.append(image)
                    self._log(job_dir, f"✓ Image saved: {image_file.name} ({image_file.stat().st_size / 1024 / 1024:.2f} MB)")
                else:
                    raise PackagingError(f"Image file was not created or is empty")
                    
            except PackagingError as e:
                failed_images.append(image)
                self._log(job_dir, f"✗ WARNING: Failed to download image {image}: {e}")
                # Continue with other images
                continue
        
        # Log summary
        self._log(job_dir, f"Image download summary: {len(successful_images)} successful, {len(failed_images)} failed")
        
        if failed_images:
            self._log(job_dir, f"Failed images: {', '.join(failed_images)}")
            if len(failed_images) == len(images):
                raise PackagingError(
                    f"Failed to download any images. "
                    f"Check network connectivity, registry authentication, and image availability. "
                    f"Failed images: {', '.join(failed_images)}"
                )
            else:
                self._log(job_dir, "WARNING: Some images failed to download. Bundle may be incomplete.")
    
    def _create_bundle_readme(self, job_dir: Path, chart_name: str, chart_version: str, images: List[str]) -> str:
        """Create README with instructions for air-gap deployment."""
        readme_content = f"""# Helm Chart Air-Gap Bundle

## Chart Information
- **Chart Name**: {chart_name}
- **Chart Version**: {chart_version}
- **Packaged At**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")}
- **Container Images**: {len(images)}

## Bundle Contents
- `chart/` - Helm chart package and dependencies
- `images/` - Container images as tar archives (one `.tar` file per image)
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
  --namespace <your-namespace} \\
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
kubectl logs -n <your-namespace} -l app=<your-app-label>
```

## Container Images Included

This bundle contains the following container images:

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
                         include_images: bool = True) -> str:
        """Package a Helm chart from a repository."""
        job_id, job_dir = self._create_job_dir()
        
        try:
            self._log(job_dir, f"Starting packaging job: {job_id}")
            
            # Initialize metadata
            metadata = {
                "job_id": job_id,
                "status": "in_progress",
                "source_type": "repo",
                "repo_url": repo_url,
                "chart_name": chart_name,
                "chart_version": chart_version or "latest",
                "created_at": datetime.now().isoformat(),
                "include_images": include_images
            }
            
            # Step 1: Fetch chart
            chart_file = self._fetch_chart_from_repo(repo_url, chart_name, chart_version, job_dir)
            
            # Step 2: Extract chart
            chart_dir = self._extract_chart(chart_file, job_dir)
            
            # Step 3: Render templates
            render_dir = self._render_chart_templates(chart_dir, values_yaml, job_dir)
            
            # Step 4: Discover images
            images = self._discover_images(render_dir, job_dir)
            metadata["images"] = images
            metadata["images_count"] = len(images)
            
            # Step 5: Download images
            if include_images and images:
                self._download_images(images, job_dir)
            
            # Step 6: Build bundle
            bundle_filename = self._build_bundle(
                job_dir, chart_file, chart_name, chart_version or "latest",
                images, bundle_name, job_id
            )
            
            # Update metadata
            metadata["status"] = "success"
            metadata["bundle_filename"] = bundle_filename
            metadata["completed_at"] = datetime.now().isoformat()
            
            self._save_job_metadata(job_dir, metadata)
            self._log(job_dir, "Packaging completed successfully")
            
            return job_id
            
        except Exception as e:
            # Update metadata with error
            metadata = {
                "job_id": job_id,
                "status": "error",
                "error_message": str(e),
                "created_at": datetime.now().isoformat(),
                "failed_at": datetime.now().isoformat()
            }
            self._save_job_metadata(job_dir, metadata)
            self._log(job_dir, f"Packaging failed: {e}")
            raise
    
    def package_from_oci(self, oci_chart: str, values_yaml: Optional[str] = None,
                        bundle_name: Optional[str] = None, include_images: bool = True) -> str:
        """Package a Helm chart from an OCI registry."""
        job_id, job_dir = self._create_job_dir()
        
        try:
            self._log(job_dir, f"Starting packaging job: {job_id}")
            
            # Extract chart name from OCI URL
            chart_name = oci_chart.split("/")[-1] if "/" in oci_chart else oci_chart
            chart_version = "latest"  # Will be determined from chart
            
            # Initialize metadata
            metadata = {
                "job_id": job_id,
                "status": "in_progress",
                "source_type": "oci",
                "oci_chart": oci_chart,
                "chart_name": chart_name,
                "created_at": datetime.now().isoformat(),
                "include_images": include_images
            }
            
            # Step 1: Fetch chart
            chart_file = self._fetch_chart_from_oci(oci_chart, job_dir)
            
            # Extract version from filename if possible
            if "-" in chart_file.stem:
                parts = chart_file.stem.rsplit("-", 1)
                if len(parts) == 2:
                    chart_name = parts[0]
                    chart_version = parts[1]
            
            metadata["chart_name"] = chart_name
            metadata["chart_version"] = chart_version
            
            # Step 2: Extract chart
            chart_dir = self._extract_chart(chart_file, job_dir)
            
            # Step 3: Render templates
            render_dir = self._render_chart_templates(chart_dir, values_yaml, job_dir)
            
            # Step 4: Discover images
            images = self._discover_images(render_dir, job_dir)
            metadata["images"] = images
            metadata["images_count"] = len(images)
            
            # Step 5: Download images
            if include_images and images:
                self._download_images(images, job_dir)
            
            # Step 6: Build bundle
            bundle_filename = self._build_bundle(
                job_dir, chart_file, chart_name, chart_version,
                images, bundle_name, job_id
            )
            
            # Update metadata
            metadata["status"] = "success"
            metadata["bundle_filename"] = bundle_filename
            metadata["completed_at"] = datetime.now().isoformat()
            
            self._save_job_metadata(job_dir, metadata)
            self._log(job_dir, "Packaging completed successfully")
            
            return job_id
            
        except Exception as e:
            # Update metadata with error
            metadata = {
                "job_id": job_id,
                "status": "error",
                "error_message": str(e),
                "created_at": datetime.now().isoformat(),
                "failed_at": datetime.now().isoformat()
            }
            self._save_job_metadata(job_dir, metadata)
            self._log(job_dir, f"Packaging failed: {e}")
            raise
