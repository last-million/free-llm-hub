"""verify.py: independent verifier (different family) + corrector, pure half."""
import json

import pytest

import verify


# --------------------------------------------------------------------------- #
# family()
# --------------------------------------------------------------------------- #

SPELLINGS = [
    # kimi
    ("moonshotai/kimi-k3", "kimi"),
    ("morph-kimik3", "kimi"),
    ("@cf/moonshotai/kimi-k2.7", "kimi"),
    ("AnyProvider:kimi-k3", "kimi"),
    ("nvidia:moonshotai/kimi-k3", "kimi"),
    ("KIMI-K3", "kimi"),
    ("moonshot-v1-128k", "kimi"),
    ("moonshotai/moonlight-16b-a3b-instruct", "kimi"),      # namespace only
    # claude
    ("srv_x:anthropic/claude-sonnet-4", "claude"),
    ("GithubCopilot:claude-sonnet-5.5", "claude"),
    ("Airforce:claude-opus-5", "claude"),
    ("claude41opusthinking", "claude"),
    ("gemini-claude-opus-4-6-thinking", "claude"),          # g4f spelling of a Claude model
    # gemini / gemma
    ("models/gemini-3.5-flash", "gemini"),
    ("google/gemini-3-flash-preview:free", "gemini"),
    ("gemini-flash-latest", "gemini"),
    ("google/gemma-3-27b-it:free", "gemma"),
    ("srv_x:gemma4:31b", "gemma"),
    # qwen
    ("qwen/qwen3.8-27b:free", "qwen"),
    ("turboderp/Qwen3.8-27B-exl3", "qwen"),
    ("Qwen/QwQ-32B", "qwen"),
    ("srv_a:qwen3:8b", "qwen"),
    # glm
    ("zai-org/GLM-5.3-Flash", "glm"),
    ("z-ai/glm-5.3-free", "glm"),
    ("morph-glm53flash", "glm"),
    ("morph-glm53-744b", "glm"),
    ("zai-org-glm-5-3-flash", "glm"),
    ("G4FSpace:srv_x:z-ai/glm-5.3", "glm"),
    ("THUDM/chatglm3-6b", "glm"),
    # deepseek
    ("deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek"),
    ("morph-dsv4flash", "deepseek"),
    ("deepseek/deepseek-r1-distill-llama-70b:free", "deepseek"),
    # llama / nemotron
    ("@cf/meta/llama-4-scout-17b-16e-instruct", "llama"),
    ("meta-llama/Llama-3.3-70B-Instruct", "llama"),
    ("groq:llama3.3:70b", "llama"),
    ("nvidia/llama-3.3-nemotron-super-49b-v1.5", "nemotron"),
    ("nvidia/nemotron-3-ultra-550b-a55b:free", "nemotron"),
    # gpt / gpt-oss
    ("openai/gpt-oss-120b", "gpt-oss"),
    ("@cf/openai/gpt-oss-20b", "gpt-oss"),
    ("srv_msg68ooo:gpt-oss:latest", "gpt-oss"),
    ("openai-fast", "gpt-oss"),
    ("openai/gpt-5.2", "gpt"),
    ("gpt-4o-mini", "gpt"),
    ("openai/o4-mini", "gpt"),
    ("o3", "gpt"),
    ("chatgpt-4o-latest", "gpt"),
    ("openai", "gpt"),
    # mistral
    ("mistralai/mistral-small-3.2-24b-instruct:free", "mistral"),
    ("srv_mqjxnj9i:codestral:latest", "mistral"),
    ("devstral-medium-2507", "mistral"),
    ("mixtral-8x7b-32768", "mistral"),
    ("magistral-medium-latest", "mistral"),
    # the rest
    ("MiniMaxAI/MiniMax-M2.7", "minimax"),
    ("minimax/minimax-m3", "minimax"),
    ("xiaomi/mimo-v2.6-pro:free", "mimo"),
    ("x-ai/grok-4-fast:free", "grok"),
    ("grok-code-fast-1", "grok"),
    ("cohere/command-a-03-2025", "cohere"),
    ("CohereLabs/c4ai-command-r-plus", "cohere"),
    ("command-r7b-12-2024", "cohere"),
    ("microsoft/phi-4-reasoning-plus", "phi"),
    ("phi4:14b", "phi"),
    # unknown
    ("stealth/space-bunny-alpha", "unknown:space-bunny-alpha"),
    ("openrouter:stealth/space-bunny-alpha:free", "unknown:space-bunny-alpha"),
    ("auto", "unknown:auto"),
    ("pa:657cce02:auto", "unknown:auto"),
    ("cognitivecomputations/dolphin-2.9", "unknown:dolphin-2.9"),
]


@pytest.mark.parametrize("model_id,expected", SPELLINGS)
def test_family_of_every_spelling(model_id, expected):
    assert verify.family(model_id) == expected


def test_enough_real_spellings_are_covered():
    assert len(SPELLINGS) >= 30
    named = {f for _, f in SPELLINGS if not f.startswith("unknown:")}
    assert named == {"kimi", "glm", "qwen", "deepseek", "gemini", "gemma", "llama", "mistral",
                     "claude", "gpt", "gpt-oss", "minimax", "mimo", "nemotron", "grok",
                     "cohere", "phi"}


def test_family_tolerates_junk_input():
    assert verify.family(None) == "unknown:"
    assert verify.family("") == "unknown:"
    assert verify.family(42) == "unknown:"


def test_two_unknown_models_are_two_families():
    assert verify.family("stealth/space-bunny-alpha") != verify.family("stealth/pixel-canary")


def test_identity_matches_the_hub_normalizer():
    """verify.identity mirrors app._normalize_model_identity: one model is one
    identity in routing and here."""
    import app
    for model_id, _ in SPELLINGS:
        assert verify.identity(model_id) == app._normalize_model_identity(model_id), model_id


# --------------------------------------------------------------------------- #
# pick_verifier()
# --------------------------------------------------------------------------- #

def test_pick_prefers_other_family_on_another_provider():
    producer = ("groq", "moonshotai/kimi-k3")
    cands = [
        ("groq", "moonshotai/kimi-k3", 140),           # the producer itself
        ("nvidia", "moonshotai/kimi-k3", 139),         # same weights elsewhere
        ("groq", "qwen/qwen3.8-27b", 135),             # other family, same provider
        ("cerebras", "zai-org/GLM-5.3-Flash", 133),    # other family, other provider
        ("google", "gemini-3-flash", 120),
    ]
    assert verify.pick_verifier(producer, cands) == ("cerebras", "zai-org/GLM-5.3-Flash")


def test_pick_highest_score_among_equals():
    producer = ("groq", "kimi-k3")
    cands = [("google", "gemini-3-flash", 120), ("cerebras", "glm-5.3", 133),
             ("openrouter", "deepseek-v4", 131)]
    assert verify.pick_verifier(producer, cands) == ("cerebras", "glm-5.3")


def test_pick_other_family_on_same_provider_beats_same_family():
    producer = ("groq", "moonshotai/kimi-k3")
    cands = [("nvidia", "kimi-k2.7", 150), ("groq", "llama-3.3-70b", 60)]
    assert verify.pick_verifier(producer, cands) == ("groq", "llama-3.3-70b")


def test_pick_falls_back_to_same_family_preferring_another_model():
    producer = ("groq", "moonshotai/kimi-k3")
    cands = [("nvidia", "moonshotai/kimi-k3:free", 150), ("groq", "kimi-k2.7", 120)]
    assert verify.pick_verifier(producer, cands) == ("groq", "kimi-k2.7")
    # only the same weights on another host left: still better than nothing
    assert verify.pick_verifier(producer, [("nvidia", "kimi-k3", 1)]) == ("nvidia", "kimi-k3")


def test_pick_none_when_empty_or_only_the_producer():
    assert verify.pick_verifier(("groq", "kimi-k3"), []) is None
    assert verify.pick_verifier(("groq", "kimi-k3"), None) is None
    assert verify.pick_verifier(("groq", "kimi-k3"), [("groq", "moonshotai/kimi-k3:free", 9)]) is None


def test_pick_tolerates_bad_rows_and_scores():
    producer = ("groq", "kimi-k3")
    cands = [None, ("x",), ("a", "glm-5.3", "n/a"), ("b", "qwen3.8", float("nan")),
             ("c", "deepseek-v4", 5)]
    assert verify.pick_verifier(producer, cands) == ("c", "deepseek-v4")


# --------------------------------------------------------------------------- #
# is_risky()
# --------------------------------------------------------------------------- #

def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


RISKY_CALLS = [
    _call("Write", {"file_path": "a.py", "content": "x = 1"}),
    _call("Edit", {"file_path": "a.py", "old_string": "a", "new_string": "b"}),
    _call("MultiEdit", {"file_path": "a.py", "edits": [{"old_string": "a", "new_string": "b"}]}),
    _call("edit", {"filePath": "a.py", "oldString": "a", "newString": "b"}),       # opencode
    _call("apply_patch", {"input": "*** Begin Patch\n*** Update File: a.py\n@@\n-a\n+b\n*** End Patch"}),
    _call("Bash", {"command": "rm -rf build"}),
    _call("Bash", {"command": "pytest -q"}),
    _call("Bash", {"command": "echo hi > out.txt"}),
    _call("Bash", {"command": "git commit -am wip"}),
    _call("Bash", {"command": "git branch feature"}),
    _call("Bash", {"command": "find . -name '*.pyc' -delete"}),
    _call("Bash", {"command": "sed -i 's/a/b/' x.py"}),
    _call("Bash", {"command": "ls $(rm -rf x)"}),
    _call("shell", {"command": ["bash", "-lc", "python setup.py install"]}),          # codex
    _call("exec_command", {"cmd": "npm install"}),
    _call("run_terminal_cmd", {"command": "make"}),
    _call("delete_file", {"target_file": "a.py"}),
    _call("str_replace_based_edit_tool", {"command": "str_replace", "path": "a.py",
                                          "old_str": "a", "new_str": "b"}),
    _call("NotebookEdit", {"notebook_path": "n.ipynb", "new_source": "print(1)"}),
    _call("mcp__fs__write_file", {"path": "a.txt", "content": "x"}),
    _call("tool", {"path": "a.py", "content": "x"}),                       # generic name, write shape
    _call("files", {"action": "delete", "path": "a.py"}),                  # generic name, delete action
    {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "a", "content": "b"}},
    {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch\n*** Add File: x\n+1"},
    {"type": "local_shell_call", "action": {"type": "exec", "command": ["bash", "-lc", "rm x"]}},
]

SAFE_CALLS = [
    _call("Read", {"file_path": "a.py"}),
    _call("Grep", {"pattern": "def x", "path": "."}),
    _call("Glob", {"pattern": "**/*.py"}),
    _call("LS", {"path": "."}),
    _call("list_dir", {"relative_workspace_path": "."}),
    _call("read_file", {"path": "a.py"}),
    _call("WebFetch", {"url": "https://x", "prompt": "summarise"}),
    _call("TodoWrite", {"todos": [{"content": "do x", "status": "pending"}]}),
    _call("update_plan", {"plan": [{"step": "x", "status": "pending"}]}),
    _call("BashOutput", {"bash_id": "1"}),
    _call("Bash", {"command": "ls -la"}),
    _call("Bash", {"command": "git status && git diff HEAD~1 | head -50"}),
    _call("Bash", {"command": "cat a.py 2>&1 | grep foo"}),
    _call("Bash", {"command": "rg -n 'def ' src > /dev/null"}),
    _call("Bash", {"command": "git branch -a"}),
    _call("shell", {"command": ["bash", "-lc", "sed -n '1,40p' app.py"]}),
    _call("exec_command", {"cmd": "Get-Content app.py | Select-Object -First 5"}),
    _call("str_replace_based_edit_tool", {"command": "view", "path": "a.py"}),
]


@pytest.mark.parametrize("call", RISKY_CALLS)
def test_writing_or_running_calls_are_risky_on_hard_turns(call):
    assert verify.is_risky([call], "hard") is True


@pytest.mark.parametrize("call", SAFE_CALLS)
def test_read_only_calls_are_not_risky(call):
    assert verify.is_risky([call], "hard") is False


@pytest.mark.parametrize("difficulty", ["simple", "medium", None])
def test_risky_tool_calls_only_count_on_hard_turns(difficulty):
    assert verify.is_risky([RISKY_CALLS[0]], difficulty) is False


def test_mixed_calls_are_risky_when_any_one_writes():
    assert verify.is_risky([SAFE_CALLS[0], RISKY_CALLS[1]], "hard") is True


@pytest.mark.parametrize("text", [
    "All tests pass now.",
    "Done. 12 passed in 0.4s",
    "I've fixed the bug in parser.py.",
    "The issue has been resolved.",
    "Everything works now.",
    "Done!",
    "Tous les tests passent.",
])
def test_unverified_done_claims_are_risky(text):
    assert verify.is_risky(text, "medium") is True
    assert verify.is_risky({"role": "assistant", "content": text}, "hard") is True
    assert verify.is_risky(text, "hard", observed_pass=False) is True


@pytest.mark.parametrize("text", [
    "All tests pass now.",
    "I've fixed the bug in parser.py.",
])
def test_done_claims_are_fine_when_the_hub_saw_them_pass(text):
    assert verify.is_risky(text, "hard", observed_pass=True) is False


@pytest.mark.parametrize("text", [
    "Let me make sure the tests pass.",
    "I'll keep going until all tests pass.",
    "Not all tests pass yet: 3 failed.",
    "12 passed, 3 failed.",
    "Here is the summary of the file you asked about.",
    "",
])
def test_plans_and_honest_reports_are_not_claims(text):
    assert verify.is_risky(text, "hard") is False


def test_simple_turns_are_never_verified_for_claims():
    assert verify.is_risky("Done.", "simple") is False


def test_message_with_tool_calls_and_claim_text():
    msg = {"role": "assistant", "content": "Now let me read the file.", "tool_calls": [SAFE_CALLS[0]]}
    assert verify.is_risky(msg, "hard") is False
    msg = {"role": "assistant", "content": None, "tool_calls": [RISKY_CALLS[0]]}
    assert verify.is_risky(msg, "hard") is True
    assert verify.is_risky(None, "hard") is False


# --------------------------------------------------------------------------- #
# digest()
# --------------------------------------------------------------------------- #

def _conversation(tool_output="ok"):
    return [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Fix the off-by-one in counter.py"},
        {"role": "assistant", "content": None,
         "tool_calls": [_call("Read", {"file_path": "counter.py"}, "r1")]},
        {"role": "tool", "tool_call_id": "r1", "content": "OLD-RESULT-ONE"},
        {"role": "assistant", "content": None,
         "tool_calls": [_call("Bash", {"command": "pytest -q"}, "r2")]},
        {"role": "tool", "tool_call_id": "r2", "content": "RESULT-TWO " + tool_output},
        {"role": "assistant", "content": None,
         "tool_calls": [_call("Grep", {"pattern": "x"}, "r3")]},
        {"role": "tool", "tool_call_id": "r3", "content": "RESULT-THREE"},
        {"role": "user", "content": "<system-reminder>Todo list is empty. Ignore.</system-reminder>"},
    ]


def _total(msgs):
    return sum(len(m["content"]) for m in msgs)


def test_digest_shape_and_the_real_instruction():
    proposed = {"role": "assistant", "content": "Applying the fix.",
                "tool_calls": [_call("Edit", {"file_path": "counter.py", "old_string": "i <= n",
                                              "new_string": "i < n"})]}
    msgs = verify.digest(_conversation(), proposed)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert '"ok"' in msgs[0]["content"] and '"severity"' in msgs[0]["content"]
    user = msgs[1]["content"]
    assert "Fix the off-by-one in counter.py" in user
    assert "Todo list is empty" not in user           # reminder block is not the instruction
    # last two tool results only, oldest first, named after their call
    assert "OLD-RESULT-ONE" not in user
    assert user.index("RESULT-TWO") < user.index("RESULT-THREE")
    assert "Bash" in user and "Grep" in user
    # the proposal: text and the tool call with its arguments
    assert "Applying the fix." in user
    assert "TOOL CALL 1: Edit" in user and "i < n" in user
    assert _total(msgs) <= verify.DIGEST_MAX_CHARS


def test_digest_reminder_inside_the_instruction_message_is_removed():
    convo = [{"role": "user",
              "content": "<system-reminder>CLAUDE.md says hi</system-reminder>\nRename foo to bar"}]
    user = verify.digest(convo, "ok")[1]["content"]
    assert "Rename foo to bar" in user and "CLAUDE.md says hi" not in user
    assert "RECENT TOOL RESULTS: none" in user


def test_digest_stays_within_budget_and_keeps_head_and_tail():
    huge = "HEAD-MARK " + "x" * 200000 + " TAIL-MARK"
    convo = _conversation(tool_output=huge)
    convo[1]["content"] = "INSTR-START " + "y" * 100000 + " INSTR-END"
    proposed = {"role": "assistant", "content": "P-START " + "z" * 100000 + " P-END",
                "tool_calls": [_call("Write", {"file_path": "a", "content": "w" * 50000})]}
    msgs = verify.digest(convo, proposed)
    assert _total(msgs) <= verify.DIGEST_MAX_CHARS
    user = msgs[1]["content"]
    for mark in ("HEAD-MARK", "TAIL-MARK", "INSTR-START", "INSTR-END", "P-START",
                 "RESULT-THREE"):
        assert mark in user, mark
    assert "chars omitted" in user


def test_digest_of_a_plain_text_proposal_and_no_messages():
    msgs = verify.digest([], "The answer is 42.")
    assert "The answer is 42." in msgs[1]["content"]
    assert _total(msgs) <= verify.DIGEST_MAX_CHARS


# --------------------------------------------------------------------------- #
# parse_verdict()
# --------------------------------------------------------------------------- #

def test_parse_plain_json():
    v = verify.parse_verdict('{"ok": false, "problems": ["drops the import"], "severity": "high"}')
    assert v == {"ok": False, "problems": ["drops the import"], "severity": "high"}


def test_parse_fenced_json():
    v = verify.parse_verdict('```json\n{"ok": true, "problems": [], "severity": "low"}\n```')
    assert v == {"ok": True, "problems": [], "severity": "low"}


def test_parse_prose_around_json_and_reasoning():
    text = ("<think>hmm {not json}</think>Here is my verdict:\n"
            '{"ok": false, "problems": ["claims tests pass, none ran"], "severity": "HIGH"}\nThanks!')
    v = verify.parse_verdict(text)
    assert v["ok"] is False and v["severity"] == "high"
    assert v["problems"] == ["claims tests pass, none ran"]


def test_parse_lenient_json():
    v = verify.parse_verdict("{'ok': False, 'problems': ['a', 'b',], 'severity': 'low',}")
    assert v == {"ok": False, "problems": ["a", "b"], "severity": "low"}


def test_parse_missing_fields_default():
    assert verify.parse_verdict('{"ok": false}') == {"ok": False, "problems": [], "severity": "low"}
    v = verify.parse_verdict('{"problems": ["wrong file edited"]}')
    assert v == {"ok": False, "problems": ["wrong file edited"], "severity": "low"}
    assert verify.parse_verdict('{"ok": true}')["ok"] is True


def test_parse_trinity_style_verdicts():
    v = verify.parse_verdict('{"verdict": "REVISE", "problems": "the patch removes a test"}')
    assert v["ok"] is False and v["problems"] == ["the patch removes a test"]
    assert verify.parse_verdict('{"verdict": {"ok": false, "problems": ["x"]}}')["ok"] is False
    assert verify.parse_verdict("ACCEPT")["ok"] is True
    v = verify.parse_verdict("REVISE:\n- edits the wrong function\n- no test was run")
    assert v["ok"] is False and v["problems"] == ["edits the wrong function", "no test was run"]


def test_parse_problem_objects_are_flattened():
    v = verify.parse_verdict('{"ok": false, "problems": [{"description": "bad path"}, 3]}')
    assert v["problems"] == ["bad path", "3"]


@pytest.mark.parametrize("text", [None, "", "   ", "lorem ipsum dolor", "{not json at all",
                                  '{"foo": 1}', "[1, 2, 3]", 42])
def test_parse_garbage_fails_open(text):
    assert verify.parse_verdict(text) == {"ok": True, "problems": [], "severity": "low",
                                          "unparsed": True}


# --------------------------------------------------------------------------- #
# corrector_messages()
# --------------------------------------------------------------------------- #

def test_corrector_for_a_text_proposal():
    convo = [{"role": "user", "content": "Is x.py done?"}]
    verdict = {"ok": False, "problems": ["claims tests pass but none ran"], "severity": "high"}
    out = verify.corrector_messages(convo, {"role": "assistant", "content": "Done, all tests pass."},
                                    verdict)
    assert out[0] == convo[0] and out[0] is not convo[0]          # original kept, not mutated
    assert out[1] == {"role": "assistant", "content": "Done, all tests pass."}
    note = out[2]
    assert note["role"] == "user" and verify.CORRECTOR_NOTE_HEADER in note["content"]
    assert "claims tests pass but none ran" in note["content"]
    assert "HIGH" in note["content"]
    assert len(out) == 3 and len(convo) == 1


def test_corrector_for_a_tool_call_proposal_keeps_the_protocol_valid():
    convo = [{"role": "user", "content": "Fix counter.py"}]
    proposed = {"role": "assistant", "content": "Editing.",
                "tool_calls": [_call("Edit", {"file_path": "counter.py", "old_string": "a",
                                              "new_string": "b"}, "e1"),
                               {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}}]}
    out = verify.corrector_messages(convo, proposed, {"ok": False, "problems": ["edits line 3, bug is on 9"]})
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "tool", "tool", "user"]
    asst = out[1]
    assert asst["content"] == "Editing."
    ids = [c["id"] for c in asst["tool_calls"]]
    assert ids[0] == "e1" and ids[1]                              # missing id is filled in
    assert all(c["type"] == "function" for c in asst["tool_calls"])
    assert asst["tool_calls"][1]["function"]["name"] == "Bash"
    assert json.loads(asst["tool_calls"][1]["function"]["arguments"]) == {"command": "pytest"}
    assert [m["tool_call_id"] for m in out[2:4]] == ids
    assert all(m["content"] == verify.NOT_EXECUTED_TEXT for m in out[2:4])
    assert "edits line 3, bug is on 9" in out[4]["content"]
    assert "not executed" in out[4]["content"]


def test_corrector_without_problems_still_asks_for_a_recheck():
    out = verify.corrector_messages([], "draft", {"ok": False, "problems": []})
    assert out[0] == {"role": "assistant", "content": "draft"}
    assert "without details" in out[1]["content"]
    out = verify.corrector_messages([], "draft", None)
    assert out[-1]["role"] == "user"
