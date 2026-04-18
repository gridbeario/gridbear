# Third-Party Attributions

This file lists third-party libraries and projects that GridBear uses or was inspired by.

## Direct Dependencies

### Python

| Library | License | Usage |
|---------|---------|-------|
| python-telegram-bot | LGPLv3 | Telegram channel adapter |
| discord.py | MIT | Discord channel adapter |
| sentence-transformers | Apache 2.0 | Embedding models |
| openai | MIT | Whisper transcription, DALL-E, OpenAI runner |
| anthropic | MIT | Claude API runner |
| google-genai | Apache 2.0 | Gemini runner |
| httpx | BSD | HTTP client |
| FastAPI | MIT | Admin UI, REST API, MCP Gateway |
| Jinja2 | BSD | Template engine |
| uvicorn | BSD | ASGI server |
| psycopg | LGPL | PostgreSQL driver |
| edge-tts | MIT | Edge TTS provider |
| cryptography | Apache 2.0 / BSD | Secrets encryption |
| mcp | MIT (Anthropic) | Model Context Protocol SDK |
| livekit | Apache 2.0 | LiveKit agent integration |

### Node.js (mcp-gmail-server)

| Library | License | Usage |
|---------|---------|-------|
| @modelcontextprotocol/sdk | MIT (Anthropic) | MCP server protocol |
| googleapis | Apache 2.0 | Gmail and Calendar API |

### Frontend

| Library | License | Usage |
|---------|---------|-------|
| Tailwind CSS | MIT | UI styling |
| DaisyUI | MIT | UI component library |
| Alpine.js | MIT | Lightweight JS framework |
| Drawflow | MIT | Workflow visual editor |

## Inspirations

### Cheshire Cat AI

The hook system pattern was inspired by [Cheshire Cat AI](https://cheshire-cat-ai.github.io/docs/).

However, GridBear's implementation is **entirely original code** - no code was copied.
The hook system uses a different architecture and API design.

Cheshire Cat AI is licensed under GPLv3.

### Anthropic Claude Artifacts

The single-file HTML artifact pattern — self-contained HTML/CSS/JS with
embedded data, rendered in a sandboxed iframe — is modelled after the
[Artifacts feature](https://www.anthropic.com/news/artifacts) in Anthropic's
Claude consumer UI. GridBear's implementation is entirely original code;
the pattern is architectural inspiration. Anthropic's product is
proprietary; no code was copied.

### Memory System Terminology

The memory system uses standard cognitive science terminology:

- **Episodic Memory**: Memory of events and experiences (conversations)
- **Declarative Memory**: Memory of facts and knowledge

These terms were introduced by [Endel Tulving](https://en.wikipedia.org/wiki/Endel_Tulving) in 1972
and are standard terminology in cognitive psychology and neuroscience.

References:
- Tulving, E. (1972). "Episodic and semantic memory"
- https://en.wikipedia.org/wiki/Episodic_memory
- https://en.wikipedia.org/wiki/Declarative_memory

## MCP Servers

### Home Assistant

GridBear connects to Home Assistant's native MCP API. No code from Home Assistant is included.

### Odoo

GridBear connects to an external Odoo MCP server via SSE. The server implementation is separate from this project.

### Google Sheets

GridBear uses the [mcp-google-sheets](https://github.com/xing5/mcp-google-sheets) package (MIT License, Copyright 2025 Xing Wu) as an MCP server for Google Sheets integration.

### Gmail

The Gmail MCP server (`mcp-gmail-server/`) is original code that uses:
- Google's official `googleapis` library (Apache 2.0)
- Anthropic's MCP SDK (MIT)
