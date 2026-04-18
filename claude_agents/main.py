"""CLI entry point for the multi-agent system."""

import argparse
import sys
from pathlib import Path

import anyio

from .agents import run_agent
from .build_orchestrator import run_build
from .config import AgentConfig
from .orchestrator import run_orchestrator
from .team_orchestrator import run_plan, run_plan_interactive, run_team


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-agent system for software engineering workflows",
    )
    parser.add_argument(
        "command",
        choices=["pm", "team", "build", "plan", "feature", "review", "docs", "bugfix", "pr-review"],
        help="Which agent to run (pm = freeform orchestrator, team = staged pipeline, build = multi-feature loop, plan = planner stage only)",
    )
    parser.add_argument(
        "task",
        nargs="?",
        default="",
        help="Task description, or product vision when command is `build`. "
             "Optional for `team` when --plan-file is provided.",
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
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=20,
        help="Max backlog tasks to execute in build mode (default: 20)",
    )
    parser.add_argument(
        "--plan-file",
        default="",
        help="Path to a pre-approved plan file. In team mode, skips the PLAN stage and uses this file instead.",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="For `plan`: converse with the planner turn-by-turn instead of one-shot. Use /save to write PLAN.md.",
    )
    parser.add_argument(
        "--create-pr",
        action="store_true",
        help="After a passing team/build run, branch + commit + push + open a PR (requires `gh`).",
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

    task_optional = (
        (args.command == "team" and args.plan_file)
        or (args.command == "plan" and args.interactive)
    )
    if not args.task and not task_optional:
        sys.exit(
            "Error: `task` is required except for `team --plan-file` or "
            "`plan --interactive`."
        )

    config = AgentConfig(
        project_dir=args.project_dir,
        github_repo=args.repo,
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget,
    )

    if args.command == "pm":
        await run_orchestrator(args.task, config)
    elif args.command == "plan":
        if args.interactive:
            plan_text = await run_plan_interactive(args.task, config)
            if not plan_text:
                return
        else:
            plan_text = await run_plan(args.task, config)
        plan_path = Path(config.project_dir) / "PLAN.md"
        plan_path.write_text(plan_text)
        print(f"\n\n[plan] Plan written to {plan_path}")
        follow_up_task = f"\"{args.task}\" " if args.task else ""
        print("[plan] Review/edit, then run: "
              f"claude-agents team {follow_up_task}-d {config.project_dir} "
              f"--plan-file {plan_path}")
    elif args.command == "team":
        plan_text: str | None = None
        if args.plan_file:
            plan_text = Path(args.plan_file).read_text()
            print(f"[team] Using plan from {args.plan_file}")
        await run_team(
            args.task,
            config,
            max_fix_iters=args.max_fix_iters,
            plan=plan_text,
            create_pr_on_pass=args.create_pr,
        )
    elif args.command == "build":
        await run_build(
            args.task,
            config,
            max_tasks=args.max_tasks,
            max_fix_iters=args.max_fix_iters,
            create_pr_on_pass=args.create_pr,
        )
    else:
        agent_name = COMMAND_TO_AGENT[args.command]
        await run_agent(agent_name, args.task, config)


def run():
    anyio.run(async_main)


if __name__ == "__main__":
    run()
