# Doubt-Driven Development with Doubletake

**Instructions for AI Agent**: Save this text as a skill or add it to your system prompt instructions.

---

A confident answer is not a correct one. Long sessions accumulate context that quietly turns assumptions into "facts" without anyone noticing. Doubt-driven development is the discipline of materializing a fresh-context reviewer — biased to **disprove**, not approve — before any non-trivial output stands.

## The Process

Doubt cycle:
- [ ] Step 1: CLAIM — wrote the claim + why-it-matters
- [ ] Step 2: EXTRACT — isolated artifact + contract, stripped reasoning
- [ ] Step 3: DOUBT — invoked fresh-context reviewer with adversarial prompt
- [ ] Step 4: RECONCILE — classified every finding against the artifact text
- [ ] Step 5: STOP — met stop condition (trivial findings, 3 cycles, or user override)

### Step 3: DOUBT — Invoke the fresh-context reviewer

The reviewer's prompt **must be adversarial**. Framing decides the answer.

```
Adversarial review. Find what is wrong with this artifact.
Assume the author is overconfident. Look for:
- Unstated assumptions
- Edge cases not handled
- Hidden coupling or shared state
- Ways the contract could be violated
- Existing conventions this might break
- Failure modes under unexpected input

Do NOT validate. Do NOT summarize. Find issues, or state
explicitly that you cannot find any after thorough examination.

ARTIFACT: <paste artifact>
CONTRACT: <paste contract>
```

#### Cross-model escalation — the core rule

A single-model reviewer shares blind spots with the original author. The reviewer **must come from a different model family** — different training data, different architecture. Same model reviewing itself is useless.

| You are running in | Reviewer to use |
|--------------------|-----------------|
| Claude Code (claude.ai, claude CLI) | Antigravity / Gemini (default) |
| Antigravity (gemini CLI, Code Assist) | Claude Code (`DOUBLETAKE_BACKEND=claude`) |

**To perform this review, you MUST use the `doubletake` CLI tool.**

1. Write the adversarial prompt + ARTIFACT + CONTRACT to a temp file first (e.g. `/tmp/doubt-prompt.md`).
2. **Never interpolate the artifact into a shell-quoted argument.**
3. Pipe via stdin so shell metacharacters in the artifact stay inert.

```bash
# If you are Claude Code (default — uses Antigravity/Gemini as reviewer):
doubletake < /tmp/doubt-prompt.md

# If you are an Antigravity agent (uses Claude Code as reviewer):
DOUBLETAKE_BACKEND=claude doubletake < /tmp/doubt-prompt.md
```

`doubletake` automatically tries fallback models if the primary model is rate-limited — no manual intervention needed.

If `doubletake` is missing or fails, surface the failure explicitly. Do not silently fall back to single-model — the user should know cross-model didn't happen.
