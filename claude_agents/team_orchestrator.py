"""Team-mode staged orchestrator: plan -> PRD -> exec -> verify -> fix loop.

Coexists with the freeform PM orchestrator. Each stage is its own query(),
and artifacts are threaded forward as plain text. The exec/verify/fix loop
runs until the verifier emits `VERIFICATION: PASS` or the iteration cap hits.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit import PromptSession

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
    INTERACTIVE_VERIFIER_PROMPT,
    FIXER_PROMPT,
)
from .tools import create_github_mcp_server


PASS_SENTINEL = "VERIFICATION: PASS"
FAIL_SENTINEL = "VERIFICATION: FAIL"


def _join_sections(*sections: str) -> str:
    """Join non-empty sections with a markdown horizontal-rule separator."""
    return "\n\n---\n\n".join(s for s in sections if s and s.strip())


@dataclass
class TokenTracker:
    """Accumulates token usage and cost across agent stages."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    cost_usd: float = 0.0
    stages: list[tuple[str, dict]] = field(default_factory=list)

    def record(self, stage_name: str, rm) -> None:
        usage = rm.usage or {} if hasattr(rm, "usage") else {}
        cost = (rm.total_cost_usd or 0.0) if hasattr(rm, "total_cost_usd") else 0.0
        entry = {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "cache_create": usage.get("cache_creation_input_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "cost": cost,
        }
        self.input_tokens += entry["input"]
        self.output_tokens += entry["output"]
        self.cache_creation += entry["cache_create"]
        self.cache_read += entry["cache_read"]
        self.cost_usd += cost
        self.stages.append((stage_name, entry))

    def absorb(self, other: "TokenTracker") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation += other.cache_creation
        self.cache_read += other.cache_read
        self.cost_usd += other.cost_usd
        self.stages.extend(other.stages)

    def summary(self, title: str = "TOKEN USAGE") -> str:
        lines = [f"\n===== {title} ====="]
        lines.append(
            f"{'stage':<30} {'in':>8} {'out':>8} "
            f"{'cache_r':>8} {'cache_w':>8} {'cost':>10}"
        )
        for name, u in self.stages:
            lines.append(
                f"{name[:30]:<30} {u['input']:>8} {u['output']:>8} "
                f"{u['cache_read']:>8} {u['cache_create']:>8} "
                f"${u['cost']:>9.4f}"
            )
        lines.append("-" * 78)
        lines.append(
            f"{'TOTAL':<30} {self.input_tokens:>8} {self.output_tokens:>8} "
            f"{self.cache_read:>8} {self.cache_creation:>8} "
            f"${self.cost_usd:>9.4f}"
        )
        return "\n".join(lines)


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
    token_tracker: "TokenTracker" = field(default_factory=lambda: TokenTracker())


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


def _print_assistant_blocks(message: AssistantMessage) -> str:
    """Stream an assistant message's text and tool-use blocks to stdout.

    Separates consecutive messages with a trailing newline so bursts of
    short commentary ("Now updating X:" + "Now updating Y:") render on
    their own lines instead of running together. Also prints tool-use
    markers so the user can see what the agent is actually doing.

    Returns the concatenated text content (for buffering/parsing).
    """
    text_parts: list[str] = []
    last_char_newline = True
    for block in message.content:
        if isinstance(block, TextBlock):
            print(block.text, end="", flush=True)
            text_parts.append(block.text)
            last_char_newline = block.text.endswith("\n")
        elif isinstance(block, ToolUseBlock):
            args_preview = ""
            if isinstance(block.input, dict):
                for key in ("file_path", "pattern", "command", "path"):
                    if key in block.input:
                        args_preview = f" {block.input[key]}"
                        break
            prefix = "" if last_char_newline else "\n"
            print(f"{prefix}[→ {block.name}{args_preview}]", flush=True)
            last_char_newline = True
    if not last_char_newline:
        print()
    return "".join(text_parts)


async def _run_stage(
    stage_name: str,
    system_prompt: str,
    user_prompt: str,
    config: AgentConfig,
    tools: list[str],
    github_server,
    model: str | None = None,
    tracker: TokenTracker | None = None,
) -> str:
    """Run one pipeline stage as a single query() call, streaming output."""
    print(f"\n\n===== STAGE: {stage_name} =====\n")
    result_text = ""
    last_result_msg = None
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
            last_result_msg = message
        elif isinstance(message, AssistantMessage):
            _print_assistant_blocks(message)
    if tracker is not None and last_result_msg is not None:
        tracker.record(stage_name, last_result_msg)
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
    tracker = TokenTracker()
    result = await _run_stage(
        "PLAN",
        PLANNER_PROMPT,
        base_task,
        config,
        ["Read", "Glob", "Grep", "Bash"],
        github_server,
        model=config.light_model,
        tracker=tracker,
    )
    print(tracker.summary("PLAN TOKEN USAGE"))
    return result


async def _stream_turn(
    client: ClaudeSDKClient,
    tracker: TokenTracker | None = None,
    stage_label: str = "turn",
) -> str:
    """Consume one response turn, printing text and tool activity. Returns final text."""
    buffered = ""
    async for message in client.receive_response():
        if isinstance(message, ResultMessage):
            if tracker is not None:
                tracker.record(stage_label, message)
        elif isinstance(message, AssistantMessage):
            buffered += _print_assistant_blocks(message)
    return buffered


_PLAN_BLOCK_RE = re.compile(r"(# Plan\b.*?)(?=\n#\s|\Z)", re.DOTALL)


def _extract_last_plan(text: str) -> str:
    """Return the last `# Plan` block found in text, stripped. Empty if none."""
    matches = _PLAN_BLOCK_RE.findall(text or "")
    return matches[-1].strip() if matches else ""


async def run_plan_interactive(initial_task: str, config: AgentConfig) -> tuple[str, bool]:
    """Interactive planning session. Returns (plan_markdown, auto_run_team).

    Commands:
    - `/save` -> (plan, False): write PLAN.md and exit.
    - `/save-and-run` -> (plan, True): write PLAN.md and chain into team mode.
    - `/quit` -> ("", False): abort without saving.

    Robustness: the full assistant transcript is buffered so that if the
    final FINALIZE response doesn't contain a `# Plan` block (e.g., the
    agent points back at an earlier draft), we fall back to the most recent
    `# Plan` block anywhere in the conversation.
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
    print(
        "Commands: `/save` writes PLAN.md and exits, "
        "`/save-and-run` writes PLAN.md then chains into team mode, "
        "`/quit` exits without saving.\n"
    )

    transcript = ""
    tracker = TokenTracker()
    turn_counter = 0
    session: PromptSession = PromptSession()

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
        turn_counter += 1
        transcript += await _stream_turn(client, tracker, f"turn {turn_counter}") + "\n\n"

        while True:
            user_input = await session.prompt_async("\n\n> ", multiline=False)
            text = user_input.strip()
            if text == "/quit":
                print("[plan] Aborted.")
                print(tracker.summary("INTERACTIVE PLAN TOKEN USAGE"))
                return "", False
            if text in ("/save", "/save-and-run"):
                auto_run = text == "/save-and-run"
                await client.query(
                    "FINALIZE. Output the complete final plan right now in "
                    "the structured `# Plan` markdown format from your "
                    "instructions. Emit the whole plan verbatim - do not "
                    "reference earlier messages. This response is parsed "
                    "and saved as-is."
                )
                print()
                turn_counter += 1
                final = await _stream_turn(client, tracker, f"turn {turn_counter} (finalize)")
                transcript += final + "\n\n"
                print(tracker.summary("INTERACTIVE PLAN TOKEN USAGE"))

                plan = _extract_last_plan(final)
                if not plan:
                    plan = _extract_last_plan(transcript)
                    if plan:
                        print(
                            "\n[plan] Finalize response had no `# Plan` "
                            "block; recovered the most recent one from the "
                            "conversation."
                        )
                if not plan:
                    print(
                        "\n[plan] WARNING: No `# Plan` block found anywhere "
                        "in the conversation. Saving raw last response - "
                        "you will likely need to re-plan."
                    )
                    plan = final.strip()
                return plan, auto_run
            if not text:
                continue
            await client.query(user_input)
            turn_counter += 1
            transcript += await _stream_turn(client, tracker, f"turn {turn_counter}") + "\n\n"


async def run_verify_interactive(
    prd: str,
    exec_report: str | None,
    config: AgentConfig,
) -> None:
    """Interactive verification session.

    Feeds the verifier the PRD (and optionally the most recent exec report),
    runs an initial PASS/FAIL check against the current repo state, then
    lets the user ask follow-up questions. Read-only — the agent cannot
    modify code.
    """
    github_server = create_github_mcp_server()

    opening_parts = ["Here is the PRD to verify:", prd]
    if exec_report:
        opening_parts += [
            "---",
            "Most recent executor report for context:",
            exec_report,
        ]
    opening_parts += [
        "---",
        "Run each acceptance criterion against the current repo state. "
        "For each one, emit `[PASS|FAIL] <criterion>` with concrete "
        "evidence (test name, command output, file:line). End with a short "
        "list of decisions for me to make. Then wait for my follow-ups.",
    ]
    opening = "\n\n".join(opening_parts)

    print("\n===== INTERACTIVE VERIFY =====")
    print("Commands: `/quit` to exit. Read-only session - agent cannot edit code.\n")

    tracker = TokenTracker()
    turn = 0
    session: PromptSession = PromptSession()

    async with ClaudeSDKClient(
        options=ClaudeAgentOptions(
            cwd=config.project_dir,
            system_prompt=INTERACTIVE_VERIFIER_PROMPT,
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode=config.permission_mode,
            model=config.light_model,
            max_turns=config.max_turns,
            mcp_servers={"github": github_server},
        )
    ) as client:
        await client.query(opening)
        turn += 1
        await _stream_turn(client, tracker, f"turn {turn}")

        while True:
            user_input = await session.prompt_async("\n\n> ", multiline=False)
            text = user_input.strip()
            if text == "/quit":
                print("[verify] Exiting.")
                print(tracker.summary("INTERACTIVE VERIFY TOKEN USAGE"))
                return
            if not text:
                continue
            await client.query(user_input)
            turn += 1
            await _stream_turn(client, tracker, f"turn {turn}")


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
    project_dir = Path(config.project_dir)

    def _save_exec_artifact() -> None:
        artifact = result.exec_report or ""
        if result.fix_reports:
            artifact += "\n\n---\n\n" + "\n\n---\n\n".join(
                f"Fix iter {i + 1}:\n{fr}" for i, fr in enumerate(result.fix_reports)
            )
        if artifact:
            (project_dir / "EXEC_REPORT.md").write_text(artifact)

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
            tracker=result.token_tracker,
        )

    result.prd = await _run_stage(
        "PRD",
        PRD_PROMPT,
        _join_sections(base_task, result.plan),
        config,
        read_only_tools,
        github_server,
        model=config.light_model,
        tracker=result.token_tracker,
    )
    if result.prd:
        (project_dir / "PRD.md").write_text(result.prd)
        print(f"\n[team] PRD saved to {project_dir / 'PRD.md'}")

    exec_input = _join_sections(base_task, result.plan, result.prd)
    result.exec_report = await _run_stage(
        "EXEC",
        EXEC_LEAD_PROMPT,
        exec_input,
        config,
        write_tools,
        github_server,
        tracker=result.token_tracker,
    )
    _save_exec_artifact()

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
            tracker=result.token_tracker,
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
            tracker=result.token_tracker,
        )
        result.fix_reports.append(fix_report)
        _save_exec_artifact()

    result.commit_message = _best_commit_message(result.exec_report, result.fix_reports)

    print("\n\n===== TEAM RUN SUMMARY =====")
    print(f"Passed: {result.passed}")
    print(f"Iterations: {result.iterations}")
    print(f"Fix rounds: {len(result.fix_reports)}")
    if result.commit_message:
        print(f"Commit message: {result.commit_message.splitlines()[0]}")
    print(result.token_tracker.summary("TEAM RUN TOKEN USAGE"))

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
