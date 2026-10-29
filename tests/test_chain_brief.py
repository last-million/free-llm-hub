"""The smart-contracts / web3 brief (craft.CHAIN_DEV, skill `chain_dev`), and the
two pieces that make contract work visible to the hub's other machinery: observed
`forge test` / `hardhat test` results (evidence.py) and forge/anvil/slither in the
THIS MACHINE block (envprobe.py). Pure: no tool runs, nothing is installed.
"""
import pytest

import craft
import envprobe
import evidence
import skills


# --------------------------------------------------------------------------- #
# the brief
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "write an ERC20 token in Solidity with Foundry tests",
    "audit this smart contract for reentrancy",
    "add fuzz tests to my hardhat project",
    "build a DeFi staking dapp",
    "fork mainnet with anvil and test the swap against uniswap",
    "my vault contract fails forge test",
    "connect the frontend with wagmi and viem",
    "deploy the governance contract to sepolia",
    "run slither on the contracts folder",
    "port this to vyper",
    "an ERC-721 mint page with ethers.js",
])
def test_the_brief_fires_on_contract_work(text):
    assert "chain_dev" in craft.names(text)


@pytest.mark.parametrize("text", [
    "explain how a mutex works",
    "set up azure ai foundry for our agents",
    "run models with foundry local",
    "forge ahead with the refactor",
    "build me a restaurant website",
    "refactor the auth module and write tests",
    "what is the capital of France",
    "fix the token refresh in the login flow",
])
def test_the_brief_stays_silent_elsewhere(text):
    assert "chain_dev" not in craft.names(text)


def test_the_brief_takes_a_domain_slot_first():
    """A dapp landing page must not lose the contract brief to two generic domains."""
    names = [n for n in craft.names("build a defi dapp landing page and deploy it")
             if n not in {o for o, _r, _b in craft._ORTHOGONAL}]
    assert names[0] == "chain_dev" and len(names) <= craft.MAX_BRIEFS


def test_the_brief_is_small_and_says_the_right_things():
    b = craft.CHAIN_DEV
    assert len(b) < 1400 and "ANTI" in b                   # the domain-brief budget
    low = b.lower()
    for word in ("foundry", "hardhat", "unit tests", "fuzz", "invariant", "gas",
                 "reentrancy", "access control", "oracle", "rounding", "replay", "nonce",
                 "storage layout", "unchecked external calls"):
        assert word in low, word
    for word in ("local fork", "testnet", "mainnet transaction unless the user explicitly asks",
                 "private key", "seed phrase", ".gitignore"):
        assert word in low, word
    for tool in ("chain_tools_status", "chain_source", "chain_read", "chain_fork_start",
                 "chain_analyze"):
        assert tool in b
    assert "if offered" in low                              # never promises a missing tool


def test_the_brief_rides_before_the_loop_and_inside_the_ceiling():
    for text in ("write an ERC20 token in Solidity with Foundry tests",
                 "build a defi dapp landing page with login, seo and images and deploy it",
                 "build a web3 nft marketplace website with stripe checkout and deploy it",
                 "audit my lending contract"):
        msg = craft.system_message(text)["content"]
        assert craft.CHAIN_DEV in msg
        assert msg.index(craft.CHAIN_DEV) < msg.index(craft.PLAN_PHASES)
        assert len(msg) <= craft.BRIEF_CEILING_CHARS, text
    tool_less = craft.system_message("audit my lending contract", tools=False)["content"]
    assert craft.CHAIN_DEV in tool_less and craft.VERIFY_READ in tool_less


def test_the_canonical_worst_case_requests_do_not_pay_for_it():
    for text in ("build an online store and deploy it", "create a landing page for my saas",
                 "build me a restaurant website"):
        assert "chain_dev" not in craft.names(text)


def test_it_is_a_toggleable_builtin_skill(monkeypatch):
    row = [b for b in skills.BUILTIN if b[0] == "chain_dev"]
    assert row and row[0][1] == "Smart contracts / web3"
    assert "chain_dev" in skills.BUILTIN_IDS
    monkeypatch.setattr(craft, "_SKILL_SOURCE", lambda: (["chain_dev"], []))
    assert "chain_dev" not in craft.names("audit this smart contract")
    monkeypatch.setattr(craft, "_SKILL_SOURCE", lambda: ([], []))
    assert "chain_dev" in craft.names("audit this smart contract")


# --------------------------------------------------------------------------- #
# observed contract tests (evidence.py)
# --------------------------------------------------------------------------- #

FORGE_PASS = """[⠊] Compiling...
No files changed, compilation skipped

Ran 2 tests for test/Counter.t.sol:CounterTest
[PASS] testFuzz_SetNumber(uint256) (runs: 256, μ: 30899, ~: 31288)
[PASS] test_Increment() (gas: 31303)
Suite result: ok. 2 passed; 0 failed; 0 skipped; finished in 7.86ms (7.38ms CPU time)

Ran 1 test suite in 8.48ms (7.86ms CPU time): 2 tests passed, 0 failed, 0 skipped (2 total tests)
"""

FORGE_FAIL = """Ran 3 tests for test/Vault.t.sol:VaultTest
[PASS] test_Deposit() (gas: 51000)
[FAIL: assertion failed: 1 != 2] test_Withdraw() (gas: 61000)
[FAIL: panic: arithmetic underflow or overflow (0x11); counterexample: calldata=0x args=[0]] testFuzz_Share(uint256) (runs: 3)
Suite result: FAILED. 1 passed; 2 failed; 0 skipped; finished in 12.1ms (11.0ms CPU time)

Ran 1 test suite in 13.0ms (12.1ms CPU time): 1 tests passed, 2 failed, 0 skipped (3 total tests)

Failing tests:
Encountered 2 failing tests in test/Vault.t.sol:VaultTest
"""

FORGE_OLD = """Running 2 tests for test/A.t.sol:ATest
[PASS] test_a() (gas: 100)
[PASS] test_b() (gas: 200)
Test result: ok. 2 passed; 0 failed; 0 skipped; finished in 1.2ms
Ran 1 test suites: 2 tests passed, 0 failed, 0 skipped (2 total tests)
"""

FORGE_COMPILE_FAIL = """[⠊] Compiling 3 files with Solc 0.8.26
Error: Compiler run failed:
Error (2314): Expected ';' but got '}'
 --> src/Vault.sol:12:5:
"""

MOCHA_PASS = """

  Lock
    Deployment
      ✔ Should set the right unlockTime (812ms)
      ✔ Should set the right owner

  2 passing (850ms)
"""

MOCHA_FAIL = """
  Token
    ✔ mints
    1) transfers

  1 passing (1s)
  1 failing

  1) Token
       transfers:
     AssertionError: expected 1 to equal 2
"""

HARDHAT3 = """Compiling your Solidity contracts...
Compiled 2 Solidity files with solc 0.8.28 (evm target: cancun)

Running Solidity tests

  contracts/Counter.t.sol:CounterTest
    ✔ test_InitialValue()
    ✔ testFuzz_Inc(uint8) (runs: 256)

  2 passing

Running node:test tests

  Counter
    ✔ emits the Increment event

  1 passing (412ms)
"""


@pytest.mark.parametrize("command,code,out,verdict,passed,failed", [
    ("forge test", 0, FORGE_PASS, "PASS", 2, 0),
    ("forge test -vvv --match-contract Vault", 1, FORGE_FAIL, "FAIL", 1, 2),
    ("forge t", 0, FORGE_OLD, "PASS", 2, 0),
    ("forge test", 1, FORGE_COMPILE_FAIL, "FAIL", 0, 1),
    ("forge test", 0, "No tests found in project! Forge looks for functions that "
                      "starts with `test`.\n", "NO_TESTS", 0, 0),
    ("forge test | tail -5", 0, FORGE_PASS, "UNDETERMINED", 2, 0),      # someone else's code
    ("forge test", 0, FORGE_FAIL, "UNDETERMINED", 1, 2),                # never FAIL on exit 0
    ("npx hardhat test", 0, MOCHA_PASS, "PASS", 2, 0),
    ("npx hardhat test", 1, MOCHA_FAIL, "FAIL", 1, 1),
    ("pnpm hardhat test", 0, MOCHA_PASS, "PASS", 2, 0),
    ("npx hardhat test", 0, HARDHAT3, "PASS", 3, 0),
    ("npx hardhat test", 1, "Error HH600: Compilation failed\n", "FAIL", 0, 1),
    ('wsl bash -c "cd ~/vault && forge test"', 0, FORGE_PASS, "PASS", 2, 0),
])
def test_contract_test_runs_are_observed(command, code, out, verdict, passed, failed):
    r = evidence.classify(command, code, out)
    assert r["verdict"] == verdict, r
    assert (r["passed"], r["failed"]) == (passed, failed)
    assert r["kind"] == "test"


def test_ansi_colours_do_not_hide_the_forge_summary():
    coloured = FORGE_PASS.replace("Suite result: ok.", "Suite result: \x1b[32mok\x1b[0m.")
    assert evidence.classify("forge test", 0, coloured)["verdict"] == "PASS"


def test_an_npm_test_script_that_runs_hardhat_is_read_through():
    out = "\n> vault@1.0.0 test\n> hardhat test\n" + MOCHA_PASS
    r = evidence.classify("npm test", 0, out)
    assert r["verdict"] == "PASS" and "hardhat test" in r["tool"]


@pytest.mark.parametrize("command,code,out,verdict", [
    ("forge build", 0, "Compiling 12 files with Solc 0.8.26\nCompiler run successful!\n", "PASS"),
    ("forge build", 0, "No files changed, compilation skipped\n", "PASS"),
    ("forge build", 1, FORGE_COMPILE_FAIL, "FAIL"),
    ("npx hardhat compile", 0, "Compiled 4 Solidity files successfully (evm target: paris).\n",
     "PASS"),
    ("npx hardhat compile", 1, "Error HH600: Compilation failed\n", "FAIL"),
])
def test_contract_builds_are_observed(command, code, out, verdict):
    r = evidence.classify(command, code, out)
    assert r["verdict"] == verdict and r["kind"] == "build"


def test_other_forge_and_hardhat_commands_are_not_evidence():
    for command in ("forge fmt", "forge script script/Deploy.s.sol", "npx hardhat node",
                    "forge --version", "cast call 0x0 'f()'"):
        assert evidence.detect(command) is None, command


def test_a_later_pass_of_the_same_suite_covers_an_earlier_failure():
    assert evidence.covers("forge test", "forge test")
    assert evidence.covers("forge test", "forge test test/Vault.t.sol")    # whole suite
    assert evidence.covers("npx hardhat test", "hardhat test")
    assert not evidence.covers("forge test test/A.t.sol", "forge test test/Vault.t.sol")


# --------------------------------------------------------------------------- #
# THIS MACHINE (envprobe.py)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tool,text,want", [
    ("forge", "forge Version: 1.3.5-stable\nCommit SHA: 9979a41\nBuild Profile: maxperf\n", "1.3.5"),
    ("forge", "forge 0.2.0 (a1b2c3d 2024-08-01T00:00:00.000000000Z)", "0.2.0"),
    ("anvil", "anvil Version: 1.3.5-stable\r\n", "1.3.5"),
    ("slither", "0.10.4\n", "0.10.4"),
    ("forge", "forge: command not found", None),
])
def test_contract_tool_versions(tool, text, want):
    assert envprobe.parse_version(tool, text) == want


def _snap(**tools):
    base = {"node": "22.11.0", "npm": "10.9.0", "python": "3.12.6"}
    base.update(tools)
    return {"tools": base, "docker_daemon": None, "services": {}, "busy_ports": []}


def test_the_block_names_contract_tools_only_when_present():
    with_tools = envprobe.block(_snap(forge="1.3.5", anvil="1.3.5", slither="0.10.4"), "session")
    assert "forge 1.3, anvil 1.3, slither 0.10" in with_tools
    without = envprobe.block(_snap(), "session")
    assert "forge" not in without and "slither" not in without
    assert len(with_tools) - len(without) < 40


def test_the_probe_asks_for_contract_tool_versions_only():
    calls = []
    paths = {"forge": "/home/u/.foundry/bin/forge", "anvil": "/home/u/.foundry/bin/anvil"}

    def run(argv, timeout):
        calls.append(list(argv))
        return {"forge": (0, "forge Version: 1.3.5-stable\n"),
                "anvil": (0, "anvil Version: 1.3.5-stable\n")}.get(argv[0].rsplit("/", 1)[-1])

    snap = envprobe.probe(which=lambda n: paths.get(n), run=run, port_open=lambda p: False,
                          os_name="linux")
    assert snap["tools"]["forge"] == "1.3.5" and snap["tools"]["anvil"] == "1.3.5"
    assert snap["tools"]["slither"] is None
    assert all(c[-1] == "--version" for c in calls)


def test_contract_builds_get_the_machine_block():
    assert envprobe.wants_block("write a solidity smart contract for a vault")
    assert envprobe.wants_block("build a dapp for our dao")
    assert not envprobe.wants_block("what is a smart contract?")
