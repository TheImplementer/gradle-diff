#!/usr/bin/env python3
import json
import subprocess
import sys
import os
import hashlib
import re
import argparse
from datetime import datetime

# Configuration
GRAPH_FILE = "project-graph.json"
GRADLE_COMMAND = "./gradlew" 
GRADLE_TASK = "exportProjectGraph"

# Files that should NEVER trigger a build/test
IGNORED_PATTERNS = [
    r".*\.md$",
    r"docs/.*",
    r"\.gitignore",
    r"jenkins/.*",
    r"scripts/.*",
    r"\.github/.*"
]

# Remote Cache Config
BUCKET = os.environ.get("GRADLE_DIFF_S3_BUCKET")
PREFIX = os.environ.get("GRADLE_DIFF_S3_PREFIX", "gradle-diff-cache")

def get_hash(file_list):
    """Generates a combined hash for all build configuration files."""
    hasher = hashlib.md5()
    for f in sorted(file_list):
        if os.path.exists(f):
            with open(f, 'rb') as fd:
                hasher.update(fd.read())
    return hasher.hexdigest()

def s3_download(remote_path, local_path):
    if not BUCKET: return False
    full_s3_path = f"s3://{BUCKET}/{PREFIX}/{remote_path}"
    print(f"[CACHE] Checking S3 for cached graph: {full_s3_path}", file=sys.stderr)
    try:
        subprocess.check_call(["aws", "s3", "ls", full_s3_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[CACHE] Remote cache HIT. Downloading...", file=sys.stderr)
        subprocess.check_call(["aws", "s3", "cp", full_s3_path, local_path, "--quiet"])
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"[CACHE] Remote cache MISS (or AWS CLI not configured).", file=sys.stderr)
        return False

def s3_upload(local_path, remote_path):
    if not BUCKET: return
    full_s3_path = f"s3://{BUCKET}/{PREFIX}/{remote_path}"
    print(f"[CACHE] Uploading new graph to S3: {full_s3_path}", file=sys.stderr)
    try:
        subprocess.check_call(["aws", "s3", "cp", local_path, full_s3_path, "--quiet"])
        print(f"[CACHE] Successfully uploaded graph to S3.", file=sys.stderr)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"[CACHE] Warning: Failed to upload cache to S3: {e}", file=sys.stderr)

def refresh_graph(extra_args=None):
    """Runs the Gradle task to refresh the project dependency graph."""
    print("Cache miss or config changed. Refreshing project graph via Gradle...", file=sys.stderr)
    try:
        cmd = [GRADLE_COMMAND if os.path.exists(GRADLE_COMMAND) else "gradle", GRADLE_TASK, "--quiet"]
        if extra_args:
            cmd.extend(extra_args)
        subprocess.check_call(cmd)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Error refreshing graph: {e}. Ensure gradle is installed or gradlew is executable.", file=sys.stderr)
        sys.exit(1)

def get_git_info(since_commit):
    """Returns commits and file statuses since since_commit."""
    try:
        # Get commit log
        log_output = subprocess.check_output([
            'git', 'log', f'{since_commit}..HEAD', 
            '--pretty=format:%h|%an|%ad|%s', '--date=short'
        ]).decode('utf-8')
        commits = []
        if log_output.strip():
            for line in log_output.strip().split('\n'):
                parts = line.split('|')
                if len(parts) == 4:
                    commits.append({"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})

        # Get file statuses (Added, Modified, Deleted)
        status_output = subprocess.check_output(['git', 'diff', '--name-status', since_commit]).decode('utf-8')
        all_files = []
        filtered_files = []
        if status_output.strip():
            for line in status_output.strip().split('\n'):
                parts = line.split('\t')
                if len(parts) >= 2:
                    status, file_path = parts[0], parts[1]
                    file_info = {"status": status, "path": file_path}
                    all_files.append(file_info)
                    if not any(re.match(pattern, file_path) for pattern in IGNORED_PATTERNS):
                        filtered_files.append(file_info)
        
        return commits, all_files, filtered_files
    except subprocess.CalledProcessError:
        print(f"Error: Git operations failed for commit '{since_commit}'", file=sys.stderr)
        return [], [], []

def find_affected_projects(graph_file, changed_files):
    """Finds all projects affected by changed_files and builds a detailed reason map."""
    if not os.path.exists(graph_file):
        return [], {}
        
    with open(graph_file, 'r') as f:
        projects = json.load(f)

    report_data = {
        "global_trigger": None,
        "direct_impact": {},
        "transitive_impact": {}
    }

    # 1. Global triggers
    global_triggers = [
        "gradle/libs.versions.toml",
        "buildSrc/",
        "gradle.properties",
        "settings.gradle",
        "build.gradle",
        "gradle-diff.gradle"
    ]
    
    changed_paths = [f['path'] for f in changed_files]
    
    for file_path in changed_paths:
        for trigger in global_triggers:
            if file_path.startswith(trigger):
                report_data["global_trigger"] = file_path
                all_paths = sorted([p['path'] for p in projects if p['path'] != ":"])
                return all_paths, report_data

    # 2. Directory mapping (Direct Impact)
    direct_affected = {} 
    for file_info in changed_files:
        file_path = file_info['path']
        best_match = None
        for p in sorted(projects, key=lambda x: len(x['dir']), reverse=True):
            p_dir = p['dir'].rstrip('/')
            if not p_dir or p_dir == '.': continue
            if file_path.startswith(p_dir + '/'):
                best_match = p['path']
                break
        if best_match:
            if best_match not in direct_affected: direct_affected[best_match] = []
            direct_affected[best_match].append(file_info)

    report_data["direct_impact"] = direct_affected

    # 3. Inverted graph for Transitive Impact
    dependants = {p['path']: set() for p in projects}
    for p in projects:
        for dep in p.get('dependencies', []):
            if dep in dependants:
                dependants[dep].add(p['path'])

    # 4. Transitive closure
    total_affected = set(direct_affected.keys())
    queue = list(direct_affected.keys())
    
    transitive_reasons = {} 
    
    while queue:
        current = queue.pop(0)
        if current in dependants:
            for dep_of_current in dependants[current]:
                if dep_of_current not in total_affected:
                    total_affected.add(dep_of_current)
                    queue.append(dep_of_current)
                    if dep_of_current not in transitive_reasons: transitive_reasons[dep_of_current] = set()
                    transitive_reasons[dep_of_current].add(current)
                elif dep_of_current in transitive_reasons:
                    transitive_reasons[dep_of_current].add(current)

    report_data["transitive_impact"] = {k: list(v) for k, v in transitive_reasons.items()}

    return sorted(list(total_affected)), report_data

def generate_html_report(data, output_path):
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Gradle Diff Report</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 1000px; margin: 0 auto; padding: 20px; background: #f4f7f9; }}
            .card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }}
            h1, h2 {{ color: #2c3e50; }}
            .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; text-transform: uppercase; }}
            .badge-hit {{ background: #d4edda; color: #155724; }}
            .badge-miss {{ background: #f8d7da; color: #721c24; }}
            .badge-global {{ background: #fff3cd; color: #856404; }}
            .status-A {{ background: #28a745; color: white; }}
            .status-M {{ background: #ffc107; color: black; }}
            .status-D {{ background: #dc3545; color: white; }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; }}
            .stat-box {{ text-align: center; padding: 15px; border-radius: 6px; background: #f8f9fa; border: 1px solid #dee2e6; }}
            .stat-val {{ display: block; font-size: 24px; font-weight: bold; color: #007bff; }}
            .stat-label {{ font-size: 12px; color: #6c757d; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #eee; font-size: 14px; }}
            th {{ background: #f8f9fa; color: #666; font-weight: 600; }}
            .project-path {{ font-family: monospace; font-weight: bold; color: #e83e8c; }}
            .reason {{ font-size: 13px; color: #666; margin-left: 10px; }}
            .task-list {{ background: #2d3436; color: #dfe6e9; padding: 15px; border-radius: 6px; font-family: monospace; white-space: pre-wrap; word-break: break-all; }}
            .commit-hash {{ font-family: monospace; color: #007bff; }}
        </style>
    </head>
    <body>
        <h1>Gradle Diff Analysis</h1>
        <p>Generated at: {timestamp}</p>

        <div class="grid">
            <div class="card stat-box">
                <span class="stat-label">Since Commit</span>
                <span class="stat-val" style="font-size: 14px;">{since_commit}</span>
            </div>
            <div class="card stat-box">
                <span class="stat-label">Cache Status</span>
                <span class="badge {cache_class}">{cache_status} ({cache_source})</span>
            </div>
            <div class="card stat-box">
                <span class="stat-label">Affected Projects</span>
                <span class="stat-val">{affected_count}</span>
            </div>
            <div class="card stat-box">
                <span class="stat-label">Files Changed</span>
                <span class="stat-val">{files_changed}</span>
            </div>
        </div>

        {global_section}

        <div class="card">
            <h2>Recent Commits</h2>
            <table>
                <thead><tr><th>Hash</th><th>Author</th><th>Date</th><th>Subject</th></tr></thead>
                <tbody>{commit_rows}</tbody>
            </table>
        </div>

        <div class="card">
            <h2>Affected Projects & Impact Path</h2>
            <table>
                <thead><tr><th>Project</th><th>Reason</th></tr></thead>
                <tbody>{project_rows}</tbody>
            </table>
        </div>

        <div class="card">
            <h2>Detailed File Changes</h2>
            <table>
                <thead><tr><th>Status</th><th>Path</th></tr></thead>
                <tbody>{file_rows}</tbody>
            </table>
        </div>

        <div class="card">
            <h2>Execution Command</h2>
            <div class="task-list">./gradlew {tasks}</div>
        </div>
    </body>
    </html>
    """
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cache_class = "badge-hit" if data["cache"]["status"] == "hit" else "badge-miss"
    
    global_section = ""
    if data.get("global_trigger"):
        global_section = f"""
        <div class="card" style="border-left: 5px solid #ffc107;">
            <span class="badge badge-global">Global Trigger Detected</span>
            <p><strong>{data['global_trigger']}</strong> was modified. All projects are considered affected.</p>
        </div>
        """

    commit_rows = ""
    for c in data.get("commits", []):
        commit_rows += f"<tr><td class='commit-hash'>{c['hash']}</td><td>{c['author']}</td><td>{c['date']}</td><td>{c['subject']}</td></tr>"
    if not commit_rows: commit_rows = "<tr><td colspan='4'>No new commits found.</td></tr>"

    project_rows = ""
    for p in data.get("affected_projects", []):
        if p == ":": continue
        reason = ""
        if p in data.get("direct_impact", {}):
            file_count = len(data['direct_impact'][p])
            reason = f"Directly modified ({file_count} files)"
        elif p in data.get("transitive_impact", {}):
            triggers = ", ".join(data['transitive_impact'][p])
            reason = f"Depends on: {triggers}"
        
        project_rows += f"<tr><td><span class='project-path'>{p}</span></td><td class='reason'>{reason}</td></tr>"
    if not project_rows: project_rows = "<tr><td colspan='2'>No projects affected.</td></tr>"

    file_rows = ""
    for f in data.get("file_details", []):
        status_badge = f"<span class='badge status-{f['status'][0]}'>{f['status']}</span>"
        file_rows += f"<tr><td>{status_badge}</td><td style='font-family: monospace;'>{f['path']}</td></tr>"
    if not file_rows: file_rows = "<tr><td colspan='2'>No files changed.</td></tr>"

    content = html_template.format(
        timestamp=timestamp,
        since_commit=data["since_commit"],
        cache_status=data["cache"]["status"].upper(),
        cache_source=data["cache"]["source"].upper(),
        cache_class=cache_class,
        affected_count=len(data["affected_projects"]),
        files_changed=data["changes"]["filtered"],
        global_section=global_section,
        commit_rows=commit_rows,
        project_rows=project_rows,
        file_rows=file_rows,
        tasks=" ".join(data.get("tasks", []))
    )
    
    with open(output_path, 'w') as f:
        f.write(content)

def main():
    parser = argparse.ArgumentParser(description="Calculate affected Gradle tasks based on Git diff.")
    parser.add_argument("since", help="The git commit/branch to diff against.")
    # We use nargs='*' so we can parse flags manually from the remaining args
    parser.add_argument("args", nargs="*", help="Gradle tasks (e.g. test) and flags (e.g. -Puser=foo).")
    parser.add_argument("--report", help="Path to write a JSON report of the analysis.")
    parser.add_argument("--html-report", help="Path to write an HTML report of the analysis.")
    
    parsed, unknown = parser.parse_known_args()
    
    # Separate tasks from flags
    task_names = []
    gradle_flags = []
    
    # Combine positional args and unknown args to find all flags
    all_args = parsed.args + unknown
    
    for arg in all_args:
        if arg.startswith("-"):
            gradle_flags.append(arg)
        else:
            task_names.append(arg)
            
    # Default to 'test' if no tasks specified
    if not task_names:
        task_names = ["test"]

    report = {
        "since_commit": parsed.since,
        "cache": {"status": "hit", "source": "local"},
        "config_hash": None,
        "changes": {"total": 0, "filtered": 0},
        "commits": [],
        "file_details": [],
        "affected_projects": [],
        "tasks": []
    }

    # 1. Calculate Configuration Hash
    build_scripts = []
    for root, dirs, files in os.walk('.'):
        if 'build' in dirs: dirs.remove('build')
        if '.git' in dirs: dirs.remove('.git')
        for f in files:
            if f.endswith(('.gradle', '.gradle.kts', '.toml', '.properties')):
                build_scripts.append(os.path.join(root, f))
    
    current_hash = get_hash(build_scripts)
    report["config_hash"] = current_hash
    remote_key = f"graph-{current_hash}.json"
    
    print(f"[CACHE] Calculated configuration hash: {current_hash}", file=sys.stderr)

    # 2. Cache Logic
    stale = True
    if os.path.exists(GRAPH_FILE):
        hash_file = ".gradle-diff-hash"
        if os.path.exists(hash_file):
            with open(hash_file, 'r') as f:
                saved_hash = f.read().strip()
                if saved_hash == current_hash:
                    print("[CACHE] Local cache HIT.", file=sys.stderr)
                    stale = False
                else:
                    print(f"[CACHE] Local cache STALE. (Saved: {saved_hash} != Current: {current_hash})", file=sys.stderr)
        else:
            print("[CACHE] Local hash file missing.", file=sys.stderr)
    else:
        print("[CACHE] Local graph file missing.", file=sys.stderr)

    if stale:
        if BUCKET and s3_download(remote_key, GRAPH_FILE):
            report["cache"] = {"status": "hit", "source": "s3"}
            stale = False
        else:
            report["cache"] = {"status": "miss", "source": "none"}
            refresh_graph(gradle_flags)
            if BUCKET:
                s3_upload(GRAPH_FILE, remote_key)
        
        with open(".gradle-diff-hash", "w") as f:
            f.write(current_hash)

    # 3. Analyze Git Changes
    commits, all_files, filtered_files = get_git_info(parsed.since)
    report["commits"] = commits
    report["file_details"] = all_files
    report["changes"] = {"total": len(all_files), "filtered": len(filtered_files)}
    
    if not filtered_files:
        if parsed.report:
            with open(parsed.report, 'w') as f: json.dump(report, f, indent=2)
        if parsed.html_report:
            generate_html_report(report, parsed.html_report)
        return

    affected_paths, impact_report = find_affected_projects(GRAPH_FILE, filtered_files)
    report.update(impact_report)
    report["affected_projects"] = affected_paths
    
    if affected_paths:
        task_list = []
        for p in affected_paths:
            if p == ":": continue
            for t in task_names:
                task_list.append(f"{p}:{t}")
        
        report["tasks"] = task_list
        print(" ".join(task_list))

    if parsed.report:
        with open(parsed.report, 'w') as f: json.dump(report, f, indent=2)
    if parsed.html_report:
        generate_html_report(report, parsed.html_report)

if __name__ == "__main__":
    main()
