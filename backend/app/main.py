"""Flask application entrypoint."""
import os
import re
import subprocess
import threading
from flask import Flask, render_template, request, redirect, url_for, send_file, flash, jsonify
from pathlib import Path
import json
import yaml
from flask_wtf.csrf import CSRFProtect

from . import settings
from .packager import HelmPackager, PackagingError, JobCancelledError
from .credential_manager import CredentialManager
from . import repo_manager

# Get the path to templates directory (one level up from app/)
BASE_DIR = Path(__file__).parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"

app = Flask(__name__, template_folder=str(TEMPLATES_DIR))
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max file upload

csrf = CSRFProtect(app)

# Initialize credential manager and sync env credentials on startup
credential_manager = CredentialManager()
credential_manager.sync_env_credentials()

# Sync stored Helm repos into helm (e.g. after container restart)
try:
    repo_manager.sync_helm_repos_from_storage()
except Exception:
    pass


def _repo_update_loop():
    """Background thread: update Helm repos periodically."""
    import time
    interval_sec = max(0, settings.REPO_UPDATE_INTERVAL_HOURS * 3600)
    if interval_sec <= 0:
        return
    while True:
        time.sleep(interval_sec)
        try:
            if repo_manager.list_repos():
                repo_manager.update_repos()
        except Exception:
            pass


if settings.REPO_UPDATE_INTERVAL_HOURS > 0:
    _repo_update_thread = threading.Thread(target=_repo_update_loop, daemon=True)
    _repo_update_thread.start()


def _index_context(extra=None):
    """Build context for the single combined page (package form + jobs list)."""
    from datetime import datetime
    repos = repo_manager.list_repos()
    repos_updated_at = repo_manager.get_last_updated()
    jobs = []
    if settings.JOBS_DIR.exists():
        for job_dir in sorted(settings.JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not job_dir.is_dir():
                continue
            job_id = job_dir.name
            job_data = {}
            job_json_path = job_dir / "job.json"
            if job_json_path.exists():
                try:
                    with open(job_json_path, "r") as f:
                        job_data = json.load(f)
                except Exception:
                    job_data = {"status": "unknown"}
            else:
                job_data = {"status": "in_progress"}
            job_data["job_id"] = job_id
            if not job_data.get("created_at"):
                try:
                    mtime = job_dir.stat().st_mtime
                    job_data["created_at"] = datetime.fromtimestamp(mtime).isoformat()
                except Exception:
                    pass
            bundle_filename = job_data.get("bundle_filename")
            job_data["bundle_exists"] = (
                bool(bundle_filename) and (settings.BUNDLES_DIR / bundle_filename).exists()
            )
            jobs.append(job_data)
    has_running_jobs = any(j.get("status") == "in_progress" for j in jobs)
    ctx = {
        "repos": repos,
        "repos_updated_at": repos_updated_at,
        "jobs": jobs,
        "has_running_jobs": has_running_jobs,
    }
    if extra:
        ctx.update(extra)
    return ctx


@app.route("/")
@app.route("/jobs")
def index():
    """Single page: jobs list + start new package form."""
    return render_template("index.html", **_index_context())


@app.route("/package", methods=["POST"])
def package():
    """Handle form submission and start packaging."""
    try:
        # Get form data
        source_type = request.form.get("source_type", "").strip()
        repo_url = request.form.get("repo_url", "").strip()
        chart_name = request.form.get("chart_name", "").strip()
        chart_version = request.form.get("chart_version", "").strip() or None
        oci_chart = request.form.get("oci_chart", "").strip()
        oci_version = request.form.get("oci_version", "").strip() or None
        values_yaml = request.form.get("values_yaml", "").strip()
        bundle_name = request.form.get("bundle_name", "").strip()
        include_images = request.form.get("include_images", "yes") == "yes"
        
        # Handle uploaded values file
        values_file = None
        if "values_file" in request.files:
            file = request.files["values_file"]
            if file.filename:
                values_file = file.read().decode("utf-8")
        
        # Use uploaded file if provided, otherwise use textarea
        if values_file:
            values_yaml = values_file
        
        # Validation
        errors = []
        if source_type == "repo":
            if not repo_url:
                errors.append("Repository URL is required")
            elif not (repo_url.startswith("http://") or repo_url.startswith("https://")):
                errors.append("Repository URL must start with http:// or https://")
            if not chart_name:
                errors.append("Chart name is required")
            elif not re.match(r'^[a-zA-Z0-9._-]+$', chart_name):
                errors.append("Chart name contains invalid characters")
        elif source_type == "oci":
            if not oci_chart:
                errors.append("OCI chart URL is required")
            elif not oci_chart.startswith("oci://"):
                errors.append("OCI chart URL must start with 'oci://'")
        else:
            errors.append("Please select a source type")
        
        # Validate bundle name if provided
        if bundle_name:
            if not re.match(r'^[a-zA-Z0-9._-]+$', bundle_name):
                errors.append("Bundle name contains invalid characters (use only letters, numbers, dots, dashes, underscores)")
        
        # Validate values YAML if provided
        if values_yaml:
            try:
                yaml.safe_load(values_yaml)
            except yaml.YAMLError as e:
                errors.append(f"Invalid YAML in values: {str(e)}")
        
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("index.html", **_index_context(extra={
                "source_type": source_type,
                "repo_url": repo_url,
                "chart_name": chart_name,
                "chart_version": chart_version,
                "oci_chart": oci_chart,
                "oci_version": oci_version,
                "values_yaml": values_yaml,
                "bundle_name": bundle_name,
                "include_images": include_images,
            }))
        
        # Create job dir first so we have job_id for progress page
        packager = HelmPackager(credential_manager=credential_manager)
        job_id, job_dir = packager._create_job_dir()
        # Write initial progress so progress page has something to show
        packager._write_progress(job_dir, "starting", 0, "Starting packaging job...", {})
        
        def run_package():
            try:
                if source_type == "repo":
                    packager.package_from_repo(
                        repo_url=repo_url,
                        chart_name=chart_name,
                        chart_version=chart_version,
                        values_yaml=values_yaml,
                        bundle_name=bundle_name,
                        include_images=include_images,
                        _job_id=job_id,
                        _job_dir=job_dir
                    )
                else:
                    packager.package_from_oci(
                        oci_chart=oci_chart,
                        chart_version=oci_version,
                        values_yaml=values_yaml,
                        bundle_name=bundle_name,
                        include_images=include_images,
                        _job_id=job_id,
                        _job_dir=job_dir
                    )
            except Exception:
                pass  # Error stored in job metadata
        
        # Run packaging in background
        thread = threading.Thread(target=run_package)
        thread.daemon = True
        thread.start()
        
        return redirect(url_for("job_progress", job_id=job_id))
        
    except PackagingError as e:
        flash(f"Packaging error: {str(e)}", "error")
        return render_template("index.html", **_index_context(extra={
            "source_type": request.form.get("source_type", ""),
            "repo_url": request.form.get("repo_url", ""),
            "chart_name": request.form.get("chart_name", ""),
            "chart_version": request.form.get("chart_version", ""),
            "oci_chart": request.form.get("oci_chart", ""),
            "oci_version": request.form.get("oci_version", ""),
            "values_yaml": request.form.get("values_yaml", ""),
            "bundle_name": request.form.get("bundle_name", ""),
            "include_images": request.form.get("include_images", "yes"),
        }))
    except Exception as e:
        flash(f"Unexpected error: {str(e)}", "error")
        return render_template("index.html", **_index_context())


@app.route("/result/<job_id>")
def result(job_id):
    """Display packaging result."""
    job_dir = settings.JOBS_DIR / job_id
    
    if not job_dir.exists():
        flash(f"Job {job_id} not found", "error")
        return redirect(url_for("index"))
    
    # Load job metadata
    job_json_path = job_dir / "job.json"
    if job_json_path.exists():
        with open(job_json_path, "r") as f:
            job_data = json.load(f)
    else:
        job_data = {}
    
    # Load log
    log_path = job_dir / "log.txt"
    log_content = ""
    if log_path.exists():
        with open(log_path, "r") as f:
            log_content = f.read()
    
    # Check if bundle exists
    bundle_path = settings.BUNDLES_DIR / job_data.get("bundle_filename", "")
    bundle_exists = bundle_path.exists() if job_data.get("bundle_filename") else False
    
    return render_template("result.html",
                         job_id=job_id,
                         job_data=job_data,
                         log_content=log_content,
                         bundle_exists=bundle_exists,
                         bundle_filename=job_data.get("bundle_filename", ""))


@app.route("/download/<job_id>")
def download(job_id):
    """Download the bundle file."""
    job_dir = settings.JOBS_DIR / job_id
    
    if not job_dir.exists():
        flash(f"Job {job_id} not found", "error")
        return redirect(url_for("index"))
    
    # Load job metadata to get bundle filename
    job_json_path = job_dir / "job.json"
    if not job_json_path.exists():
        flash("Job metadata not found", "error")
        return redirect(url_for("index"))
    
    with open(job_json_path, "r") as f:
        job_data = json.load(f)
    
    bundle_filename = job_data.get("bundle_filename")
    if not bundle_filename:
        flash("Bundle filename not found", "error")
        return redirect(url_for("result", job_id=job_id))
    
    bundle_path = settings.BUNDLES_DIR / bundle_filename
    if not bundle_path.exists():
        flash("Bundle file not found", "error")
        return redirect(url_for("result", job_id=job_id))
    
    return send_file(
        bundle_path,
        as_attachment=True,
        download_name=bundle_filename
    )


@app.route("/job/<job_id>/progress")
def job_progress(job_id):
    """Show packaging progress page (polls for status)."""
    job_dir = settings.JOBS_DIR / job_id
    if not job_dir.exists():
        flash(f"Job {job_id} not found", "error")
        return redirect(url_for("index"))
    return render_template(
        "progress.html",
        job_id=job_id,
        helm_timeout=settings.HELM_TIMEOUT,
        image_pull_timeout=settings.IMAGE_PULL_TIMEOUT
    )


@app.route("/job/<job_id>/status")
def job_status(job_id):
    """Return current job status and progress (JSON) for polling."""
    job_dir = settings.JOBS_DIR / job_id
    if not job_dir.exists():
        return jsonify({"error": "Job not found"}), 404
    
    # Load progress
    progress = None
    progress_path = job_dir / "progress.json"
    if progress_path.exists():
        try:
            with open(progress_path, "r") as f:
                progress = json.load(f)
        except Exception:
            progress = {"step": "unknown", "percent": 0, "message": "Loading...", "details": {}}
    
    # Load job metadata
    job_data = {}
    job_json_path = job_dir / "job.json"
    if job_json_path.exists():
        try:
            with open(job_json_path, "r") as f:
                job_data = json.load(f)
        except Exception:
            pass
    
    status = job_data.get("status", "in_progress")
    return jsonify({
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "job": job_data
    })


@app.route("/job/<job_id>/log")
def job_log(job_id):
    """Return log content since offset (for real-time log streaming). Query param: offset (byte offset)."""
    job_dir = settings.JOBS_DIR / job_id
    if not job_dir.exists():
        return jsonify({"error": "Job not found"}), 404
    log_path = job_dir / "log.txt"
    offset = request.args.get("offset", "0")
    try:
        offset = int(offset)
    except ValueError:
        offset = 0
    if not log_path.exists():
        return jsonify({"content": "", "offset": 0})
    try:
        with open(log_path, "r") as f:
            f.seek(offset)
            content = f.read()
        new_offset = offset + len(content)
        return jsonify({"content": content, "offset": new_offset})
    except Exception:
        return jsonify({"content": "", "offset": offset})


# --- Helm Repositories ---

@app.route("/repos")
def repos_list():
    """List Helm repositories and allow add/update/delete."""
    repos = repo_manager.list_repos()
    updated_at = repo_manager.get_last_updated()
    return render_template("repos/list.html", repos=repos, updated_at=updated_at)


@app.route("/repos", methods=["POST"])
def repos_add():
    """Add a Helm repository (form: name, url)."""
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    if not name or not url:
        flash("Name and URL are required", "error")
        return redirect(url_for("repos_list"))
    try:
        repo_manager.add_repo(name, url)
        repo_manager.update_repos()
        flash(f"Repository '{name}' added and updated", "success")
    except ValueError as e:
        flash(str(e), "error")
    except subprocess.CalledProcessError as e:
        flash(f"Failed to add repository: {e.stderr or e}", "error")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    return redirect(url_for("repos_list"))


@app.route("/repos/update", methods=["POST"])
def repos_update():
    """Update all Helm repositories (refresh chart list)."""
    try:
        repo_manager.update_repos()
        flash("All repositories updated successfully", "success")
    except Exception as e:
        flash(f"Update failed: {str(e)}", "error")
    return redirect(request.referrer or url_for("repos_list"))


@app.route("/api/repos/update", methods=["POST"])
def api_repos_update():
    """Update all Helm repositories; return JSON with new updated_at."""
    try:
        repo_manager.update_repos()
        updated = repo_manager.get_last_updated()
        return jsonify({"ok": True, "updated_at": updated})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/repos/delete", methods=["POST"])
def repos_delete():
    """Remove a Helm repository (form: name)."""
    name = request.form.get("name", "").strip()
    if not name:
        flash("Repository name is required", "error")
        return redirect(url_for("repos_list"))
    try:
        repo_manager.remove_repo(name)
        flash(f"Repository '{name}' removed", "success")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    return redirect(url_for("repos_list"))


@app.route("/api/repos/charts")
def api_repos_charts():
    """Search charts across added repos. Query param: q (optional)."""
    query = request.args.get("q", "").strip()
    charts = repo_manager.search_charts(query)
    return jsonify({"charts": charts})


@app.route("/api/repos/charts/<repo_name>/<chart_name>/versions")
def api_repos_chart_versions(repo_name, chart_name):
    """Return available versions for repo/chart."""
    versions = repo_manager.get_chart_versions(repo_name, chart_name)
    return jsonify({"versions": versions})


@app.route("/api/repos/updated")
def api_repos_updated():
    """Return last updated timestamp for repo list."""
    updated = repo_manager.get_last_updated()
    return jsonify({"updated_at": updated})


@app.route("/job/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id):
    """Cancel a running job."""
    job_dir = settings.JOBS_DIR / job_id
    if not job_dir.exists():
        flash(f"Job {job_id} not found", "error")
        return redirect(url_for("index"))
    
    # Load job status
    job_json_path = job_dir / "job.json"
    job_data = {}
    if job_json_path.exists():
        try:
            with open(job_json_path, "r") as f:
                job_data = json.load(f)
        except Exception:
            pass
    
    status = job_data.get("status", "in_progress")
    if status not in ["in_progress"]:
        flash(f"Cannot cancel job with status: {status}", "error")
        return redirect(url_for("result", job_id=job_id))
    
    # Create cancel flag
    packager = HelmPackager(credential_manager=credential_manager)
    packager._mark_cancelled(job_dir)
    flash("Job cancellation requested. The job will stop at the next checkpoint.", "info")
    return redirect(url_for("result", job_id=job_id))


@app.route("/job/<job_id>/retry", methods=["POST"])
def job_retry(job_id):
    """Retry a failed or cancelled job using stored parameters."""
    job_dir = settings.JOBS_DIR / job_id
    if not job_dir.exists():
        flash(f"Job {job_id} not found", "error")
        return redirect(url_for("index"))
    
    # Load job metadata
    job_json_path = job_dir / "job.json"
    if not job_json_path.exists():
        flash("Cannot retry: job metadata not found", "error")
        return redirect(url_for("index"))
    
    try:
        with open(job_json_path, "r") as f:
            job_data = json.load(f)
    except Exception as e:
        flash(f"Error reading job metadata: {str(e)}", "error")
        return redirect(url_for("index"))
    
    status = job_data.get("status")
    if status not in ["error", "cancelled"]:
        flash(f"Cannot retry job with status: {status}", "error")
        return redirect(url_for("result", job_id=job_id))
    
    # Extract parameters
    source_type = job_data.get("source_type")
    if not source_type:
        flash("Cannot retry: missing source type in job metadata", "error")
        return redirect(url_for("result", job_id=job_id))
    
    # Create new job with same parameters
    packager = HelmPackager(credential_manager=credential_manager)
    new_job_id, new_job_dir = packager._create_job_dir()
    packager._write_progress(new_job_dir, "starting", 0, "Retrying packaging job...", {})
    
    def run_retry():
        try:
            if source_type == "repo":
                packager.package_from_repo(
                    repo_url=job_data.get("repo_url", ""),
                    chart_name=job_data.get("chart_name", ""),
                    chart_version=job_data.get("chart_version"),
                    values_yaml=job_data.get("values_yaml"),
                    bundle_name=job_data.get("bundle_name"),
                    include_images=job_data.get("include_images", True),
                    _job_id=new_job_id,
                    _job_dir=new_job_dir
                )
            else:  # oci
                packager.package_from_oci(
                    oci_chart=job_data.get("oci_chart", ""),
                    chart_version=job_data.get("chart_version"),
                    values_yaml=job_data.get("values_yaml"),
                    bundle_name=job_data.get("bundle_name"),
                    include_images=job_data.get("include_images", True),
                    _job_id=new_job_id,
                    _job_dir=new_job_dir
                )
        except Exception:
            pass  # Error stored in job metadata
    
    thread = threading.Thread(target=run_retry)
    thread.daemon = True
    thread.start()
    
    flash("Job retry started", "success")
    return redirect(url_for("job_progress", job_id=new_job_id))


@app.route("/job/<job_id>/delete", methods=["POST"])
def job_delete(job_id):
    """Delete a job and optionally its bundle."""
    job_dir = settings.JOBS_DIR / job_id
    if not job_dir.exists():
        flash(f"Job {job_id} not found", "error")
        return redirect(url_for("index"))
    
    # Check if job is running
    job_json_path = job_dir / "job.json"
    job_data = {}
    if job_json_path.exists():
        try:
            with open(job_json_path, "r") as f:
                job_data = json.load(f)
        except Exception:
            pass
    
    status = job_data.get("status", "in_progress")
    if status == "in_progress":
        flash("Cannot delete a running job. Cancel it first.", "error")
        return redirect(url_for("result", job_id=job_id))
    
    # Delete bundle if requested and exists
    delete_bundle = request.form.get("delete_bundle", "false") == "true"
    bundle_filename = job_data.get("bundle_filename")
    if delete_bundle and bundle_filename:
        bundle_path = settings.BUNDLES_DIR / bundle_filename
        if bundle_path.exists():
            try:
                bundle_path.unlink()
                flash(f"Bundle {bundle_filename} deleted", "info")
            except Exception as e:
                flash(f"Warning: Could not delete bundle: {str(e)}", "warning")
    
    # Delete job directory
    try:
        import shutil
        shutil.rmtree(job_dir)
        flash("Job deleted successfully", "success")
    except Exception as e:
        flash(f"Error deleting job: {str(e)}", "error")
    
    return redirect(url_for("index"))


@app.route("/credentials")
def credentials_list():
    """List all registry credentials."""
    creds = credential_manager.get_all_credentials()
    return render_template("credentials/list.html", credentials=creds)


@app.route("/credentials/add", methods=["GET", "POST"])
def credentials_add():
    """Add a new credential."""
    if request.method == "POST":
        server = request.form.get("server", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        errors = []
        if not server:
            errors.append("Server is required")
        if not username:
            errors.append("Username is required")
        if not password:
            errors.append("Password is required")
        
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("credentials/form.html", 
                                 server=server, username=username)
        
        try:
            credential_id = credential_manager.add_credential(server, username, password)
            flash(f"Credential for {server} added successfully", "success")
            return redirect(url_for("credentials_list"))
        except ValueError as e:
            flash(str(e), "error")
            return render_template("credentials/form.html",
                                 server=server, username=username)
        except Exception as e:
            flash(f"Error adding credential: {str(e)}", "error")
            return render_template("credentials/form.html",
                                 server=server, username=username)
    
    return render_template("credentials/form.html")


@app.route("/credentials/<credential_id>/edit", methods=["GET", "POST"])
def credentials_edit(credential_id):
    """Edit an existing credential."""
    creds = credential_manager.get_all_credentials(include_passwords=True)
    cred = next((c for c in creds if c["id"] == credential_id), None)
    
    if not cred:
        flash("Credential not found", "error")
        return redirect(url_for("credentials_list"))
    
    if request.method == "POST":
        server = request.form.get("server", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        errors = []
        if not server:
            errors.append("Server is required")
        if not username:
            errors.append("Username is required")
        
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("credentials/form.html",
                                 credential_id=credential_id,
                                 server=server, username=username,
                                 is_edit=True)
        
        try:
            # Only update password if provided
            password_to_use = password if password else None
            credential_manager.update_credential(
                credential_id, server=server, username=username, password=password_to_use
            )
            flash(f"Credential for {server} updated successfully", "success")
            return redirect(url_for("credentials_list"))
        except ValueError as e:
            flash(str(e), "error")
            return render_template("credentials/form.html",
                                 credential_id=credential_id,
                                 server=server, username=username,
                                 is_edit=True)
        except Exception as e:
            flash(f"Error updating credential: {str(e)}", "error")
            return render_template("credentials/form.html",
                                 credential_id=credential_id,
                                 server=server, username=username,
                                 is_edit=True)
    
    return render_template("credentials/form.html",
                         credential_id=credential_id,
                         server=cred["server"],
                         username=cred["username"],
                         is_edit=True)


@app.route("/credentials/<credential_id>/delete", methods=["POST"])
def credentials_delete(credential_id):
    """Delete a credential."""
    try:
        credential_manager.delete_credential(credential_id)
        flash("Credential deleted successfully", "success")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Error deleting credential: {str(e)}", "error")
    
    return redirect(url_for("credentials_list"))


@app.route("/credentials/<credential_id>/test", methods=["POST"])
def credentials_test(credential_id):
    """Test credential login."""
    from flask import jsonify
    result = credential_manager.test_login(credential_id)
    return jsonify(result)


@app.route("/credentials/env-status")
def credentials_env_status():
    """Get status of environment variable credentials."""
    from flask import jsonify
    has_env_creds = bool(
        settings.REGISTRY_USERNAME and 
        settings.REGISTRY_PASSWORD and 
        settings.REGISTRY_SERVER
    )
    return jsonify({
        "configured": has_env_creds,
        "server": settings.REGISTRY_SERVER if has_env_creds else None,
        "username": settings.REGISTRY_USERNAME if has_env_creds else None
    })


if __name__ == "__main__":
    app.run(host=settings.FLASK_HOST, port=settings.FLASK_PORT, debug=settings.FLASK_DEBUG)
