"""Team-mode staged orchestrator: plan -> PRD -> exec -> verify -> fix loop.

Coexists with the freeform PM orchestrator. Each stage is its own query(),
and artifacts are threaded forward as plain text. The exec/verify/fix loop
runs until the verifier emits `VERIFICATION: PASS` or the iteration cap hits.
"""

from dataclasses import dataclass, field

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    TextBlock,
)

from .config import AgentConfig
from .prompts import (
    PLANNER_PROMPT,
    PRD_PROMPT,
    EXEC_LEAD_PROMPT,
    VERIFIER_PROMPT,
    FIXER_PROMPT,
)
from .tools import create_github_mcp_server


PASS_SENTINEL = "VERIFICATION: PASS"
FAIL_SENTINEL = "VERIFICATION: FAIL"


@dataclass
class TeamRunResult:
    plan: str = ""
    prd: str = ""
    exec_report: str = ""
    verify_reports: list[str] = field(default_factory=list)
    fix_reports: list[str] = field(default_factory=list)
    passed: bool = False
    iterations: int = 0


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


async def run_team(
    task: str,
    config: AgentConfig,
    max_fix_iters: int = 3,
) -> TeamRunResult:
    """Run the staged Team pipeline for a single task.

    Stages: plan -> PRD -> exec -> verify -> (fix -> verify) * up to max_fix_iters.
    Terminates early when the verifier emits VERIFICATION: PASS.
    """
    github_server = create_github_mcp_server()
    result = TeamRunResult()

    repo_context = (
        f"\n\nGitHub repository: {config.github_repo}" if config.github_repo else ""
    )
    base_task = f"Task: {task}{repo_context}"

    read_only_tools = ["Read", "Glob", "Grep", "Bash"]
    write_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

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
        f"{base_task}\n\n---\n\n{result.plan}",
        config,
        read_only_tools,
        github_server,
        model=config.light_model,
    )

    exec_input = (
        f"{base_task}\n\n---\n\n{result.plan}\n\n---\n\n{result.prd}"
    )
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

    print("\n\n===== TEAM RUN SUMMARY =====")
    print(f"Passed: {result.passed}")
    print(f"Iterations: {result.iterations}")
    print(f"Fix rounds: {len(result.fix_reports)}")
    return result
