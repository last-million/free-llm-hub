<!-- evidence: say it once, no "Wait," <- hub.log 2026-09-27..10-03: 8 canary junk answers from dahl/DeepSeek-V4-Flash-0731 ("2118.2118.2118...", "8106\nWait, the instruction says...") -->
<!-- evidence: stop after the answer <- hub.log: 13 stream-gate cuts (7 extra_text, 5 repetition, 1 restarted) and 3 junk-bench benchings, same model -->
<!-- evidence: native channel, no DSML text <- tool_rescue.py: DeepSeek V4 sent <|DSML|> / tool-calls-begin markup as reply text (2026-09) -->
<!-- evidence: each call once <- hub.log: 8 exact-duplicate tool calls dropped by [tool-dedupe] -->
<!-- evidence: schema field names only <- app.py _TOOL_DIALECT_MISMATCH: deepseek-v4-pro invented a different apply_patch JSON shape almost every call (6 repros, 2026-07-27) -->
## any
- Asked for only a number or a word? Write it once and stop. Never repeat the answer or add "Wait," second thoughts after it.
- Keep self-checks in your reasoning, never in the reply.
## tools
- Use the native tool-call channel only. Never write <｜DSML｜>, <｜tool▁calls▁begin｜> or a JSON call as reply text.
- Call each command once, then wait for its result. Never emit the same call twice in one turn.
- Fill arguments with exactly the field names the tool's schema lists: no wrapper keys, no extra nesting.
