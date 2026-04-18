# Multi-Agent Orchestration

A multi-agent system for software engineering workflows, built on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python). Six specialized AI agents handle feature development, code review, documentation, bug fixing, PR review, and project management.

## Agents

| Agent | Role | Responsibilities |
|---|---|---|
| **Feature Builder** | Principal Engineer | Implements features following project conventions, writes comprehensive tests (base + edge cases) |
| **Code Reviewer** | Senior Engineer | Audits codebase for performance bottlenecks and security vulnerabilities (OWASP Top 10) |
| **Documentation** | Technical Writer | Creates and updates documentation for new features and bug fixes |
| **Bug Fixer** | Senior Engineer | Reads GitHub issues, reproduces bugs with failing tests, applies minimal fixes |
| **PR Reviewer** | Staff Engineer | Reviews pull requests for correctness, test coverage, security, and style |
| **Project Manager** | PM / Orchestrator | Prioritizes work, delegates to other agents, coordinates end-to-end workflows |

## Architecture

```
                    +-----------------+
                    | Project Manager |
                    |  (Orchestrator) |
                    +--------+--------+
                             |
            +-------+--------+--------+--------+
            |       |        |        |        |
        +---v--+ +--v---+ +-v----+ +-v-----+ +v--------+
        |Feature| |Code  | |Docs  | |Bug    | |PR       |
        |Builder| |Review| |Agent | |Fixer  | |Reviewer |
        +---+---+ +--+---+ +-+----+ +-+-----+ ++--------+
            |         |      |        |         |
            +---------+------+--------+---------+
                             |
                    +--------v--------+
                    |  GitHub Tools   |
                    |  (MCP Server)   |
                    +-----------------+
```

The **Project Manager** agent uses the Claude Agent SDK's subagent system to spawn specialized agents as needed. Each subagent runs with its own system prompt, tool set, and isolated context. GitHub integration is handled via custom MCP tools wrapping the `gh` CLI.

## Orchestration Modes

Four orchestration modes are available:

### PM mode (freeform)
A single Project Manager agent analyzes the task and decides which subagents to invoke, in what order. Flexible — good when the shape of the work isn't known up front.

### Team mode (staged pipeline)
A deterministic pipeline with a verify/fix loop:

```
plan -> PRD -> exec -> [ verify -> fix ] * N
```

- **Plan** (read-only): a Principal Engineer drafts a plan from the repo state.
- **PRD** (read-only): a Tech Lead converts the plan into testable acceptance criteria.
- **Exec** (write): the executor implements against the PRD.
- **Verify** (read-only): the verifier runs tests/commands against each acceptance criterion and emits `VERIFICATION: PASS` or `VERIFICATION: FAIL`.
- **Fix** (write): if the verifier fails, the fixer applies minimal changes and the loop re-verifies.

The loop terminates early on PASS or when `--max-fix-iters` is exhausted. Plan/PRD/verify run on the light model to keep costs down; exec/fix run on the default model.

### Plan mode (planner stage only)
Runs just the planner and writes the result to `PLAN.md` in the project directory. Lets you review/edit the plan before committing to implementation. Feed the approved plan back into team mode with `--plan-file`.

### Build mode (multi-feature outer loop)
Takes a product vision, generates an ordered backlog of tasks (`BACKLOG.md` in the project dir), then runs the full team pipeline once per task:

```
vision -> backlog planner -> BACKLOG.md
           for each [ ] task in BACKLOG.md:
               team pipeline (plan -> PRD -> exec -> verify -> fix)
               mark [x] on pass / [!] on fail
```

The backlog is persisted, so runs are **resumable** — re-running `build` against an existing `BACKLOG.md` continues from the next unchecked task. You can also hand-edit the backlog between runs. Failed tasks are marked `- [!]` and skipped; the loop does not abort on failure.

## Prerequisites

- Python 3.10+
- [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/) (`pip install claude-agent-sdk`)
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated for GitHub features

## Installation

```bash
git clone git@github.com:aagusuab/multi-agent-orchestration.git
cd multi-agent-orchestration
pip install -e .
```

## Usage

### Project Manager (freeform orchestrator)

Let the PM analyze your project and delegate work across all agents:

```bash
claude-agents pm "Review open issues, fix critical bugs, then do a security audit" \
  --project-dir /path/to/your/project \
  --repo owner/repo
```

### Team (staged pipeline)

Run the plan -> PRD -> exec -> verify -> fix pipeline for a single task. Best when you want deterministic stages and a persistent verify/fix loop until acceptance criteria pass:

```bash
claude-agents team "Add a token-bucket rate limiter middleware to the API" \
  --project-dir /path/to/your/project \
  --max-fix-iters 3
```

### Plan, then team (review the plan before executing)

Generate a plan, review/edit `PLAN.md`, then run team mode against the approved plan:

```bash
claude-agents plan "Add a token-bucket rate limiter middleware to the API" \
  --project-dir /path/to/your/project
# edit PLAN.md as needed
claude-agents team --plan-file PLAN.md \
  --project-dir /path/to/your/project
```

When `--plan-file` is used, the `task` argument is optional — the plan's `## Goal` section carries the intent.

### Build (multi-feature outer loop)

Describe a product vision and let the system decompose it into a backlog and execute each task through the full team pipeline:

```bash
claude-agents build "A CLI todo app with SQLite storage, add/list/complete commands, and JSON export" \
  --project-dir /path/to/new/project \
  --max-tasks 10 \
  --max-fix-iters 3
```

`BACKLOG.md` is written to the project directory. Re-run the same command to resume from the next `- [ ]` task. Edit the backlog file between runs to adjust priorities or add tasks.

### Individual Agents

Run any agent directly for focused tasks:

```bash
# Implement a new feature
claude-agents feature "Add rate limiter middleware to the API" -d /path/to/project

# Security and performance audit
claude-agents review "Audit the authentication module" -d /path/to/project

# Fix a bug from GitHub issues
claude-agents bugfix "Fix issue #42" -d /path/to/project -r owner/repo

# Write documentation
claude-agents docs "Document the new caching layer" -d /path/to/project

# Review a pull request
claude-agents pr-review "Review PR #15" -d /path/to/project -r owner/repo
```

### Options

| Flag | Description | Default |
|---|---|---|
| `--project-dir`, `-d` | Path to the project directory | `.` |
| `--repo`, `-r` | GitHub repository (`owner/repo`) | |
| `--model`, `-m` | Claude model to use | `claude-opus-4-6` |
| `--max-turns` | Max agent turns per run | `200` |
| `--max-budget` | Max budget in USD per run | `5.0` |
| `--max-fix-iters` | Max verify/fix iterations (team/build modes) | `3` |
| `--max-tasks` | Max backlog tasks to execute (build mode only) | `20` |
| `--plan-file` | Path to a pre-approved plan file (team mode) | |

## Project Structure

```
claude_agents/
  config.py              # AgentConfig dataclass
  prompts.py             # System prompts for all agents and pipeline stages
  tools.py               # Custom MCP tools for GitHub integration
  agents.py              # Agent definitions and standalone runner
  orchestrator.py        # PM agent that coordinates subagents (freeform mode)
  team_orchestrator.py   # Staged pipeline with verify/fix loop (team mode, plan mode)
  build_orchestrator.py  # Multi-feature outer loop over a persisted backlog (build mode)
  main.py                # CLI entry point
```

## License

MIT
