"""Flask application entrypoint."""
import os
import re
from flask import Flask, render_template, request, redirect, url_for, send_file, flash
from pathlib import Path
import json
import yaml

from . import settings
from .packager import HelmPackager, PackagingError

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max file upload


@app.route("/")
def index():
    """Main form page."""
    return render_template("index.html")


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
            return render_template("index.html", 
                                 source_type=source_type,
                                 repo_url=repo_url,
                                 chart_name=chart_name,
                                 chart_version=chart_version,
                                 oci_chart=oci_chart,
                                 values_yaml=values_yaml,
                                 bundle_name=bundle_name,
                                 include_images=include_images)
        
        # Create packager and start job
        packager = HelmPackager()
        
        if source_type == "repo":
            job_id = packager.package_from_repo(
                repo_url=repo_url,
                chart_name=chart_name,
                chart_version=chart_version,
                values_yaml=values_yaml,
                bundle_name=bundle_name,
                include_images=include_images
            )
        else:  # oci
            job_id = packager.package_from_oci(
                oci_chart=oci_chart,
                values_yaml=values_yaml,
                bundle_name=bundle_name,
                include_images=include_images
            )
        
        return redirect(url_for("result", job_id=job_id))
        
    except PackagingError as e:
        flash(f"Packaging error: {str(e)}", "error")
        return render_template("index.html",
                             source_type=request.form.get("source_type", ""),
                             repo_url=request.form.get("repo_url", ""),
                             chart_name=request.form.get("chart_name", ""),
                             chart_version=request.form.get("chart_version", ""),
                             oci_chart=request.form.get("oci_chart", ""),
                             values_yaml=request.form.get("values_yaml", ""),
                             bundle_name=request.form.get("bundle_name", ""),
                             include_images=request.form.get("include_images", "yes"))
    except Exception as e:
        flash(f"Unexpected error: {str(e)}", "error")
        return render_template("index.html")


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


if __name__ == "__main__":
    app.run(host=settings.FLASK_HOST, port=settings.FLASK_PORT, debug=settings.FLASK_DEBUG)
