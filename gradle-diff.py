#!/usr/bin/env python3
import json
import subprocess
import sys
import os
import hashlib

# Configuration
GRAPH_FILE = "project-graph.json"
GRADLE_COMMAND = "./gradlew" 
GRADLE_TASK = "exportProjectGraph"

# Remote Cache Config (Set these in your CI environment)
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
        subprocess.check_call(["aws", "s3", "cp", full_s3_path, local_path, "--quiet"])
        return True
    except subprocess.CalledProcessError:
        return False

def s3_upload(local_path, remote_path):
    if not BUCKET: return
    try:
        full_s3_path = f"s3://{BUCKET}/{PREFIX}/{remote_path}"
        subprocess.check_call(["aws", "s3", "cp", local_path, full_s3_path, "--quiet"])
    except subprocess.CalledProcessError as e:
        print(f"Warning: Failed to upload cache to S3: {e}", file=sys.stderr)

def refresh_graph():
    """Runs the Gradle task to refresh the project dependency graph."""
    print("Cache miss or config changed. Refreshing project graph via Gradle...", file=sys.stderr)
    try:
        cmd = GRADLE_COMMAND if os.path.exists(GRADLE_COMMAND) else "gradle"
        subprocess.check_call([cmd, GRADLE_TASK, "--quiet"])
    except subprocess.CalledProcessError as e:
        print(f"Error refreshing graph: {e}", file=sys.stderr)
        sys.exit(1)

def get_git_changes(since_commit):
    """Returns a list of changed files since since_commit."""
    try:
        output = subprocess.check_output(['git', 'diff', '--name-only', since_commit]).decode('utf-8')
        return [f for f in output.strip().split('\n') if f]
    except subprocess.CalledProcessError:
        return []

def find_affected_projects(graph_file, changed_files):
    """Finds all projects affected by changed_files based on the graph."""
    with open(graph_file, 'r') as f:
        projects = json.load(f)

    # 1. Global triggers
    global_triggers = [
        "gradle/libs.versions.toml",
        "buildSrc/",
        "gradle.properties",
        "settings.gradle",
        "build.gradle"
    ]
    
    for file_path in changed_files:
        for trigger in global_triggers:
            if file_path.startswith(trigger):
                print(f"Global configuration change detected: {file_path}. Affecting all projects.", file=sys.stderr)
                return sorted([p['path'] for p in projects if p['path'] != ":"])

    # 2. Directory mapping
    affected_paths = set()
    for file_path in changed_files:
        best_match = None
        for p in sorted(projects, key=lambda x: len(x['dir']), reverse=True):
            p_dir = p['dir'].rstrip('/')
            if not p_dir or p_dir == '.': continue
            if file_path.startswith(p_dir + '/'):
                best_match = p['path']
                break
        if best_match:
            affected_paths.add(best_match)

    # 3. Inverted graph
    dependants = {p['path']: set() for p in projects}
    for p in projects:
        for dep in p.get('dependencies', []):
            if dep in dependants:
                dependants[dep].add(p['path'])

    # 4. Transitive closure
    total_affected = set()
    queue = list(affected_paths)
    while queue:
        current = queue.pop(0)
        if current not in total_affected:
            total_affected.add(current)
            if current in dependants:
                queue.extend(list(dependants[current]))

    return sorted(list(total_affected))

def main():
    if len(sys.argv) < 2:
        print("Usage: gradle-diff <since_commit> [task_suffix]")
        sys.exit(1)

    since_commit = sys.argv[1]
    task_suffix = sys.argv[2] if len(sys.argv) > 2 else "test"

    # 1. Calculate Configuration Hash
    build_scripts = []
    for root, dirs, files in os.walk('.'):
        if 'build' in dirs: dirs.remove('build')
        if '.git' in dirs: dirs.remove('.git')
        for f in files:
            if f.endswith(('.gradle', '.gradle.kts', '.toml', '.properties')):
                build_scripts.append(os.path.join(root, f))
    
    current_hash = get_hash(build_scripts)
    remote_key = f"graph-{current_hash}.json"

    # 2. Try to get cached graph (Local or Remote)
    stale = True
    if os.path.exists(GRAPH_FILE):
        # Check if local file matches current config
        hash_file = ".gradle-diff-hash"
        if os.path.exists(hash_file):
            with open(hash_file, 'r') as f:
                if f.read().strip() == current_hash:
                    stale = False

    if stale:
        # Try S3 Download
        if BUCKET and s3_download(remote_key, GRAPH_FILE):
            print(f"Remote cache hit for {current_hash}. Downloaded from S3.", file=sys.stderr)
            stale = False
        else:
            # Full Refresh
            refresh_graph()
            # Upload to S3 for future builds
            if BUCKET:
                s3_upload(GRAPH_FILE, remote_key)
        
        # Save local hash
        with open(".gradle-diff-hash", "w") as f:
            f.write(current_hash)

    # 3. Analyze Git Changes
    changed_files = get_git_changes(since_commit)
    if not changed_files:
        return

    affected = find_affected_projects(GRAPH_FILE, changed_files)
    
    if affected:
        print(" ".join([f"{p}:{task_suffix}" for p in affected if p != ":"]))

if __name__ == "__main__":
    main()
