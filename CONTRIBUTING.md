# Contributing to GridBear

Thank you for your interest in contributing to GridBear.

## Development Setup

### Prerequisites

- Python 3.11+
- Docker and Docker Compose v2
- Node.js 18+ (for Tailwind CSS builds)
- Git

### Getting Started

```bash
# Fork and clone
git clone https://github.com/YOUR_USERNAME/gridbear.git
cd gridbear

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install
pre-commit install --hook-type pre-push

# Copy config templates
cp .env.example .env
# Edit .env with your settings (at minimum: POSTGRES_PASSWORD)

# Start infrastructure
docker compose up -d postgres
```

### Running Tests

```bash
# All unit tests
pytest tests/unit

# Single test file
pytest tests/unit/test_foo.py

# With coverage
pytest --cov=core tests/unit

# Integration tests (requires running containers)
pytest -m integration
```

### Linting

Pre-commit hooks run automatically, but you can run them manually:

```bash
ruff check .          # Lint
ruff check --fix .    # Lint + auto-fix
ruff format .         # Format
```

### Building CSS

```bash
npm install
npm run css:build     # Production build
npm run css:watch     # Watch mode for development
```

## Code Style

- **Python**: PEP 8, enforced by ruff (line length 88, target Python 3.11)
- **Imports**: stdlib, third-party, framework, local — alphabetical within groups
- **SQL**: Always parameterized (`%s` placeholders for psycopg). Never string interpolation.
- **No `# coding: utf-8`** headers
- **Meaningful variable names** — no single-letter variables except loop indices

### Dependency Direction

```
plugins/ ──depends on──► core/
plugins/ ──depends on──► ui/ (for admin routes only)

core/ ──NEVER imports──► plugins/
ui/   ──NEVER imports──► plugins/  (use interfaces + registry)
```

If you need plugin functionality in core, define an interface in `core/interfaces/` and have the plugin implement it.

## Making Changes

### Branch Workflow

1. Create a feature branch from `master`
2. Make your changes in small, focused commits
3. Push to your fork
4. Open a Pull Request against `master`

### Commit Messages

Format: `[TAG] area: short description`

Tags: `FIX` `IMP` `ADD` `REM` `REF` `REV` `MOV` `REL` `MERGE` `CLA` `I18N` `PERF`

Examples:
```
[ADD] telegram: voice message transcription support
[FIX] memory: episodic search returns wrong user context
[IMP] ui: add loading spinner to plugin reload button
[REF] core: extract shared ToolAdapter base class
```

Rules:
- Max 50 characters for the subject line
- Use imperative mood, present tense ("add", not "added")
- Body lines max 80 characters
- Explain **why**, not what

### Plugin Development

To create a new plugin:

1. Create a directory under `plugins/your-plugin/`
2. Add a `manifest.json` with type, entry_point, dependencies
3. Implement the appropriate interface (channel, service, runner, or MCP provider)
4. Add admin routes in `plugins/your-plugin/admin/routes.py` (optional)
5. Add tests in `tests/unit/plugins/your-plugin/`
6. Enable in `config/plugins.json`

See existing plugins for reference. The `memo` plugin is a good minimal example.

## Pull Request Guidelines

Before opening a PR, consider discussing your approach on [Discord](https://discord.gg/WhTK4PPmaE) — especially for larger changes.

- One logical change per PR
- Include tests for new functionality
- All CI checks must pass (lint, tests, type-check)
- Describe the **why** in the PR description, not just the what

## Documentation

The public docs site (**[docs.gridbear.io](https://docs.gridbear.io)**) is built with MkDocs Material from the `docs/` directory. To preview locally:

```bash
pip install -e ".[dev]"
mkdocs serve
```

Open `http://localhost:8000`. Pages edit live on save. The GitHub Actions workflow `.github/workflows/docs.yml` deploys every push to `main` that touches `docs/**`, `mkdocs.yml`, or `CHANGELOG.md`.

## Reporting Issues & Proposing Changes

Pick the right channel:

- **[GitHub Issues](https://github.com/gridbeario/gridbear/issues)** — bug reports and concrete feature requests (something broken or a well-scoped ask ready to be worked on). Include steps to reproduce for bugs; check existing issues before opening a new one.
- **[GitHub Discussions](https://github.com/gridbeario/gridbear/discussions)** — open-ended conversations and design work:
  - *RFC / Design* — propose a non-trivial change (new plugin, architecture shift, cross-cutting refactor). Post the design first, iterate, then open a PR that links back to the discussion.
  - *Ideas* — informal "what if we…" suggestions not yet ready for a full RFC.
  - *Q&A* — usage questions, setup help.
- **[SECURITY.md](https://github.com/gridbeario/gridbear/blob/main/SECURITY.md)** — security vulnerabilities (do **not** open a public Issue or Discussion for these).

## License

By contributing, you agree that your contributions will be licensed under the [LGPL-3.0 License](https://github.com/gridbeario/gridbear/blob/main/LICENSE).
