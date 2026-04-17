"""Configuration for the multi-agent system."""

from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """Configuration for the agent system."""

    # The target project directory agents will work on
    project_dir: str = "."

    # GitHub repository in "owner/repo" format
    github_repo: str = ""

    # Model to use for agents
    model: str = "claude-opus-4-6"

    # Model for lighter-weight agents (docs, simple reviews)
    light_model: str = "claude-sonnet-4-6"

    # Max turns per agent invocation
    max_turns: int = 200

    # Max budget per agent run in USD
    max_budget_usd: float = 5.0

    # Permission mode for agents
    permission_mode: str = "acceptEdits"

    # Git branch prefix for agent work
    branch_prefix: str = "agent/"

    # Labels to filter GitHub issues for the bug fixer
    bug_labels: list[str] = field(default_factory=lambda: ["bug"])
