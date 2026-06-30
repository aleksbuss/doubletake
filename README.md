# doubletake

A read-only second-opinion CLI for AI-agent **Doubt-Driven Development**.

Your AI agent writes an adversarial review prompt (artifact + contract) to a file,
pipes it into `doubletake`, and gets back a ruthless critique from Google Gemini —
a different model than the author, so it does not share the author's blind spots.

```bash
doubletake < /tmp/doubt-prompt.md
```

`doubletake` has **zero Python runtime dependencies** (pure stdlib) and works on
CPython ≥ 3.9.

---

## Prerequisites

Before installing, you need **one** of:

- **Antigravity subscription** (recommended) — sign in via the
  [Antigravity app](https://antigravity.dev) or the `gemini` CLI
  (`gemini auth login`). This creates `~/.gemini/oauth_creds.json` which
  `doubletake` reuses. No API key required.
- **Gemini API key** — set `GEMINI_API_KEY` in your environment as a fallback
  when no Antigravity login exists.

---

## 🤖 Instructions for AI Agents (Claude Code, Codex, Aider, etc.)

**Hello Agent!** If the user gave you a link to this repository and asked you to
install this skill, follow these steps exactly:

1. **Install the CLI**:
   ```bash
   uv tool install git+https://github.com/aleksbuss/doubletake.git
   ```
   *(Fallback: `pipx install git+https://github.com/aleksbuss/doubletake.git`)*

2. **Verify installation**:
   ```bash
   doubletake --version
   ```
   If the command is not found, the `uv`/`pipx` bin directory may not be in
   `PATH`. Ask the user to run `uv tool update-shell` (or add the printed path
   manually).

3. **Install the Skill** — `SKILL.md` teaches you *when and how* to invoke
   `doubletake` as part of your development loop (the Doubt-Driven Development
   methodology). Without it, you have the binary but not the workflow.
   - **Claude Code**: append the exact contents of `SKILL.md` to
     `~/.claude/CLAUDE.md` (or the local `./CLAUDE.md`). Create the file if it
     does not exist.
   - **Other agents**: append `SKILL.md` to your persistent custom-instructions
     file.

4. **Confirm**: Tell the user the skill is installed and that you will now use
   `doubletake` to cross-model-review significant outputs before finalising them.

---

## 👤 Instructions for Humans (Manual Installation)

1. **Install the CLI**:
   ```bash
   uv tool install git+https://github.com/aleksbuss/doubletake.git
   ```

2. **Verify**:
   ```bash
   doubletake --version
   ```

3. **Configure your agent** — copy `SKILL.md` into your agent's custom
   instructions (e.g. `~/.claude/CLAUDE.md` for Claude Code).

---

## Usage

Pipe any text prompt to stdin:

```bash
cat > /tmp/doubt-prompt.md << 'EOF'
Adversarial review. Find what is wrong with this artifact.
Do NOT validate. Do NOT summarize.

ARTIFACT:
def divide(a, b):
    return a / b

CONTRACT: Must handle division by zero without raising.
EOF

doubletake < /tmp/doubt-prompt.md
```

Output streams directly to stdout. Diagnostics go to stderr.

---

## Authentication

`doubletake` picks the best available backend automatically:

| Condition | Backend used |
|-----------|-------------|
| `~/.gemini/oauth_creds.json` exists and valid | Antigravity subscription (Code Assist) — **no API key** |
| Login exists but **tokens expired** | `AuthError` → run `gemini auth login` to fix |
| No login, `GEMINI_API_KEY` set | Gemini Developer API |
| `DOUBLETAKE_BACKEND=gemini_api` | Force API-key path regardless of login |

The OAuth session is read in-memory only; `oauth_creds.json` is never rewritten.

---

## Configuration

All optional, via environment variables:

```bash
export DOUBLETAKE_MODEL="gemini-3.1-pro-preview"  # default (also: gemini-3-pro-preview, gemini-2.5-pro)
export DOUBLETAKE_TIMEOUT="120"                    # idle timeout in seconds (default: 120)
export DOUBLETAKE_BACKEND="gemini_api"             # force GEMINI_API_KEY path
export DOUBLETAKE_OAUTH_CREDS="~/.gemini/oauth_creds.json"  # override creds path
```
