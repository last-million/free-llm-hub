<!-- evidence: answer first, no narration <- hub.log 2026-10-01..03 canary: kimi-k3 (g4f relays) answered "The user is asking a simple arithmetic question: 9050 + 1 =" instead of the number (3 WRONG, 1 junk) -->
<!-- evidence: no template tokens or punctuation runs <- answer_check.py: nvidia/kimi-k3 streamed "<|close|>!!!!..." as a codex answer (2026-09-27) -->
<!-- evidence: native channel only <- tool_rescue.py: Kimi's own <|tool_call_begin|>functions.NAME:0 markup reached clients as text -->
## any
- Start with the answer itself. Never narrate the request ("The user is asking...", "The user wants...").
- Never output chat-template tokens such as <|close|> or <|im_end|>, and never pad with runs of punctuation.
## tools
- Call tools only through the native tool-call channel. Never write <|tool_call_begin|> or functions.NAME:0 as text.
