"""Project Manager orchestrator that coordinates all agents."""

import anyio

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    TextBlock,
)

from .agents import get_agent_definitions
from .config import AgentConfig
from .prompts import PROJECT_MANAGER_PROMPT
from .tools import create_github_mcp_server


async def run_orchestrator(task: str, config: AgentConfig) -> str:
    """Run the PM orchestrator which delegates to specialized agents.

    The PM agent has access to all subagents and GitHub tools. It decides
    which agents to invoke, in what order, and coordinates their work.
    """
    github_server = create_github_mcp_server()
    agents = get_agent_definitions()

    repo_context = ""
    if config.github_repo:
        repo_context = f"\n\nGitHub repository: {config.github_repo}"

    full_prompt = f"{task}{repo_context}"

    result_text = ""
    async for message in query(
        prompt=full_prompt,
        options=ClaudeAgentOptions(
            cwd=config.project_dir,
            system_prompt=PROJECT_MANAGER_PROMPT,
            allowed_tools=["Read", "Glob", "Grep", "Bash", "Agent"],
            agents=agents,
            permission_mode=config.permission_mode,
            model=config.model,
            max_turns=config.max_turns,
            mcp_servers={"github": github_server},
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result
            print(f"\n\n--- PM Final Report ---\n{result_text}")
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)

    return result_text
