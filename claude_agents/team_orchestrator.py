"""Team-mode staged orchestrator: plan -> PRD -> exec -> verify -> fix loop.

Coexists with the freeform PM orchestrator. Each stage is its own query(),
and artifacts are threaded forward as plain text. The exec/verify/fix loop
runs until the verifier emits `VERIFICATION: PASS` or the iteration cap hits.
"""

import re
from dataclasses import dataclass, field

import anyio

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
)

from .config import AgentConfig
from .pr_creator import create_pr
from .prompts import (
    PLANNER_PROMPT,
    INTERACTIVE_PLANNER_PROMPT,
    PRD_PROMPT,
    EXEC_LEAD_PROMPT,
    VERIFIER_PROMPT,
    FIXER_PROMPT,
)
from .tools import create_github_mcp_server


PASS_SENTINEL = "VERIFICATION: PASS"
FAIL_SENTINEL = "VERIFICATION: FAIL"


def _join_sections(*sections: str) -> str:
    """Join non-empty sections with a markdown horizontal-rule separator."""
    return "\n\n---\n\n".join(s for s in sections if s and s.strip())


@dataclass
class TeamRunResult:
    plan: str = ""
    prd: str = ""
    exec_report: str = ""
    verify_reports: list[str] = field(default_factory=list)
    fix_reports: list[str] = field(default_factory=list)
    passed: bool = False
    iterations: int = 0
    commit_message: str = ""


_COMMIT_MSG_RE = re.compile(
    r"##\s+Suggested commit message\s*\n+```[^\n]*\n(.*?)\n```",
    re.DOTALL,
)


def _extract_commit_message(report: str) -> str:
    """Pull the fenced commit message out of an Exec/Fix Report. Empty if absent."""
    m = _COMMIT_MSG_RE.search(report or "")
    return m.group(1).strip() if m else ""


def _best_commit_message(exec_report: str, fix_reports: list[str]) -> str:
    """Prefer the latest fix's message (it saw exec + fixes). Fall back to exec."""
    for fr in reversed(fix_reports):
        msg = _extract_commit_message(fr)
        if msg:
            return msg
    return _extract_commit_message(exec_report)


async def _run_stage(
    stage_name: str,
    system_prompt: str,
    user_prompt: str,
    config: AgentConfig,
    tools: list[str],
    github_server,
    model: str | None = None,
) -> str:
    """Run one pipeline stage as a single query() call, streaming output."""
    print(f"\n\n===== STAGE: {stage_name} =====\n")
    result_text = ""
    async for message in query(
        prompt=user_prompt,
        options=ClaudeAgentOptions(
            cwd=config.project_dir,
            system_prompt=system_prompt,
            allowed_tools=tools,
            permission_mode=config.permission_mode,
            model=model or config.model,
            max_turns=config.max_turns,
            mcp_servers={"github": github_server},
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or result_text
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)
    return result_text


async def run_plan(task: str, config: AgentConfig) -> str:
    """Run only the PLAN stage and return its markdown output.

    Used by the standalone `plan` command so the user can review/edit a plan
    before feeding it into `team --plan-file`.
    """
    github_server = create_github_mcp_server()
    repo_context = (
        f"\n\nGitHub repository: {config.github_repo}" if config.github_repo else ""
    )
    base_task = f"Task: {task}{repo_context}"
    return await _run_stage(
        "PLAN",
        PLANNER_PROMPT,
        base_task,
        config,
        ["Read", "Glob", "Grep", "Bash"],
        github_server,
        model=config.light_model,
    )


async def _stream_turn(client: ClaudeSDKClient) -> str:
    """Consume one response turn, printing text and tool activity. Returns final text."""
    buffered = ""
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)
                    buffered += block.text
                elif isinstance(block, ToolUseBlock):
                    args_preview = ""
                    if isinstance(block.input, dict):
                        for key in ("file_path", "pattern", "command", "path"):
                            if key in block.input:
                                args_preview = f" {block.input[key]}"
                                break
                    print(f"\n[→ {block.name}{args_preview}]", flush=True)
    return buffered


async def run_plan_interactive(initial_task: str, config: AgentConfig) -> str:
    """Interactive planning session. Returns the final plan markdown.

    The user converses with the planner until typing `/save`, at which point
    the agent is asked to emit the final structured plan. `/quit` aborts
    without saving.
    """
    github_server = create_github_mcp_server()
    repo_context = (
        f"\n\nGitHub repository: {config.github_repo}" if config.github_repo else ""
    )
    opening = (
        f"Task: {initial_task}{repo_context}\n\n"
        "Start by reading the repo to ground yourself, then ask me your "
        "clarifying questions. Do not produce the final plan yet."
    ) if initial_task else (
        "I want to plan something, but let me drive. Read the repo first to "
        "ground yourself, then ask me what I want to build."
    )

    print("\n===== INTERACTIVE PLAN =====")
    print("Commands: `/save` writes PLAN.md and exits, `/quit` exits without saving.\n")

    async with ClaudeSDKClient(
        options=ClaudeAgentOptions(
            cwd=config.project_dir,
            system_prompt=INTERACTIVE_PLANNER_PROMPT,
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode=config.permission_mode,
            model=config.light_model,
            max_turns=config.max_turns,
            mcp_servers={"github": github_server},
        )
    ) as client:
        await client.query(opening)
        await _stream_turn(client)

        while True:
            user_input = await anyio.to_thread.run_sync(input, "\n\n> ")
            text = user_input.strip()
            if text == "/quit":
                print("[plan] Aborted.")
                return ""
            if text == "/save":
                await client.query("FINALIZE")
                print()
                final = await _stream_turn(client)
                return final.strip()
            if not text:
                continue
            await client.query(user_input)
            await _stream_turn(client)


async def run_team(
    task: str,
    config: AgentConfig,
    max_fix_iters: int = 3,
    plan: str | None = None,
    create_pr_on_pass: bool = False,
) -> TeamRunResult:
    """Run the staged Team pipeline for a single task.

    Stages: plan -> PRD -> exec -> verify -> (fix -> verify) * up to max_fix_iters.
    Terminates early when the verifier emits VERIFICATION: PASS.

    If `plan` is provided, the PLAN stage is skipped and the supplied plan is
    threaded forward to the PRD stage.

    If `create_pr_on_pass` is True and verification passes, the changes are
    branched, committed, pushed, and opened as a PR (gh required for the
    final PR step).
    """
    github_server = create_github_mcp_server()
    result = TeamRunResult()

    sections = []
    if task:
        sections.append(f"Task: {task}")
    if config.github_repo:
        sections.append(f"GitHub repository: {config.github_repo}")
    base_task = "\n\n".join(sections)

    read_only_tools = ["Read", "Glob", "Grep", "Bash"]
    write_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

    if plan is not None:
        print("\n\n===== STAGE: PLAN (provided, skipping generation) =====\n")
        result.plan = plan
    else:
        result.plan = await _run_stage(
            "PLAN",
            PLANNER_PROMPT,
            base_task,
            config,
            read_only_tools,
            github_server,
            model=config.light_model,
        )

    result.prd = await _run_stage(
        "PRD",
        PRD_PROMPT,
        _join_sections(base_task, result.plan),
        config,
        read_only_tools,
        github_server,
        model=config.light_model,
    )

    exec_input = _join_sections(base_task, result.plan, result.prd)
    result.exec_report = await _run_stage(
        "EXEC",
        EXEC_LEAD_PROMPT,
        exec_input,
        config,
        write_tools,
        github_server,
    )

    for iteration in range(max_fix_iters + 1):
        result.iterations = iteration + 1
        verify_input = (
            f"{result.prd}\n\n---\n\nExecutor report:\n{result.exec_report}"
        )
        if result.fix_reports:
            verify_input += f"\n\n---\n\nLatest fix report:\n{result.fix_reports[-1]}"

        verify_report = await _run_stage(
            f"VERIFY (iter {iteration + 1})",
            VERIFIER_PROMPT,
            verify_input,
            config,
            read_only_tools,
            github_server,
            model=config.light_model,
        )
        result.verify_reports.append(verify_report)

        last_line = verify_report.strip().splitlines()[-1] if verify_report.strip() else ""
        if PASS_SENTINEL in last_line:
            result.passed = True
            break
        if FAIL_SENTINEL not in last_line:
            print(
                f"\n[team] Verifier did not emit a clear sentinel; "
                f"treating as FAIL. Last line: {last_line!r}"
            )

        if iteration >= max_fix_iters:
            break

        fix_input = (
            f"{result.prd}\n\n---\n\nExecutor report:\n{result.exec_report}"
            f"\n\n---\n\nVerifier failure report:\n{verify_report}"
        )
        fix_report = await _run_stage(
            f"FIX (iter {iteration + 1})",
            FIXER_PROMPT,
            fix_input,
            config,
            write_tools,
            github_server,
        )
        result.fix_reports.append(fix_report)

    result.commit_message = _best_commit_message(result.exec_report, result.fix_reports)

    print("\n\n===== TEAM RUN SUMMARY =====")
    print(f"Passed: {result.passed}")
    print(f"Iterations: {result.iterations}")
    print(f"Fix rounds: {len(result.fix_reports)}")
    if result.commit_message:
        print(f"Commit message: {result.commit_message.splitlines()[0]}")

    if create_pr_on_pass and result.passed:
        print("\n===== CREATING PR =====")
        pr_result = create_pr(
            task,
            result.prd,
            config.project_dir,
            config.branch_prefix,
            commit_message=result.commit_message or None,
        )
        print(pr_result.summary())

    return result
