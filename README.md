# ODEV - AI Upgrade Plugin

This plugin for `odev` provides a command to automatically upgrade Odoo modules using a Large Language Model (LLM).

## Installation

Install [odev](https://github.com/odoo-odev/odev/tree/main?tab=readme-ov-file#installation) if not already done. You'll
need odev version 4.0.0 or above.

This plugin depends on `odev-plugin-ai`. Enable both plugins by running:

```bash
odev plugin --enable odoo-odev/odev-plugin-ai-upgrade
```

### AI CLI Tools

This plugin leverages various AI CLI tools. You must install at least one of them:

-   **Claude Code**: `npm install -g @anthropic-ai/claude-code`
-   **Gemini CLI**: `npm install -g @google/gemini-cli`
-   **GitHub Copilot CLI**: `gh extension install github/gh-copilot`
-   **OpenCode CLI**: `curl -sL https://opencode.ai/install.sh | bash`
