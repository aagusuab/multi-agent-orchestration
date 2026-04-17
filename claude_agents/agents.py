"""Agent definitions and runners for the multi-agent system."""

import anyio

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AgentDefinition,
    ResultMessage,
    AssistantMessage,
    SystemMessage,
    TextBlock,
)

from .config import AgentConfig
from .prompts import (
    FEATURE_BUILDER_PROMPT,
    CODE_REVIEWER_PROMPT,
    DOCUMENTATION_AGENT_PROMPT,
    BUG_FIXER_PROMPT,
    PR_REVIEWER_PROMPT,
)
from .tools import create_github_mcp_server


# ---------------------------------------------------------------------------
# Subagent definitions (used by the PM orchestrator)
# ---------------------------------------------------------------------------

def get_agent_definitions() -> dict[str, AgentDefinition]:
    """Return all subagent definitions for the orchestrator."""
    return {
        "feature-builder": AgentDefinition(
            description="Principal engineer that implements features with "
                        "comprehensive tests (base + edge cases). Follows "
                        "project conventions strictly.",
            prompt=FEATURE_BUILDER_PROMPT,
            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        ),
        "code-reviewer": AgentDefinition(
            description="Senior engineer that audits the entire codebase for "
                        "performance issues and security vulnerabilities. "
                        "Produces a severity-ranked report.",
            prompt=CODE_REVIEWER_PROMPT,
            tools=["Read", "Glob", "Grep", "Bash"],
        ),
        "documentation": AgentDefinition(
            description="Technical writer that creates or updates documentation "
                        "for new features and bug fixes.",
            prompt=DOCUMENTATION_AGENT_PROMPT,
            tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        ),
        "bug-fixer": AgentDefinition(
            description="Senior engineer that reads GitHub issues, reproduces "
                        "bugs, writes failing tests, and fixes them.",
            prompt=BUG_FIXER_PROMPT,
            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        ),
        "pr-reviewer": AgentDefinition(
            description="Staff engineer that reviews pull requests for "
                        "correctness, test coverage, security, and style.",
            prompt=PR_REVIEWER_PROMPT,
            tools=["Read", "Glob", "Grep", "Bash"],
        ),
    }


# ---------------------------------------------------------------------------
# Standalone agent runners (for running agents individually)
# ---------------------------------------------------------------------------

async def run_agent(
    agent_name: str,
    task: str,
    config: AgentConfig,
) -> str:
    """Run a single agent with the given task and return its result."""
    prompts = {
        "feature-builder": FEATURE_BUILDER_PROMPT,
        "code-reviewer": CODE_REVIEWER_PROMPT,
        "documentation": DOCUMENTATION_AGENT_PROMPT,
        "bug-fixer": BUG_FIXER_PROMPT,
        "pr-reviewer": PR_REVIEWER_PROMPT,
    }

    system_prompt = prompts.get(agent_name)
    if not system_prompt:
        raise ValueError(f"Unknown agent: {agent_name}")

    tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    if agent_name in ("code-reviewer", "pr-reviewer"):
        tools = ["Read", "Glob", "Grep", "Bash"]

    github_server = create_github_mcp_server()

    result_text = ""
    async for message in query(
        prompt=task,
        options=ClaudeAgentOptions(
            cwd=config.project_dir,
            system_prompt=system_prompt,
            allowed_tools=tools,
            permission_mode=config.permission_mode,
            model=config.model,
            max_turns=config.max_turns,
            mcp_servers={"github": github_server},
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)

    return result_text
