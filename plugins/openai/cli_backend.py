"""OpenAI CLI Backend using Codex CLI.

Subprocess-based backend using ``codex exec --json`` for JSONL event streaming.
Codex handles the tool loop autonomously — this backend parses events for
logging, streaming, and cost tracking.

Codex authenticates via ``codex login`` (ChatGPT Plus subscription) or via
API key (``codex login --with-api-key``).  MCP servers are configured through
``~/.codex/config.toml`` which we write before launching the subprocess.
"""

import asyncio
import json
import os
import shutil
from pathlib import Path

from config.logging_config import logger
from core.interfaces.runner import RunnerResponse
from plugins.openai.cost_tracker import calculate_cost

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
CODEX_CONFIG_DIR = Path.home() / ".codex"


def _find_codex_binary() -> str:
    """Locate the codex binary, preferring global install over npx."""
    path = shutil.which("codex")
    if path:
        return path
    return "npx"


class OpenAICliBackend:
    """OpenAI Codex CLI backend via subprocess.

    Uses ``codex exec --json`` which outputs JSONL events to stdout.
    Codex manages MCP tool execution internally — we only parse events.
    """

    def __init__(self, config: dict):
        self.config = config
        self.model = config.get("model", os.getenv("OPENAI_MODEL", ""))
        self.timeout = config.get("timeout", 120)
        self.max_retries = config.get("max_retries", 2)
        self._gateway_url = os.getenv("MCP_GATEWAY_URL", "http://gridbear-ui:8080")
        self._codex_bin = _find_codex_binary()

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
        progress_callback=None,
        error_callback=None,
        tool_callback=None,
        stream_callback=None,
        agent_id: str | None = None,
        model: str | None = None,
        no_tools: bool = False,
        **kwargs,
    ) -> RunnerResponse:
        """Execute Codex CLI and return parsed response."""
        effective_model = model or self.model
        agent_label = agent_id or "default"

        logger.info(
            "[%s] Codex CLI call: model=%s, prompt_len=%d",
            agent_label,
            effective_model or "(default)",
            len(prompt),
        )

        # Write MCP gateway config before launching
        unified_id = kwargs.get("unified_id")
        if not no_tools and agent_id:
            self._write_mcp_config(agent_id, unified_id=unified_id)

        cmd = self._build_command(
            prompt=prompt,
            model=effective_model,
        )
        logger.debug("Running command: %s", " ".join(cmd))

        # Pass MCP token as env var for Codex config.toml
        env = os.environ.copy()
        mcp_token = getattr(self, "_current_mcp_token", None)
        if mcp_token:
            env["GRIDBEAR_MCP_TOKEN"] = mcp_token

        for attempt in range(self.max_retries + 1):
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    limit=4 * 1024 * 1024,  # 4MB buffer
                )

                text, thread_id, usage, error_msg = await asyncio.wait_for(
                    self._parse_jsonl_stream(process, stream_callback, tool_callback),
                    timeout=self.timeout,
                )

                await process.wait()

                # Log stderr for debugging
                if process.stderr:
                    stderr_data = await process.stderr.read()
                    if stderr_data:
                        stderr_text = stderr_data.decode().strip()
                        if stderr_text:
                            logger.debug(
                                "[%s] Codex stderr: %s",
                                agent_label,
                                stderr_text[:500],
                            )

                if error_msg:
                    logger.error("[%s] Codex CLI error: %s", agent_label, error_msg)
                    if error_callback:
                        await error_callback("codex_error", error_msg)
                    return RunnerResponse(
                        text=f"Codex error: {error_msg}",
                        session_id=session_id,
                        cost_usd=0.0,
                        is_error=True,
                        raw={},
                    )

                cost = 0.0
                if usage:
                    in_tok = usage.get("input_tokens", 0)
                    out_tok = usage.get("output_tokens", 0)
                    cost = calculate_cost(effective_model or "codex", in_tok, out_tok)

                logger.info(
                    "[%s] Codex CLI done: cost=$%.6f, thread=%s",
                    agent_label,
                    cost,
                    thread_id or "none",
                )

                return RunnerResponse(
                    text=text or "",
                    session_id=thread_id or session_id,
                    cost_usd=cost,
                    raw={
                        "model": effective_model or "codex-default",
                        "runner": "openai-cli",
                        "usage": usage or {},
                    },
                )

            except asyncio.TimeoutError:
                logger.error(
                    "[%s] Codex CLI timed out after %ds",
                    agent_label,
                    self.timeout,
                )
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass

                if error_callback:
                    await error_callback(
                        "timeout",
                        {
                            "timeout_seconds": self.timeout,
                            "attempt": attempt + 1,
                        },
                    )

                if attempt < self.max_retries:
                    await asyncio.sleep(1)
                    continue

                return RunnerResponse(
                    text=(
                        f"Timeout: request took more than {self.timeout} "
                        "seconds. Try simplifying the request."
                    ),
                    session_id=session_id,
                    cost_usd=0.0,
                    is_error=True,
                    raw={},
                )

            except Exception as e:
                logger.exception("[%s] Codex CLI error: %s", agent_label, e)
                if error_callback:
                    await error_callback("exception", {"error": str(e)})
                return RunnerResponse(
                    text=f"Error: {e}",
                    session_id=session_id,
                    cost_usd=0.0,
                    is_error=True,
                    raw={},
                )

        return RunnerResponse(
            text="Error: all retries failed.",
            session_id=session_id,
            cost_usd=0.0,
            is_error=True,
            raw={},
        )

    def _build_command(
        self,
        prompt: str,
        model: str | None = None,
    ) -> list[str]:
        """Build Codex CLI command.

        Codex exec outputs JSONL events when ``--json`` is passed.
        MCP servers are configured via ``~/.codex/config.toml`` (not CLI flags).
        """
        if self._codex_bin == "npx":
            cmd = ["npx", "@openai/codex", "exec"]
        else:
            cmd = [self._codex_bin, "exec"]

        # NOTE: We don't use codex's session resume because session_ids
        # from the sessions plugin are not codex thread_ids.
        # Each call is a fresh codex exec invocation.

        cmd.append("--json")

        if model:
            cmd.extend(["--model", model])

        cmd.extend(
            [
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
            ]
        )
        cmd.append(prompt)

        return cmd

    async def _parse_jsonl_stream(
        self,
        process: asyncio.subprocess.Process,
        stream_callback=None,
        tool_callback=None,
    ) -> tuple[str, str | None, dict, str | None]:
        """Parse Codex JSONL events from stdout.

        Real Codex CLI event format (v0.101+):
          {"type":"thread.started","thread_id":"..."}
          {"type":"turn.started"}
          {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
          {"type":"item.completed","item":{"type":"mcp_tool_call","name":"...","arguments":{...}}}
          {"type":"turn.completed","usage":{"input_tokens":N,"output_tokens":N,...}}
          {"type":"error","message":"..."}
          {"type":"turn.failed","error":{"message":"..."}}

        Returns (text, thread_id, usage, error_message).
        """
        text = ""
        thread_id = None
        usage = {}
        error_msg = None

        async for raw_line in process.stdout:
            line = raw_line.decode().strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Non-JSON line from Codex: %s", line[:200])
                continue

            event_type = event.get("type", "")

            if event_type == "thread.started":
                thread_id = event.get("thread_id")

            elif event_type == "item.completed":
                item = event.get("item", {})
                item_type = item.get("type", "")

                if item_type == "agent_message":
                    text = item.get("text", "")
                    if stream_callback:
                        try:
                            await stream_callback(text)
                        except Exception:
                            pass

                elif item_type == "mcp_tool_call" and tool_callback:
                    try:
                        await tool_callback(
                            item.get("name", ""),
                            item.get("arguments", {}),
                        )
                    except Exception as e:
                        logger.debug("tool_callback error: %s", e)

            elif event_type == "turn.completed":
                usage = event.get("usage", {})

            elif event_type == "error":
                error_msg = event.get("message", "Unknown error")
                logger.warning("Codex error event: %s", error_msg)

            elif event_type == "turn.failed":
                err = event.get("error", {})
                error_msg = err.get("message", "Turn failed")
                logger.warning("Codex turn failed: %s", error_msg)

        return text, thread_id, usage, error_msg

    def _write_mcp_config(self, agent_id: str, unified_id: str | None = None) -> bool:
        """Write Codex-compatible MCP config for the gateway.

        Codex reads MCP servers from ``~/.codex/config.toml``.
        We write/update the ``[mcp_servers.gridbear-gateway]`` section.

        When ``unified_id`` is provided, we create a per-user token so the
        gateway can connect to user-aware MCP servers (e.g. Odoo) with the
        correct per-user OAuth2 credentials.
        """
        from core.mcp_token_manager import get_mcp_token_manager

        tm = get_mcp_token_manager()
        if not tm:
            return False

        if unified_id:
            # Create a per-user token so the gateway resolves user_identity
            token = self._get_user_token(tm, agent_id, unified_id)
        else:
            token = tm.get_token(agent_id)

        if not token:
            return False

        config_path = CODEX_CONFIG_DIR / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing config (preserve other settings)
        existing = ""
        if config_path.exists():
            existing = config_path.read_text()

        # Remove old gridbear-gateway section if present
        lines = existing.split("\n")
        new_lines = []
        skip = False
        for line in lines:
            if line.strip() == "[mcp_servers.gridbear-gateway]":
                skip = True
                continue
            if skip and line.strip().startswith("["):
                skip = False
            if not skip:
                new_lines.append(line)

        # Append gateway config; token passed via env var at process spawn
        gateway_section = (
            "\n[mcp_servers.gridbear-gateway]\n"
            f'url = "{self._gateway_url}/mcp"\n'
            'bearer_token_env_var = "GRIDBEAR_MCP_TOKEN"\n'
        )
        content = "\n".join(new_lines).rstrip() + gateway_section
        config_path.write_text(content)

        # Store token so _build_command can pass it as env var
        self._current_mcp_token = token
        return True

    @staticmethod
    def _get_user_token(tm, agent_id: str, unified_id: str) -> str | None:
        """Create a short-lived token with user_identity for Codex CLI.

        The gateway reads ``token.user_identity`` to connect user-aware
        MCP servers (e.g. Odoo OAuth2) with the correct per-user creds.
        """
        # Get the agent's OAuth2 client
        client = tm.oauth2_db.get_by_agent_name(agent_id)
        if not client:
            logger.warning(
                "No OAuth2 client for agent %s — cannot create user token",
                agent_id,
            )
            return None

        token_obj = tm.oauth2_db.create_access_token(
            client_pk=client.id,
            user_identity=unified_id,
            scope="mcp",
            access_expiry=300,  # 5 min — one CLI invocation
            include_refresh=False,
        )
        return token_obj.token
