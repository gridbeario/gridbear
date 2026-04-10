"""Mistral CLI Backend using Vibe CLI.

Subprocess-based backend using ``vibe --prompt "..." --output json``.
Vibe handles the tool loop autonomously — we parse the final JSON output
for the response text and usage data.

Vibe authenticates via MISTRAL_API_KEY in ~/.vibe/.env.
MCP servers are configured through ~/.vibe/config.toml.
"""

import asyncio
import json
import os
import shutil

from config.logging_config import logger
from core.interfaces.runner import RunnerResponse


def _find_vibe_binary() -> str | None:
    """Locate the vibe binary."""
    return shutil.which("vibe")


class MistralCliBackend:
    """Mistral Vibe CLI backend via subprocess.

    Uses ``vibe --prompt "..." --output json`` which outputs a complete
    JSON response at completion. Vibe manages MCP tool execution
    internally.
    """

    def __init__(self, config: dict):
        self.config = config
        self.model = config.get(
            "model", os.getenv("MISTRAL_MODEL", "mistral-large-latest")
        )
        self.timeout = int(config.get("timeout", 120))
        self.max_retries = int(config.get("max_retries", 2))
        self.max_tool_iterations = int(config.get("max_tool_iterations", 20))
        self.max_price = float(config.get("max_price", 1.0))
        self._gateway_url = os.getenv("MCP_GATEWAY_URL", "http://localhost:8000")
        self._vibe_bin = _find_vibe_binary()

        if not self._vibe_bin:
            logger.warning(
                "Vibe CLI not found — install with 'pip install mistral-vibe' "
                "or check PATH"
            )

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
        """Execute Vibe CLI and return parsed response."""
        if not self._vibe_bin:
            msg = "Vibe CLI not installed. Install with: pip install mistral-vibe"
            if error_callback:
                await error_callback("cli_not_found", msg)
            return RunnerResponse(text=msg, is_error=True)

        effective_model = model or self.model
        agent_label = agent_id or "default"

        logger.info(
            "[%s] Vibe CLI call: model=%s, prompt_len=%d",
            agent_label,
            effective_model,
            len(prompt),
        )

        # MCP token for gateway access
        unified_id = kwargs.get("unified_id")
        if not no_tools and agent_id:
            self._prepare_mcp_token(agent_id, unified_id=unified_id)
            # Ensure Vibe TOML has MCP server config + correct model
            from .config_generator import write_config

            write_config(gateway_url=self._gateway_url, model=effective_model)

        cmd = self._build_command(prompt=prompt)
        logger.debug("Running vibe: %d args, prompt_len=%d", len(cmd), len(prompt))

        # Build env: API key + MCP token
        env = os.environ.copy()
        api_key = self._get_api_key()
        if api_key:
            env["MISTRAL_API_KEY"] = api_key
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

                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout,
                )

                stdout_text = stdout.decode(errors="replace").strip()
                stderr_text = stderr.decode(errors="replace").strip()

                if stderr_text:
                    logger.debug("[%s] Vibe stderr: %s", agent_label, stderr_text[:500])

                if process.returncode != 0:
                    error_msg = stderr_text or stdout_text or "Vibe CLI failed"
                    logger.error(
                        "[%s] Vibe CLI exit code %d: %s",
                        agent_label,
                        process.returncode,
                        error_msg[:200],
                    )
                    if error_callback:
                        await error_callback("vibe_error", error_msg)
                    return RunnerResponse(
                        text=f"Vibe error: {error_msg}",
                        session_id=session_id,
                        cost_usd=0.0,
                        is_error=True,
                        raw={},
                    )

                # Parse JSON output
                text, usage = self._parse_json_output(stdout_text, agent_label)

                # CLI uses Le Chat subscription — no per-token cost
                cost = 0.0

                logger.info(
                    "[%s] Vibe CLI done: cost=$%.2f, usage=%s",
                    agent_label,
                    cost,
                    usage or "n/a",
                )

                return RunnerResponse(
                    text=text or "",
                    session_id=session_id,
                    cost_usd=cost,
                    raw={
                        "model": effective_model,
                        "runner": "mistral-cli",
                        "usage": usage or {},
                    },
                )

            except asyncio.TimeoutError:
                # Capture stderr before killing for diagnostics
                stderr_dump = ""
                try:
                    process.kill()
                    _, stderr_raw = await asyncio.wait_for(
                        process.communicate(), timeout=5
                    )
                    stderr_dump = stderr_raw.decode(errors="replace").strip()
                except Exception:
                    try:
                        await process.wait()
                    except Exception:
                        pass
                logger.error(
                    "[%s] Vibe CLI timed out after %ds. stderr: %s",
                    agent_label,
                    self.timeout,
                    stderr_dump[:500] or "(empty)",
                )

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
                logger.exception("[%s] Vibe CLI error: %s", agent_label, e)
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

    def _build_command(self, prompt: str) -> list[str]:
        """Build Vibe CLI command.

        Uses -p TEXT (programmatic mode). Model is set via config.toml.
        Prompt is passed as a single argv element — exec-style invocation
        handles quoting correctly without shell interpolation.
        """
        cmd = [self._vibe_bin]

        cmd.extend(["--output", "json"])
        cmd.extend(["--max-turns", str(self.max_tool_iterations)])
        cmd.extend(["--max-price", f"{self.max_price:.2f}"])
        # -p must be last: argparse nargs='?' grabs the next token
        cmd.extend(["-p", prompt])

        return cmd

    @staticmethod
    def _parse_json_output(
        stdout: str,
        agent_label: str,
    ) -> tuple[str, dict | None]:
        """Parse Vibe --output json response.

        Expected format is a JSON object with at least a text/content field.
        The exact schema depends on Vibe version — we extract what we can.

        Returns (text, usage_dict).
        """
        if not stdout:
            return "", None

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # If not valid JSON, treat the entire output as plain text
            logger.warning(
                "[%s] Vibe output is not JSON, using as plain text", agent_label
            )
            return stdout, None

        # --output json returns an array of message objects
        if isinstance(data, list):
            # Extract last assistant message text
            parts = []
            usage = None
            for msg in data:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                if role == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, str) and content:
                        parts.append(content)
                    elif isinstance(content, list):
                        # Content blocks: [{"type": "text", "text": "..."}]
                        for block in content:
                            if isinstance(block, dict) and block.get("text"):
                                parts.append(block["text"])
                if msg.get("usage"):
                    usage = msg["usage"]
            return "\n".join(parts) if parts else "", usage

        # Single object response (legacy / future format)
        text = (
            data.get("text")
            or data.get("content")
            or data.get("message", {}).get("content", "")
            or data.get("response", "")
        )

        usage = data.get("usage")
        if not usage and "input_tokens" in data:
            usage = {
                "input_tokens": data.get("input_tokens", 0),
                "output_tokens": data.get("output_tokens", 0),
            }

        return text, usage

    def _prepare_mcp_token(
        self,
        agent_id: str,
        unified_id: str | None = None,
    ) -> bool:
        """Obtain MCP gateway token for Vibe CLI.

        Token is passed via GRIDBEAR_MCP_TOKEN env var at process spawn.
        Does NOT modify ~/.vibe/config.toml (user manages their own config).
        """
        from core.mcp_token_manager import get_mcp_token_manager

        tm = get_mcp_token_manager()
        if not tm:
            return False

        if unified_id:
            token = self._get_user_token(tm, agent_id, unified_id)
        else:
            token = tm.get_token(agent_id)

        if not token:
            return False

        self._current_mcp_token = token
        return True

    @staticmethod
    def _get_api_key() -> str | None:
        """Get Mistral API key from secrets manager or environment."""
        try:
            from ui.secrets_manager import secrets_manager

            key = secrets_manager.get_plain("MISTRAL_API_KEY")
            if key:
                return key
        except Exception:
            pass
        return os.getenv("MISTRAL_API_KEY")

    @staticmethod
    def _get_user_token(tm, agent_id: str, unified_id: str) -> str | None:
        """Create a short-lived token with user_identity for Vibe CLI."""
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
