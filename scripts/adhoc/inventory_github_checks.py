#!/usr/bin/env python3
"""Privately inventory check annotations and additional publication metadata.

Input: existing external GitHub export directory and adjacent private mirror.
Output: raw private API responses and summary. Publication review, 2026-10-25.
Uses read-only requests; no GitHub mutations, raw output or credential access.
"""

import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    root = Path(__file__).resolve().parents[2]
    if directory == root or root in directory.parents:
        parser.error("private exports must be outside the publication repository")
    metadata = json.loads((directory / "repository.json").read_text())
    repository = metadata["full_name"]
    mirror = directory.parent / "github-mirror.git"
    commits = subprocess.run(["git", "--git-dir", str(mirror), "rev-list", "--all"], check=True, capture_output=True, text=True).stdout.splitlines()
    summary = {}

    def request(name, endpoint, query=None):
        command = ["gh", "api", endpoint]
        command += ["-f", "query=" + query] if query else ["--paginate", "--slurp"]
        result = subprocess.run(command, capture_output=True)
        (directory / (name + ".json")).write_bytes(result.stdout)
        (directory / (name + ".stderr.txt")).write_bytes(result.stderr)
        summary[name] = {"exit_code": result.returncode}
        if result.returncode:
            return []
        return json.loads(result.stdout)

    checks = set()
    annotations = 0
    for commit in commits:
        pages = request("checks_" + commit, f"repos/{repository}/commits/{commit}/check-runs?per_page=100")
        for page in pages:
            for check in page["check_runs"]:
                checks.add(check["id"])
        request("statuses_" + commit, f"repos/{repository}/commits/{commit}/statuses?per_page=100")
    for identifier in sorted(checks):
        pages = request("check_annotations_" + str(identifier), f"repos/{repository}/check-runs/{identifier}/annotations?per_page=100")
        annotations += sum(len(page) for page in pages)
    owner, name = repository.split("/")
    query = "query { repository(owner:" + json.dumps(owner) + ",name:" + json.dumps(name) + ") { usesCustomOpenGraphImage openGraphImageUrl homepageUrl description } }"
    request("additional_repository_metadata", "graphql", query)
    query = "query { repository(owner:" + json.dumps(owner) + ",name:" + json.dumps(name) + ") { projectsV2(first:100) { totalCount nodes { id title url } pageInfo { hasNextPage } } } }"
    request("repository_projects", "graphql", query)
    summary["counts"] = {"reachable_commits": len(commits), "check_runs": len(checks), "annotations": annotations}
    (directory / "additional-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
