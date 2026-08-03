# ODEV - AI Upgrade Plugin

This plugin for `odev` provides a command to automatically upgrade Odoo modules using a Large Language Model (LLM).

## Installation

Install [odev](https://github.com/odoo-odev/odev/tree/main?tab=readme-ov-file#installation) if not already done. You'll
need odev version 4.0.0 or above.

This plugin depends on `odev-plugin-ai`. Enable both plugins by running:

```bash
odev plugin --enable odoo-odev/odev-plugin-ai-upgrade
```

## Usage

```bash
odev upgrade <database> --to 19.0 --task-id 12345
```

Before handing anything to the AI, the command establishes a **behaviour baseline**: it creates a
`<db>_<from_ver>_baseline` database on the source version, installs every module to upgrade together
as in production, runs their tests, and reports what it found in the prompt. That is what makes
preservation verifiable — a baseline test that later passes on the target version *without being
modified* is evidence that behaviour was preserved, while a test written after the upgrade only pins
the post-upgrade result.

The baseline aborts the upgrade if the modules do not install on their own source version, and it
reports suites that were already red, and suites that declare tests which did not run. Skip it with
`--no-baseline` only when you already have one committed: without it the upgrade can prove that the
modules install, but not that their behaviour is unchanged.

### AI CLI Tools

This plugin leverages various AI CLI tools. You must install at least one of them:

-   **Claude Code**: `npm install -g @anthropic-ai/claude-code`
-   **Gemini CLI**: `npm install -g @google/gemini-cli`
-   **GitHub Copilot CLI**: `gh extension install github/gh-copilot`
-   **OpenCode CLI**: `curl -sL https://opencode.ai/install.sh | bash`
