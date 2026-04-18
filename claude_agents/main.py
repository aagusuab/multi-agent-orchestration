"""CLI entry point for the multi-agent system."""

import argparse
import sys

import anyio

from .agents import run_agent
from .config import AgentConfig
from .orchestrator import run_orchestrator
from .team_orchestrator import run_team


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-agent system for software engineering workflows",
    )
    parser.add_argument(
        "command",
        choices=["pm", "team", "feature", "review", "docs", "bugfix", "pr-review"],
        help="Which agent to run (pm = freeform orchestrator, team = staged pipeline)",
    )
    parser.add_argument(
        "task",
        help="Task description or instruction for the agent",
    )
    parser.add_argument(
        "--project-dir", "-d",
        default=".",
        help="Path to the project directory (default: current directory)",
    )
    parser.add_argument(
        "--repo", "-r",
        default="",
        help="GitHub repository in owner/repo format",
    )
    parser.add_argument(
        "--model", "-m",
        default="claude-opus-4-6",
        help="Model to use (default: claude-opus-4-6)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=200,
        help="Max agent turns (default: 200)",
    )
    parser.add_argument(
        "--max-budget",
        type=float,
        default=5.0,
        help="Max budget in USD per agent run (default: 5.0)",
    )
    parser.add_argument(
        "--max-fix-iters",
        type=int,
        default=3,
        help="Max verify/fix iterations for team mode (default: 3)",
    )
    return parser.parse_args()


COMMAND_TO_AGENT = {
    "feature": "feature-builder",
    "review": "code-reviewer",
    "docs": "documentation",
    "bugfix": "bug-fixer",
    "pr-review": "pr-reviewer",
}


async def async_main():
    args = parse_args()

    config = AgentConfig(
        project_dir=args.project_dir,
        github_repo=args.repo,
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget,
    )

    if args.command == "pm":
        await run_orchestrator(args.task, config)
    elif args.command == "team":
        await run_team(args.task, config, max_fix_iters=args.max_fix_iters)
    else:
        agent_name = COMMAND_TO_AGENT[args.command]
        await run_agent(agent_name, args.task, config)


def run():
    anyio.run(async_main)


if __name__ == "__main__":
    run()
