# Engineering Audit — `doubletake`

**Reviewer stance:** staff/principal-level review (correctness, security, API
contract, supply chain, maintainability).
**Scope:** entire repository + its single runtime dependency
(`google-antigravity` 0.1.5) as actually installed and executed on this machine.
**Method:** static reading of the SDK source, runtime proto introspection, and
**empirical probing of the bundled Go `localharness` binary** (not assumptions).
**Date:** 2026-06.

Status legend: ✅ fixed in this change · ⚠️ open / needs a decision · 📝 recommendation.

---

## 0. Executive summary

`doubletake` is a well-scoped, well-intentioned CLI with a genuinely good security
instinct (stdin-only input, prompt-injection hardening, broken-pipe handling).
However, it shipped with one **product-defining defect** and several correctness
and security issues:

- **C-1 (Critical):** The stated goal — "use the Antigravity *subscription*, no API
  key" — is **not achievable** with the pinned SDK. The tool actually *requires* a
  Gemini API key, and the alternative paths the SDK exposes do not use the consumer
  subscription. This is the bug the maintainer already suspected, and it is deeper
  than "wired up wrong": the capability is absent from the dependency.
- **H-1 (High, security):** The agent was started with **all tools enabled**
  (including `edit_file` / `create_file` / `run_command`) while the code comment and
  README claimed "100% read-only isolation." False isolation claim + real write/exec
  surface, exposed to *untrusted* artifact text.
- **H-2 (High, correctness):** The 90 s timeout protected nothing — `Agent.chat()`
  is lazy, so `asyncio.wait_for(chat(...))` timed an instant operation while the
  actual (unbounded) generation ran untimed.
- Several **Medium/Low** issues: an invalid default model id, fragile error
  classification, version-fragile timeout handling, broken README install command,
  version drift, and zero tests.

Fixes applied are marked ✅.

---

## 0.1 RESOLUTION (shipped in v0.3.0)

The maintainer chose **Option A** (direct Code Assist). Implemented and verified
end-to-end:

- **Dropped the `google-antigravity` SDK entirely.** doubletake is now a
  **zero-dependency, pure-stdlib** CLI (`urllib`). No 99 MB Go binary, no SDK
  version coupling.
- **New `client.py`** authenticates with the user's **Antigravity subscription**
  by reusing the existing OAuth session at `~/.gemini/oauth_creds.json` (refreshed
  in-memory via `oauth2.googleapis.com`), discovers the Code Assist project via
  `loadCodeAssist`, and streams `cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse`.
- **`GEMINI_API_KEY`** remains as a fallback (direct Gemini Developer API), so the
  tool still works for users without an Antigravity login.
- **Verified:** on stock **Python 3.9.6**, with **no API key**, a real adversarial
  review streamed from the subscription (standard-tier, `gemini-2.5-pro`); also
  validated via a real `uv tool install`, broken-pipe (`| head`), and the
  no-credential error path.

This **resolves C-1** and also dissolves **H-1**: a single non-agentic
`generateContent` call has *no* tools, file access, or shell — read-only by
construction, not by configuration.

---

## C-1 ✅ RESOLVED (was Critical) — The subscription/no-key premise is unsupported by the SDK

**Claim under test:** "A user with an Antigravity subscription can run `doubletake`
without pasting any API key; it should reach Antigravity for the answer."

**Finding:** False with `google-antigravity` 0.1.5. Evidence chain:

1. `LocalAgentConfig._build_shorthand_endpoint()` *unconditionally* attaches a
   `GeminiAPIEndpoint(api_key=…)` to every model unless `vertex=True`
   (`connections/local/local_connection_config.py:116-123`). `GeminiAPIEndpoint`
   then *requires* `GEMINI_API_KEY` (`models.py:95-104`). → an API key is mandatory.
2. The only key-less SDK path is **Vertex AI + ADC** (`vertex=True` + project +
   `gcloud auth application-default login`). That bills a GCP project's Vertex usage
   and is **not** the Antigravity subscription. On this machine ADC is absent and
   `gcloud` is not installed.
3. The "let the backend choose" idea fails: an **empty model list** makes the Go
   harness abort with *"no text model configuration provided in HarnessConfig"*
   (verified by running it). A model with **no endpoint** → *"a Gemini API key is
   required."*
4. The wire protocol has a `custom_endpoint{ backend_type, config_json }` oneof, but
   the SDK's `build_models_proto` only emits `gemini_api` / `vertex` and raises on
   anything else (`local_connection.py:432-460`). I empirically probed the harness
   with **28** plausible `backend_type` values (`cloudcode`, `code_assist`,
   `managed`, `rift`, `antigravity`, `openai`, `anthropic`, …) — **all** returned
   *"unknown custom backend type"*. The CloudCode/Code-Assist gRPC client compiled
   into the binary serves user-settings/metrics/onboarding, not agent inference.
5. The real consumer-subscription credential exists at `~/.gemini/oauth_creds.json`
   (OAuth access+refresh token for `cloudcode-pa.googleapis.com`, identical to
   `gemini-cli`). Using it means **bypassing the SDK** and re-implementing a Code
   Assist client.

**Impact:** The headline value proposition does not work as designed. Worse, the
original error handling *masked* this — it mislabeled the `AntigravityValidationError`
as "GEMINI_API_KEY is not set or invalid," sending users hunting for a key instead
of revealing the architectural gap.

**Options (decision required):**

| Option | What the user does | No key string? | Uses Antigravity sub? | Cost / setup | Robustness |
|--------|--------------------|:---:|:---:|--------------|-----------|
| **A. Direct Code Assist** (bypass SDK; use `~/.gemini/oauth_creds.json` → `cloudcode-pa`) | nothing (already logged in) | ✅ | ✅ | none | ⚠️ brittle, undocumented, abandons SDK |
| **B. Vertex + ADC** (`vertex=True`) | `gcloud auth application-default login` + GCP project | ✅ | ❌ (Vertex billing) | install gcloud, enable Vertex, billing | ✅ supported by SDK |
| **C. Keep `GEMINI_API_KEY`** | paste a key | ❌ | ❌ | get a key | ✅ supported |

📝 Recommendation: Option **A** matches the literal goal with zero user setup, but
own the brittleness explicitly (isolate it behind a clear module, pin behavior,
add a fast self-test). Keep **C** as a documented fallback.

**Resolution:** Option A shipped in v0.3.0 — see §0.1. doubletake now uses the
Antigravity subscription directly (no key, no SDK), verified end-to-end.

---

## H-1 ✅ HIGH (security) — False "read-only" isolation; all tools were enabled

**Evidence:** original `cli.py` passed `capabilities=CapabilitiesConfig()` with the
comment *"Explicitly empty capabilities for 100% read-only isolation."* But
`CapabilitiesConfig()` leaves `enabled_tools=None` and `disabled_tools=None`, and
`Agent.__aenter__` (`agent.py:83-89`) then sets `active_tools = set(BuiltinTools)`
— i.e. **every** tool, including `create_file`, `edit_file`, `run_command`,
`generate_image`, `search_web`, `start_subagent`. The default `LocalAgentConfig`
policy is only `confirm_run_command()`, and the CLI registers **no** interactive
confirmation hook, with `workspaces` defaulting to `[os.getcwd()]`.

**Impact:** The reviewer model processes **untrusted artifact text**. A prompt
injection in that artifact could induce file writes within the working directory
(and the system-prompt hardening is the *only* barrier — there was no
defense-in-depth). The "secure, read-only bridge" promise (README) was untrue.

**Fix:** an interim v0.2 fix set
`CapabilitiesConfig(enabled_tools=BuiltinTools.read_only(), …)`. This was then
**superseded by v0.3**, which drops the SDK/agentic loop entirely: a single
non-agentic `generateContent` call has no tools, file access, or shell, so the
reviewer is read-only **by construction** (no `CapabilitiesConfig` exists in the
shipped code).

📝 Follow-up: consider `BuiltinTools.none()` (zero tools) since the artifact is
self-contained in the prompt; read-only file access to CWD is convenient but still
allows the model to read local files into its output.

---

## H-2 ✅ HIGH (correctness) — The timeout protected nothing

**Evidence:** original code: `response = await asyncio.wait_for(agent.chat(prompt),
timeout=90.0)` then `async for token in response`. But `Conversation.chat()`
(`conversation/conversation.py:227-243`) `await`s `send()` and **returns a lazy
`ChatResponse` immediately**; the model runs only while iterating. So `wait_for`
bounded an ~instant call, and the real generation loop had **no timeout**. A hung
upstream would hang the CLI forever.

**Fix applied (v0.3.0):** the SDK/async path is gone. The stdlib client streams
SSE synchronously with a **per-read socket idle timeout** (`urlopen(timeout=…)`),
configurable via `DOUBLETAKE_TIMEOUT` (default 120 s). This bounds *inactivity*
while allowing long but progressing reviews — the correct semantics for a
streaming CLI.

---

## M-1 ✅ MEDIUM — Invalid / unverifiable default model `gemini-3.1-pro`

`README.md` and the old `cli.py`/`agy_audit` default to `gemini-3.1-pro`. The
harness's recognized Gemini-3 ids (from the binary) are `gemini-3-pro-preview` /
`gemini-3-flash-preview`; the SDK's own `DEFAULT_MODEL` is `gemini-3.5-flash`.
Bare `gemini-3.1-pro` / `gemini-3-pro` both **404** on Code Assist. **Fix applied
(v0.3.0):** default is now **`gemini-3.1-pro-preview`** (the latest Pro; the valid
wire id, empirically verified), overridable via `DOUBLETAKE_MODEL`
(`gemini-3-pro-preview` / `gemini-2.5-pro` are good lower-rate-limit fallbacks).

## M-2 ✅ MEDIUM — Python 3.9 compatibility

Two 3.9 hazards: (a) the old `except TimeoutError` would miss `asyncio.wait_for`'s
`asyncio.TimeoutError` on 3.9/3.10; (b) PEP 604 `X | None` annotations are
evaluated at def-time on 3.9 and raise. **Fix applied (v0.3.0):** asyncio removed
entirely (no more `TimeoutError` ambiguity), and `client.py` uses
`from __future__ import annotations`. Verified running on CPython 3.9.6.

## M-3 ⚠️ MEDIUM — Fragile substring-based error classification

Error handling keys off lowercased-substring matches (`"429"`, `"api key"`,
`"validationerror"`, previously `"harness"`). `"429"` and `"400"` match random
content; `"harness"` matched benign harness messages and mislabeled them. **Partly
fixed** (removed the broad `"harness"` match; tightened to "could not find default
localharness"). 📝 Prefer typed SDK exceptions (`AntigravityConnectionError`,
`AntigravityValidationError`, `AntigravityExecutionError` exist) over string
sniffing.

## M-4 ✅ MEDIUM — README install command is broken & claims are inaccurate

- `README.md` instructs `uv tool install git+https://github.com/YOUR_GITHUB_USERNAME/doubletake.git`
  — the placeholder is unedited; copy-paste fails. Replace with `aleksbuss`.
- Configuration section says it "uses `gemini-3.1-pro` (via the Antigravity SDK)"
  and implies no key — both inaccurate (see C-1, M-1). Rewrite to state the
  `GEMINI_API_KEY` requirement (or the chosen no-key path).

## M-5 📝 MEDIUM — Supply-chain / footprint

Installing this CLI pulls a **99 MB Go binary** (`localharness`, Mach-O) plus
`google-genai`, `google-auth`, `cryptography`, `websockets`, etc. The binary
executes locally and opens a localhost WebSocket. This is legitimate (it *is* the
Antigravity engine), but worth documenting: large download, native code, network
egress to Google. Pin the SDK (done: `<0.2`) and consider verifying the wheel hash
in CI if one is added.

---

## L-1 ✅ LOW — `logging.getLogger().setLevel(logging.CRITICAL)` at import time

The original wrapper mutated the **root** logger as an import side effect to
silence SDK noise. **Resolved in v0.3:** the SDK is gone, so the call was removed
entirely; there is no logging side effect in the shipped code.

## L-2 ✅ LOW — Version drift

`__init__.__version__` was `0.1.0` while there was no single source of truth.
Bumped package + `__init__` + `cli.VERSION` to **0.3.0** in lockstep. 📝 Long term,
derive `VERSION` from `importlib.metadata.version("doubletake")` to avoid triple
maintenance.

## L-3 📝 LOW — No tests, no CI

Zero automated tests. At minimum add: (1) a unit test that `_build_config()` raises
`MissingCredentialsError` without a key and produces read-only capabilities; (2) a
smoke test that `-h`/`-v`/empty-stdin/binary-stdin behave; (3) if Option A is
chosen, a token-refresh unit test. A tiny GitHub Action (lint + these tests) would
catch regressions like H-1/H-2.

## L-4 📝 LOW — `SKILL.md` / `README.md` drift & duplication

The methodology and the "how to install" text live in three places (README,
SKILL.md, and now CLAUDE.md). Keep SKILL.md as the single source for the agent-facing
skill; have README link to it rather than restating.

---

## What is already good (keep it)

- **stdin-only contract** — structurally immune to argv/shell injection; the code
  even refuses a TTY and rejects binary input. Excellent.
- **Prompt-injection hardening** in the system prompt (treat artifact as untrusted,
  don't obey embedded instructions). Now backed by read-only capabilities.
- **Broken-pipe handling** (`| head`-friendly) and meaningful exit codes.
- **Clear separation** stdout=result / stderr=diagnostics — correct for composability.
- **The methodology itself** (`SKILL.md`) is sound: adversarial framing, fresh
  context, cross-model escalation, explicit stop conditions.

---

## Prioritized action list

1. **Decide C-1** (Option A / B / C). Implement + add a self-test that proves a
   review runs without pasting a key (if A/B).
2. Land H-1 and H-2 (done ✅) — these are real security/correctness fixes.
3. Fix README (M-4) and default model (M-1, done in code).
4. Add minimal tests + CI (L-3).
5. Replace string-based error sniffing with typed exceptions (M-3).

---

## Cross-model second opinion (dogfooded via `doubletake`, gemini-3.1-pro-preview)

doubletake was run on its own code + this audit (Doubt-Driven Development in
practice). The reviewer found real issues — triaged honestly:

**Accepted & fixed (v0.3.0):**
- **N-1 `TypeError` on null `expiry_date`** — `creds.get("expiry_date", 0)` returns
  `None` when the key is present-but-null → arithmetic crash. Fixed:
  `creds.get("expiry_date") or 0`.
- **N-2 `AttributeError` on `"response": null`** — `obj.get("response", obj)`
  returns `None` (key present), then `.get("candidates")` crashes. Fixed:
  `(obj.get("response") or {})` + null-safe candidate access.
- **N-3 uncaught `socket.timeout`** in `_access_token`/`_discover_project`/stream
  setup (only `URLError` was caught). Fixed: catch `OSError` (covers
  HTTPError/URLError/socket.timeout).
- **Audit staleness** — it correctly flagged that H-1/L-1/L-2 still described the
  superseded v0.2 (SDK) state and a wrong version (0.2.0). Corrected above.
- **Silent bad `DOUBLETAKE_TIMEOUT`** now warns on stderr instead of silently
  reverting.

**Noted, intentionally not changed:**
- *Refresh token not persisted* — by design (avoid clobbering the shared
  `oauth_creds.json` / racing the Antigravity app). We only self-refresh when the
  app's cached token is already stale; standard Google installed-app refresh tokens
  are not rotated per-refresh. Low risk.
- *SSE multi-line buffering* — `cloudcode-pa` `alt=sse` emits one compact JSON per
  `data:` line (verified); a full SSE buffer is unnecessary for this server.
- *Structural prompt-injection isolation* — inherent to any single-call LLM review;
  mitigated by the hardened system instruction. Residual risk, documented.
- *Severity calibration (C-1 "Critical", H-2 "High")* — reasonable to debate; kept,
  since C-1 nullified the product's core promise and H-2 could hang the CLI.

**Round 2 — review of the post-fix code (dogfooded again, gemini-3.1-pro-preview).**
All four findings accepted & fixed:
- **N-4** `BrokenPipeError` handler redirected *stderr* to devnull, but the broken
  fd is *stdout* (canonical recipe misapplied). Fixed: `dup2(devnull, stdout)`.
- **N-5** streaming non-ASCII text could raise `UnicodeEncodeError` under a
  non-UTF-8 locale. Fixed: `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`.
- **N-6** mid-stream connection drops (`ConnectionResetError`,
  `http.client.IncompleteRead`) were uncaught → raw traceback. Fixed: caught and
  mapped to `BackendError`.
- **N-7** the SSE parser dropped spec-valid multi-line `data:` payloads. Fixed:
  accumulate `data:` lines and dispatch on the blank-line boundary (flush at EOF).
Verified by offline unit checks (multi-line/null/thought/comment/EOF) + a live
streaming call.
