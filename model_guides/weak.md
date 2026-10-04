<!-- evidence: one step at a time, check, then next <- ECC (github.com/affaan-m/ecc, MIT) "fix incrementally, verify each fix", adapted -->
<!-- evidence: exact format / only the number <- hub.log 2026-09-12..10-04, 19 canary misses on "answer with only the number" -->
<!-- evidence: stop when complete, never repeat <- hub.log answer-gate cuts: 28 extra_text, 22 repetition -->
<!-- evidence: no reasoning or <|...|> tokens <- hub.log 8 tool_markup + 1 template_junk cuts; answer_check.py <|close|> case -->
<!-- evidence: copy paths exactly <- AGENTS.md "Tool-turn reliability": trimmed prompt lost the cwd 5/5 -->
<!-- evidence: one native tool call, never typed <- tool_rescue.py: 6+ typed-call dialects seen in the wild -->
<!-- evidence: never announce without calling <- craft.py ACT_RUN, observed 2026-08-08; hub.log 79 of 965 fan-outs where no model used a tool -->
<!-- evidence: read before edit, small scope, cheapest check first <- craft.py PROGRAMMING; ECC verification loop (build failure stops the loop), adapted -->
<!-- evidence: two fixes then report <- craft.py VERIFY_RUN two-retry cap -->
## any
- Do one thing at a time: finish it, check it, then start the next.
- Answer in exactly the asked format. Asked for only a number or a word? Output only that.
- Stop when the answer is complete. Never repeat a line or keep writing after it.
- Never print your reasoning, <think> blocks or tokens like <|...|>.
- Copy file paths, names and numbers exactly. Never guess a path: look it up.
## tools
- To act, emit ONE tool call in the native format. Never type a call as text, JSON or a code block.
- Never say you will do something without calling the tool in the same turn.
- Read a file before editing it. Change one file or one function per step.
- After each change run the cheapest check (syntax or build first, then tests) and read its real output.
- Say done only with passing output. Still failing after two fixes? Stop and list what fails.
## answer
- Put the answer first. No preamble, no restating the question.
