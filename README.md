# gradle-diff

`gradle-diff` is a high-performance tool designed for Gradle monorepos to identify and execute only the tasks affected by a Git changeset. It combines the **accuracy** of the Gradle Tooling API with the **speed** of static analysis through a smart caching mechanism.

## Key Features

-   **Sub-second Execution:** Analyzes changes in milliseconds on cache hits.
-   **100% Accurate:** Uses an exported dependency graph from Gradle rather than fragile regex parsing.
-   **S3 Remote Caching:** Perfectly suited for ephemeral CI agents (Jenkins, GitHub Actions, etc.).
-   **Safety First:** Automatically triggers full builds if global configurations like `buildSrc`, Version Catalogs (`libs.versions.toml`), or `settings.gradle` change.
-   **Rebase-Aware:** Specifically designed for trunk-based development workflows.

## How it Works

1.  **Extract:** A lightweight Gradle script (`gradle-diff.gradle`) exports the project's dependency structure to a JSON file.
2.  **Cache:** The Python CLI hashes your build configuration. If the hash matches, it skips the slow Gradle configuration phase and uses the cached JSON (locally or from S3).
3.  **Analyze:** It performs a `git diff` and traverses the dependency graph to find all affected downstream projects.
4.  **Execute:** It outputs a space-separated list of tasks (e.g., `:app:test :lib-a:test`) ready to be piped back into Gradle.

## Setup

### 1. Apply the Gradle Script
Add the following to your root `settings.gradle` or `build.gradle` to enable graph extraction:

```groovy
// settings.gradle
gradle.rootProject {
    apply from: 'gradle-diff.gradle'
}
```

### 2. Configure Environment (Optional for S3)
To enable remote caching across ephemeral agents, set:
- `GRADLE_DIFF_S3_BUCKET`: Your S3 bucket name.
- `GRADLE_DIFF_S3_PREFIX`: (Optional) S3 folder path.

### Reporting
You can generate structured reports for CI artifacts:
```bash
# JSON report for automation
python3 gradle-diff.py HEAD~1 test --report build/report.json

# HTML report for a visual dashboard
python3 gradle-diff.py HEAD~1 test --html-report build/report.html
```

### CI Pipeline (Jenkins/Trunk-Based)
For a feature branch that has been rebased onto `main`:
```bash
# 1. Find the point where the branch diverged from main
export SINCE=$(git merge-base HEAD origin/main)

# 2. Get the affected tasks
export TASKS=$(python3 gradle-diff.py $SINCE test assemble)

# 3. Run only what's necessary
if [ -n "$TASKS" ]; then
  ./gradlew $TASKS
fi
```

## Advanced Logic
- **Global Triggers:** Changes to any files in `buildSrc/`, `gradle/libs.versions.toml`, or root build scripts will cause the tool to return tasks for **all** subprojects.
- **Transitive Impact:** If `Library A` changes and `App B` depends on it, the tool will correctly identify that both `Library A` and `App B` need to be tested.

## Requirements
- Python 3.x
- AWS CLI (if using S3 caching)
- Gradle 7.0+ (recommended)
