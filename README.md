# Doubletake (doubletake)

This repository provides a secure, read-only bridge between your AI Coding Agent (e.g. Claude Code, Codex, Aider) and Google Antigravity. It enables the agent to do a "double take" (get a second opinion) using the **Doubt-Driven Development** methodology.

---

## 🤖 Instructions for AI Agents (Claude Code, Codex, Aider, etc.)

**Hello Agent!** If the user gave you a link to this repository and asked you to install this skill, please follow these exact steps:

1. **Install the CLI wrapper**: Run the following command in the user's terminal to install the `doubletake` CLI tool globally in an isolated environment.
   ```bash
   uv tool install git+https://github.com/aleksbuss/doubletake.git
   ```
   *(Fallback: If `uv` is not installed, use `pipx install git+https://github.com/aleksbuss/doubletake.git`)*

2. **Verify Installation**: Ensure that the `doubletake` command is now available in the user's PATH.

3. **Install the Skill**: 
   - Read the contents of the `SKILL.md` file located in this repository.
   - **If you are Claude Code**: Append the exact contents of `SKILL.md` into the user's global `~/.claude/CLAUDE.md` file (or the local `./CLAUDE.md` in their current project). Create the file if it does not exist.
   - **If you are Codex or another agent**: Append the contents of `SKILL.md` to whatever file or configuration system you use to store persistent custom instructions or skills for the user.

4. **Confirm**: Tell the user that the Antigravity Doubt-Driven Audit skill has been successfully installed, and you are now ready to use the `doubletake` command to double-check your code!

---

## 👤 Instructions for Humans (Manual Installation)

If you prefer to install this manually instead of asking your AI Agent to do it:

1. **Install the CLI**:
   ```bash
   uv tool install git+https://github.com/aleksbuss/doubletake.git
   ```
2. **Configure Your Agent**:
   Copy the contents of `SKILL.md` and paste it into your agent's custom instructions file (e.g. `~/.claude/CLAUDE.md` for Claude Code).

## Authentication

By default, `doubletake` uses your **Antigravity subscription** — it reuses the
OAuth session created when you sign in to the Antigravity app (or the `gemini`
CLI), stored at `~/.gemini/oauth_creds.json`. **No API key is required**, and
nothing is sent to a third-party API — requests go to Google's Code Assist
backend on your subscription.

If you have no Antigravity login, set `GEMINI_API_KEY` to use the Gemini
Developer API instead.

## Configuration

All optional, via environment variables:

```bash
export DOUBLETAKE_MODEL="gemini-3.1-pro-preview"   # default; or gemini-3-pro-preview / gemini-2.5-pro
export DOUBLETAKE_TIMEOUT="120"            # idle timeout in seconds
export DOUBLETAKE_BACKEND="gemini_api"     # force the GEMINI_API_KEY path
```

`doubletake` has **zero dependencies** (pure Python stdlib) and works on
CPython ≥ 3.9.
