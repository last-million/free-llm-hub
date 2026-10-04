<!-- evidence: answer first, no narration <- hub.log 2026-09-27 canary: llm7/minimax-m2.7 answered 'The user asks: "What is 6101 plus 1? ...' instead of the number -->
<!-- evidence: nothing but the call <- app.py _TOOL_DIALECT_MISMATCH: minimax-m2.7 sent a valid apply_patch call AND the same JSON as reply text; codex exited silently with no files -->
## any
- Start with the answer. Never restate or narrate the question ("The user asks: ...").
## tools
- When you call a tool, write nothing else in the reply: never repeat the call's JSON or arguments as text.
- One tool call per action, then wait for its result.
