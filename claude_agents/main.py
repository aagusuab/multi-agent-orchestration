"""CLI entry point for the multi-agent system."""

import argparse
import sys
from pathlib import Path

import anyio

from .agents import run_agent
from .build_orchestrator import run_build
from .config import AgentConfig
from .orchestrator import run_orchestrator
from .team_orchestrator import (
    run_plan,
    run_plan_interactive,
    run_team,
    run_verify_interactive,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-agent system for software engineering workflows",
    )
    parser.add_argument(
        "command",
        choices=["pm", "team", "build", "plan", "verify", "feature", "review", "docs", "bugfix", "pr-review"],
        help="Which agent to run (pm = freeform, team = staged pipeline, build = multi-feature loop, plan = planner only, verify = interactive verify against a PRD)",
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
    parser.add_argument(
        "--prd-file",
        default="",
        help="Path to a PRD file. Used by `verify` to check the repo against "
             "acceptance criteria. Defaults to PRD.md in --project-dir.",
    )
    parser.add_argument(
        "--exec-file",
        default="",
        help="Path to an exec report file. Optional context for `verify`. "
             "Defaults to EXEC_REPORT.md in --project-dir if present.",
    )
    # parse_intermixed_args handles the case where `nargs=?` positionals follow
    # store_true flags (e.g., `claude-agents plan --interactive "task"`).
    return parser.parse_intermixed_args()


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
        or args.command == "verify"
    )
    if not args.task and not task_optional:
        sys.exit(
            "Error: `task` is required except for `team --plan-file`, "
            "`plan --interactive`, or `verify`."
        )

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        sys.exit(
            f"Error: --project-dir {project_dir} does not exist or is not a directory."
        )

    config = AgentConfig(
        project_dir=str(project_dir),
        github_repo=args.repo,
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget,
    )

    if args.command == "pm":
        await run_orchestrator(args.task, config)
    elif args.command == "plan":
        auto_run = False
        if args.interactive:
            plan_text, auto_run = await run_plan_interactive(args.task, config)
            if not plan_text:
                return
        else:
            plan_text = await run_plan(args.task, config)
        plan_path = Path(config.project_dir) / "PLAN.md"
        plan_path.write_text(plan_text)
        print(f"\n\n[plan] Plan written to {plan_path}")
        if auto_run:
            print("[plan] /save-and-run: chaining into team mode with this plan.\n")
            await run_team(
                args.task,
                config,
                max_fix_iters=args.max_fix_iters,
                plan=plan_text,
                create_pr_on_pass=args.create_pr,
            )
        else:
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
    elif args.command == "verify":
        prd_path = Path(args.prd_file) if args.prd_file else Path(config.project_dir) / "PRD.md"
        if not prd_path.exists():
            sys.exit(
                f"Error: PRD not found at {prd_path}. Pass --prd-file or "
                "run a team pipeline first (which auto-saves PRD.md)."
            )
        prd_text = prd_path.read_text()
        exec_path = Path(args.exec_file) if args.exec_file else Path(config.project_dir) / "EXEC_REPORT.md"
        exec_text = exec_path.read_text() if exec_path.exists() else None
        if exec_text:
            print(f"[verify] Using exec context from {exec_path}")
        await run_verify_interactive(prd_text, exec_text, config)
    else:
        agent_name = COMMAND_TO_AGENT[args.command]
        await run_agent(agent_name, args.task, config)


def run():
    anyio.run(async_main)


if __name__ == "__main__":
    run()
