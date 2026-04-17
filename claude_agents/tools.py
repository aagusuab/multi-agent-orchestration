"""Custom MCP tools for GitHub integration and agent coordination."""

import json
import subprocess

from claude_agent_sdk import tool, create_sdk_mcp_server


def _run_gh(args: list[str]) -> str:
    """Run a GitHub CLI command and return its output."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout.strip()


@tool(
    "list_github_issues",
    "List open GitHub issues, optionally filtered by labels",
    {"repo": str, "labels": str, "limit": int},
)
async def list_github_issues(args):
    cmd = ["issue", "list", "--repo", args["repo"], "--json",
           "number,title,body,labels,assignees", "--limit", str(args.get("limit", 20))]
    labels = args.get("labels", "")
    if labels:
        cmd.extend(["--label", labels])
    output = _run_gh(cmd)
    return {"content": [{"type": "text", "text": output}]}


@tool(
    "get_github_issue",
    "Get details of a specific GitHub issue",
    {"repo": str, "issue_number": int},
)
async def get_github_issue(args):
    output = _run_gh([
        "issue", "view", str(args["issue_number"]),
        "--repo", args["repo"],
        "--json", "number,title,body,labels,comments,state",
    ])
    return {"content": [{"type": "text", "text": output}]}


@tool(
    "list_pull_requests",
    "List open pull requests in a GitHub repository",
    {"repo": str, "limit": int},
)
async def list_pull_requests(args):
    output = _run_gh([
        "pr", "list", "--repo", args["repo"],
        "--json", "number,title,headRefName,author,additions,deletions,files",
        "--limit", str(args.get("limit", 20)),
    ])
    return {"content": [{"type": "text", "text": output}]}


@tool(
    "get_pull_request",
    "Get details and diff of a specific pull request",
    {"repo": str, "pr_number": int},
)
async def get_pull_request(args):
    pr_info = _run_gh([
        "pr", "view", str(args["pr_number"]),
        "--repo", args["repo"],
        "--json", "number,title,body,headRefName,files,reviews,comments,additions,deletions",
    ])
    pr_diff = _run_gh([
        "pr", "diff", str(args["pr_number"]),
        "--repo", args["repo"],
    ])
    combined = json.dumps({
        "info": json.loads(pr_info) if not pr_info.startswith("Error") else pr_info,
        "diff": pr_diff[:50000],  # Limit diff size
    }, indent=2)
    return {"content": [{"type": "text", "text": combined}]}


@tool(
    "create_pull_request",
    "Create a pull request from the current branch",
    {"repo": str, "title": str, "body": str, "base": str},
)
async def create_pull_request(args):
    output = _run_gh([
        "pr", "create",
        "--repo", args["repo"],
        "--title", args["title"],
        "--body", args["body"],
        "--base", args.get("base", "main"),
    ])
    return {"content": [{"type": "text", "text": output}]}


@tool(
    "review_pull_request",
    "Submit a review on a pull request (approve, request changes, or comment)",
    {"repo": str, "pr_number": int, "event": str, "body": str},
)
async def review_pull_request(args):
    event = args["event"]  # APPROVE, REQUEST_CHANGES, or COMMENT
    output = _run_gh([
        "pr", "review", str(args["pr_number"]),
        "--repo", args["repo"],
        f"--{event.lower().replace('_', '-')}",
        "--body", args["body"],
    ])
    return {"content": [{"type": "text", "text": output}]}


def create_github_mcp_server():
    """Create an MCP server with all GitHub tools."""
    return create_sdk_mcp_server(
        "github-tools",
        tools=[
            list_github_issues,
            get_github_issue,
            list_pull_requests,
            get_pull_request,
            create_pull_request,
            review_pull_request,
        ],
    )
