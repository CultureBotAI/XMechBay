#!/usr/bin/env python3
"""Privately export GitHub publication surfaces using read-only API requests.

Inputs: owner/repository, external private output directory, authenticated gh.
Outputs: raw private JSON/log archives plus a summary without content values.
Serves repository publication review; 2026-10-25. Never commit the exports.
"""

import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if output == root or root in output.parents:
        parser.error("raw private exports must be outside the publication repository")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    summary = {}
    base = "repos/" + args.repository

    def request(name, endpoint, paginate=True, graphql=None, binary=False):
        command = ["gh", "api", endpoint]
        if paginate:
            command += ["--paginate", "--slurp"]
        if graphql:
            command += ["-f", "query=" + graphql]
        result = subprocess.run(command, capture_output=True)
        suffix = ".zip" if binary and result.returncode == 0 else ".json"
        (output / (name + suffix)).write_bytes(result.stdout)
        (output / (name + ".stderr.txt")).write_bytes(result.stderr)
        info = {"exit_code": result.returncode, "bytes": len(result.stdout)}
        summary[name] = info
        if result.returncode or binary:
            return None
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            info["invalid_json"] = True
            return None
        if paginate and isinstance(value, list) and all(isinstance(page, list) for page in value):
            value = [item for page in value for item in page]
        if isinstance(value, list):
            if value and all(isinstance(page, dict) and "total_count" in page for page in value):
                info["total_count"] = value[0]["total_count"]
            else:
                info["count"] = len(value)
        return value

    metadata = request("repository", base, False)
    endpoints = {
        "branches": "/branches?per_page=100",
        "tags": "/tags?per_page=100",
        "issues": "/issues?state=all&per_page=100",
        "pulls": "/pulls?state=all&per_page=100",
        "issue_comments": "/issues/comments?per_page=100",
        "review_comments": "/pulls/comments?per_page=100",
        "commit_comments": "/comments?per_page=100",
        "events": "/issues/events?per_page=100",
        "actions_runs": "/actions/runs?per_page=100",
        "artifacts": "/actions/artifacts?per_page=100",
        "caches": "/actions/caches?per_page=100",
        "releases": "/releases?per_page=100",
        "forks": "/forks?per_page=100",
        "deployments": "/deployments?per_page=100",
        "environments": "/environments?per_page=100",
        "pages": "/pages",
    }
    values = {name: request(name, base + endpoint) for name, endpoint in endpoints.items()}
    review_values = []
    for pull in values["pulls"] or []:
        number = pull["number"]
        request(f"pr_{number}_details", base + f"/pulls/{number}", False)
        reviews = request(f"pr_{number}_reviews", base + f"/pulls/{number}/reviews?per_page=100")
        review_values.extend(reviews or [])
        request(f"pr_{number}_commits", base + f"/pulls/{number}/commits?per_page=100")
        request(f"pr_{number}_files", base + f"/pulls/{number}/files?per_page=100")
    for page in values["actions_runs"] or []:
        for run in page.get("workflow_runs", []):
            identifier = run["id"]
            request(f"run_{identifier}_jobs", base + f"/actions/runs/{identifier}/jobs?per_page=100")
            request(f"run_{identifier}_logs", base + f"/actions/runs/{identifier}/logs", False, binary=True)
    edit_nodes = []
    for name in ("issues", "issue_comments", "review_comments", "commit_comments"):
        for item in values[name] or []:
            if item.get("node_id"):
                edit_nodes.append((name, str(item.get("number", item["id"])), item["node_id"]))
    for review in review_values:
        if review.get("node_id"):
            edit_nodes.append(("reviews", str(review["id"]), review["node_id"]))
    fragments = " ".join(
        "... on " + typename + " { userContentEdits(first:100, after:$cursor) { nodes { id diff editedAt deletedAt editor { login } } pageInfo { hasNextPage endCursor } } }"
        for typename in ("Issue", "PullRequest", "IssueComment", "PullRequestReview", "PullRequestReviewComment", "CommitComment")
    )
    for kind, identifier, node_id in edit_nodes:
        cursor = None
        page_number = 0
        while True:
            query = "query { node(id:" + json.dumps(node_id) + ") { " + fragments.replace("$cursor", json.dumps(cursor)) + " } }"
            page = request(f"edits_{kind}_{identifier}_{page_number}", "graphql", False, graphql=query)
            connection = ((page or {}).get("data", {}).get("node") or {}).get("userContentEdits") or {}
            info = connection.get("pageInfo", {})
            if not info.get("hasNextPage"):
                break
            cursor = info["endCursor"]
            page_number += 1
    refs = subprocess.run(["git", "ls-remote", "https://github.com/" + args.repository + ".git"], capture_output=True)
    (output / "advertised_refs.txt").write_bytes(refs.stdout)
    (output / "advertised_refs.stderr.txt").write_bytes(refs.stderr)
    summary["advertised_refs"] = {"exit_code": refs.returncode, "count": len(refs.stdout.splitlines())}
    if metadata:
        summary["features"] = {key: metadata.get(key) for key in ("private", "has_wiki", "has_discussions", "has_pages", "forks_count")}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
