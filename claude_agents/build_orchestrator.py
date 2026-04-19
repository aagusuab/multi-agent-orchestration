"""Build-mode orchestrator: generate a backlog, then run the team pipeline
per task in a plain Python outer loop.

Backlog is persisted to BACKLOG.md in the project directory so runs are
resumable and the user can hand-edit tasks between iterations.
"""

from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    TextBlock,
)

from .config import AgentConfig
from .prompts import BACKLOG_PLANNER_PROMPT
from .team_orchestrator import TeamRunResult, TokenTracker, run_team
from .tools import create_github_mcp_server


BACKLOG_FILENAME = "BACKLOG.md"

UNCHECKED = "- [ ]"
PASSED = "- [x]"
FAILED = "- [!]"


@dataclass
class BuildRunResult:
    vision: str
    backlog_path: str
    task_results: list[tuple[str, TeamRunResult]] = field(default_factory=list)
    completed: bool = False
    token_tracker: TokenTracker = field(default_factory=TokenTracker)


def _backlog_path(config: AgentConfig) -> Path:
    return Path(config.project_dir) / BACKLOG_FILENAME


def _parse_unchecked_tasks(text: str) -> list[tuple[str, str]]:
    """Return (raw_line, task_description) for every `- [ ]` line, in order."""
    tasks = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(UNCHECKED):
            description = stripped[len(UNCHECKED):].strip()
            tasks.append((line, description))
    return tasks


def _mark_task(path: Path, raw_line: str, marker: str) -> None:
    """Replace `raw_line` with the same line using `marker` instead of `- [ ]`.

    Raises if the line isn't found — silent failure would cause the outer
    loop to re-pick the same task forever.
    """
    content = path.read_text()
    if raw_line not in content:
        raise RuntimeError(
            f"Could not find task line in backlog to mark: {raw_line!r}"
        )
    replacement = raw_line.replace(UNCHECKED, marker, 1)
    path.write_text(content.replace(raw_line, replacement, 1))


async def _generate_backlog(
    vision: str,
    config: AgentConfig,
    tracker: TokenTracker | None = None,
) -> str:
    """Run the backlog planner and return its raw Markdown output."""
    print("\n\n===== STAGE: BACKLOG PLANNER =====\n")
    github_server = create_github_mcp_server()
    result_text = ""
    last_rm = None
    async for message in query(
        prompt=f"Product vision:\n\n{vision}",
        options=ClaudeAgentOptions(
            cwd=config.project_dir,
            system_prompt=BACKLOG_PLANNER_PROMPT,
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode=config.permission_mode,
            model=config.light_model,
            max_turns=config.max_turns,
            mcp_servers={"github": github_server},
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or result_text
            last_rm = message
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)
    if tracker is not None and last_rm is not None:
        tracker.record("BACKLOG PLANNER", last_rm)
    return result_text


async def run_build(
    vision: str,
    config: AgentConfig,
    max_tasks: int = 20,
    max_fix_iters: int = 3,
    create_pr_on_pass: bool = False,
) -> BuildRunResult:
    """Generate or resume a backlog, then run the team pipeline per task.

    If BACKLOG.md already exists in project_dir, the backlog is reused and
    any remaining `- [ ]` tasks are executed. Otherwise a new backlog is
    generated from the vision and written to disk.

    Tasks that pass verification are marked `- [x]`; tasks that exhaust the
    inner fix loop are marked `- [!]` and the outer loop moves on.
    """
    path = _backlog_path(config)
    result = BuildRunResult(vision=vision, backlog_path=str(path))

    if path.exists():
        print(f"\n[build] Resuming from existing backlog: {path}")
    else:
        backlog_md = await _generate_backlog(vision, config, tracker=result.token_tracker)
        if "# Backlog" not in backlog_md:
            raise RuntimeError(
                "Backlog planner did not return a recognizable backlog. "
                f"Got:\n{backlog_md[:500]}"
            )
        path.write_text(backlog_md)
        print(f"\n[build] Backlog written to {path}")

    executed = 0
    while executed < max_tasks:
        remaining = _parse_unchecked_tasks(path.read_text())
        if not remaining:
            result.completed = True
            break

        raw_line, task = remaining[0]
        executed += 1
        print(f"\n\n===== TASK {executed}/{max_tasks}: {task} =====\n")

        team_result = await run_team(
            task,
            config,
            max_fix_iters=max_fix_iters,
            create_pr_on_pass=create_pr_on_pass,
        )
        result.task_results.append((task, team_result))
        result.token_tracker.absorb(team_result.token_tracker)

        _mark_task(path, raw_line, PASSED if team_result.passed else FAILED)

    print("\n\n===== BUILD RUN SUMMARY =====")
    print(f"Backlog: {path}")
    print(f"Tasks attempted: {len(result.task_results)}")
    passed = sum(1 for _, r in result.task_results if r.passed)
    print(f"Tasks passed: {passed}")
    print(f"Tasks failed: {len(result.task_results) - passed}")
    print(f"Backlog drained: {result.completed}")
    print(result.token_tracker.summary("BUILD RUN TOKEN USAGE"))
    return result
