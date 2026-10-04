<!-- evidence: stop when complete, no leftover markup <- answer_check.py: llm7/GLM-5.3-Flash answered, then kept generating to max_tokens ("6510</arg_value></tool_call>6510...") -->
<!-- evidence: user's language only <- answer_check.py: same model appended unrelated CJK text to an English answer; hub.log 2026-10-03 canary: dahl/GLM-5.3-Flash junk with </arg_value> and foreign-script words -->
<!-- evidence: junk counts <- hub.log 2026-09-27: llm7/GLM-5.3-Flash benched 6 h for 3 junk answers in 60 min -->
<!-- evidence: one call per command <- tool_rescue.py: three identical bash calls in one response from llm7/GLM-5.3-Flash (2026-09-27) -->
<!-- evidence: schema fields, say what failed <- app.py _TOOL_DIALECT_MISMATCH: glm-4.7 invented apply_patch JSON shapes; one run gave up with zero files after 2 fatal attempts -->
## any
- Stop as soon as the answer is complete. Never run on into another language, another question or leftover markup such as </arg_value> or </tool_call>.
- Reply only in the user's language.
## tools
- Call tools only through the native tool-call channel, one call per command. Never type <tool_call>, <arg_key> or <arg_value> tags in the reply.
- Never send the same command twice in one turn.
- Fill arguments with exactly the fields the tool's schema names. If a call fails, fix the arguments and retry, or say what failed; never stop silently.
