#!/usr/bin/env python3
import json
import subprocess
import sys
import os
import hashlib
import re
import argparse

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
    try:
        full_s3_path = f"s3://{BUCKET}/{PREFIX}/{remote_path}"
        subprocess.check_call(["aws", "s3", "ls", full_s3_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.check_call(["aws", "s3", "cp", full_s3_path, local_path, "--quiet"])
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def s3_upload(local_path, remote_path):
    if not BUCKET: return
    try:
        full_s3_path = f"s3://{BUCKET}/{PREFIX}/{remote_path}"
        subprocess.check_call(["aws", "s3", "cp", local_path, full_s3_path, "--quiet"])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Warning: Failed to upload cache to S3: {e}", file=sys.stderr)

def refresh_graph():
    """Runs the Gradle task to refresh the project dependency graph."""
    print("Cache miss or config changed. Refreshing project graph via Gradle...", file=sys.stderr)
    try:
        cmd = GRADLE_COMMAND if os.path.exists(GRADLE_COMMAND) else "gradle"
        subprocess.check_call([cmd, GRADLE_TASK, "--quiet"])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Error refreshing graph: {e}. Ensure gradle is installed or gradlew is executable.", file=sys.stderr)
        sys.exit(1)

def get_git_changes(since_commit):
    """Returns a list of changed files since since_commit, filtered by ignore list."""
    try:
        output = subprocess.check_output(['git', 'diff', '--name-only', since_commit]).decode('utf-8')
        raw_files = [f for f in output.strip().split('\n') if f]
        
        filtered_files = []
        for f in raw_files:
            if not any(re.match(pattern, f) for pattern in IGNORED_PATTERNS):
                filtered_files.append(f)
        
        return raw_files, filtered_files
    except subprocess.CalledProcessError:
        print(f"Error: Git diff failed for commit '{since_commit}'", file=sys.stderr)
        return [], []

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
    
    for file_path in changed_files:
        for trigger in global_triggers:
            if file_path.startswith(trigger):
                report_data["global_trigger"] = file_path
                all_paths = sorted([p['path'] for p in projects if p['path'] != ":"])
                return all_paths, report_data

    # 2. Directory mapping (Direct Impact)
    direct_affected = {} # path -> [files]
    for file_path in changed_files:
        best_match = None
        for p in sorted(projects, key=lambda x: len(x['dir']), reverse=True):
            p_dir = p['dir'].rstrip('/')
            if not p_dir or p_dir == '.': continue
            if file_path.startswith(p_dir + '/'):
                best_match = p['path']
                break
        if best_match:
            if best_match not in direct_affected: direct_affected[best_match] = []
            direct_affected[best_match].append(file_path)

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
    
    transitive_reasons = {} # path -> set of paths that triggered it
    
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

    # Convert sets to lists for JSON serialization
    report_data["transitive_impact"] = {k: list(v) for k, v in transitive_reasons.items()}

    return sorted(list(total_affected)), report_data

def main():
    parser = argparse.ArgumentParser(description="Calculate affected Gradle tasks based on Git diff.")
    parser.add_argument("since", help="The git commit/branch to diff against.")
    parser.add_argument("tasks", nargs="+", help="The Gradle tasks to run (e.g., test assemble).")
    parser.add_argument("--report", help="Path to write a JSON report of the analysis.")
    args = parser.parse_args()

    report = {
        "since_commit": args.since,
        "cache": {"status": "hit", "source": "local"},
        "config_hash": None,
        "changes": {"total": 0, "filtered": 0},
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

    # 2. Cache Logic
    stale = True
    if os.path.exists(GRAPH_FILE):
        hash_file = ".gradle-diff-hash"
        if os.path.exists(hash_file):
            with open(hash_file, 'r') as f:
                if f.read().strip() == current_hash:
                    stale = False

    if stale:
        if BUCKET and s3_download(remote_key, GRAPH_FILE):
            report["cache"] = {"status": "hit", "source": "s3"}
            stale = False
        else:
            report["cache"] = {"status": "miss", "source": "none"}
            refresh_graph()
            if BUCKET:
                s3_upload(GRAPH_FILE, remote_key)
        
        with open(".gradle-diff-hash", "w") as f:
            f.write(current_hash)

    # 3. Analyze Git Changes
    raw_changes, filtered_changes = get_git_changes(args.since)
    report["changes"] = {"total": len(raw_changes), "filtered": len(filtered_changes)}
    
    if not filtered_changes:
        if args.report:
            with open(args.report, 'w') as f: json.dump(report, f, indent=2)
        return

    affected_paths, impact_report = find_affected_projects(GRAPH_FILE, filtered_changes)
    report.update(impact_report)
    report["affected_projects"] = affected_paths
    
    if affected_paths:
        task_list = []
        for p in affected_paths:
            if p == ":": continue
            for t in args.tasks:
                task_list.append(f"{p}:{t}")
        
        report["tasks"] = task_list
        print(" ".join(task_list))

    if args.report:
        with open(args.report, 'w') as f:
            json.dump(report, f, indent=2)

if __name__ == "__main__":
    main()
