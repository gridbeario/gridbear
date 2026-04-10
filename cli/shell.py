"""Server-side interactive shell — like 'odoo shell'.

Bootstraps the GridBear runtime (database, plugins, agents, ORM)
and drops into a Python REPL with everything pre-loaded.

Must be run inside the container where core/ and plugins/ are available.

Security note: This is a developer tool that intentionally executes
arbitrary Python code (like odoo shell, django shell, python -c).
It is NOT exposed to end users or network requests.
"""

import asyncio
import code
import os


def _bootstrap_runtime():
    """Initialize the GridBear runtime and return the namespace dict.

    Mirrors the bootstrap sequence from main.py but without starting
    channels, message handlers, or the event loop.
    """
    # Load .env if present
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from config.logging_config import logger
    from config.settings import BASE_DIR

    namespace = {"__name__": "__gridbear_shell__", "logger": logger}

    # Database
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set. Are you inside the container?")
        return None

    from core.database import DatabaseManager
    from core.registry import set_database

    db = DatabaseManager(database_url)
    asyncio.get_event_loop().run_until_complete(db.initialize())
    set_database(db)
    namespace["db"] = db

    # ORM
    from core.orm import Registry as ORMRegistry

    ORMRegistry.initialize(db)
    namespace["orm"] = ORMRegistry

    # Expose Model() helper for quick ORM access: Model('schema.name')
    def _get_model(dotted_name: str):
        parts = dotted_name.split(".", 1)
        if len(parts) != 2:
            print("Usage: Model('schema.name'), e.g. Model('oauth2.clients')")
            return None
        return ORMRegistry.get_model(parts[0], parts[1])

    namespace["Model"] = _get_model

    # Secrets manager
    try:
        from ui.secrets_manager import reset_secrets_manager, secrets_manager

        reset_secrets_manager()
        namespace["secrets"] = secrets_manager
    except Exception:
        pass

    # Plugin path resolver
    from core.plugin_paths import PluginPathResolver, build_plugin_dirs
    from core.registry import set_path_resolver

    path_resolver = PluginPathResolver(build_plugin_dirs(BASE_DIR))
    set_path_resolver(path_resolver)

    # Plugin manager
    from core.plugin_manager import PluginManager
    from core.registry import set_plugin_manager

    pm = PluginManager(
        path_resolver=path_resolver,
        config_path=BASE_DIR / "config" / "plugins.json",  # migration only
    )
    asyncio.get_event_loop().run_until_complete(pm.load_all(exclude_types=["channel"]))
    set_plugin_manager(pm)
    namespace["pm"] = pm

    # Models registry
    from core.models_registry import ModelsRegistry
    from core.registry import set_models_registry

    models_registry = ModelsRegistry()
    set_models_registry(models_registry)

    # Agent manager
    from core.agent_manager import AgentManager
    from core.registry import set_agent_manager

    am = AgentManager(plugin_manager=pm)
    asyncio.get_event_loop().run_until_complete(am.load_all())
    set_agent_manager(am)
    namespace["am"] = am

    # Convenience helpers
    namespace["runners"] = pm.runners
    namespace["services"] = pm.services
    namespace["channels"] = pm.channels
    namespace["mcp_providers"] = pm.mcp_providers
    namespace["agents"] = am.list_agents

    # Async helper: run coroutines from the sync REPL
    loop = asyncio.get_event_loop()
    namespace["run"] = loop.run_until_complete

    # MCP helpers
    namespace["mcp_servers"] = lambda: list(pm.get_all_mcp_server_names() or [])

    logger.info("GridBear shell ready.")
    return namespace


def _print_banner(namespace: dict):
    """Print the shell welcome banner."""
    pm = namespace.get("pm")
    am = namespace.get("am")

    svc_count = len(pm.services) + len(pm.runners) + len(pm.mcp_providers) if pm else 0
    agent_list = am.list_agents() if am else []

    print()
    print("=" * 60)
    print("  GridBear Interactive Shell")
    print("=" * 60)
    print()
    print(f"  Plugins:  {svc_count} loaded")
    print(f"  Agents:   {[a['name'] for a in agent_list]}")
    print()
    print("  Available objects:")
    print("    db             — DatabaseManager")
    print("    pm             — PluginManager")
    print("    am             — AgentManager")
    print("    orm            — ORM Registry")
    print("    Model(n)       — Get ORM model (e.g. Model('oauth2.clients'))")
    print("    secrets        — SecretsManager")
    print("    runners        — dict of runners")
    print("    services       — dict of services")
    print("    channels       — dict of channels")
    print("    mcp_providers  — dict of MCP providers")
    print("    agents()       — list agents")
    print("    run(coro)      — run async coroutine")
    print("    mcp_servers()  — list MCP server names")
    print()
    print("  Type help(obj) for docs. Ctrl+D to exit.")
    print()


def _run_code(source: str, filename: str, namespace: dict) -> int:
    """Compile and run Python source in the shell namespace.

    This is a developer tool — intentional code execution, same as
    python -c, odoo shell, or django shell. Not exposed to end users.

    Returns exit code (0 = success, 1 = error).
    """
    try:
        compiled = compile(source, filename, "exec")
    except SyntaxError:
        import traceback

        traceback.print_exc()
        return 1

    try:
        # Developer shell: intentional arbitrary code execution
        # like Python's own -c flag or code.InteractiveConsole
        builtins = (
            __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
        )
        builtins["exec"](compiled, namespace)  # noqa: S102
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except Exception:
        import traceback

        traceback.print_exc()
        return 1


def run_shell(command: str | None = None, script: str | None = None) -> int:
    """Entry point for the shell command.

    Returns exit code (0 = success, 1 = error).
    """
    # Ensure we have an event loop
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    try:
        namespace = _bootstrap_runtime()
    except Exception as e:
        print(f"ERROR: Runtime bootstrap failed: {e}")
        import traceback

        traceback.print_exc()
        return 1

    if namespace is None:
        return 1

    if command:
        return _run_code(command, "<cli>", namespace)

    if script:
        script_path = os.path.abspath(script)
        if not os.path.exists(script_path):
            print(f"ERROR: Script not found: {script_path}")
            return 1
        with open(script_path) as f:
            source = f.read()
        return _run_code(source, script_path, namespace)

    # Interactive REPL
    _print_banner(namespace)

    # Try IPython first for a better experience
    try:
        from IPython import start_ipython

        start_ipython(argv=[], user_ns=namespace)
    except ImportError:
        # Fallback to stdlib interactive console
        interactive = code.InteractiveConsole(locals=namespace)
        interactive.interact(banner="", exitmsg="Goodbye.")

    return 0
