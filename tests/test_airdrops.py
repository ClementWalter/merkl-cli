#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest==9.1.1",
#     "click==8.5.0",
#     "requests==2.34.2",
#     "pycryptodome==4.2.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Unit tests for airdrops: the airdrop address, RPC resolution and the on-chain read. No network."""

import json
import sys
from pathlib import Path

import click
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import airdrops
from airdrops import (
    CLAIMED,
    ROOT_INITIALIZED,
    ROOT_SET,
    WRAP,
    Chain,
    checksum,
    expected_funding,
    predict_airdrop,
    read_airdrop,
    resolve_api_key,
    resolve_rpc,
    selector,
)

FACTORY = "0x1111111111111111111111111111111111111111"
IMPLEMENTATION = "0x2222222222222222222222222222222222222222"
CREATOR = "0x3333333333333333333333333333333333333333"
AIRDROP = "0x7bF3f94fb31377D4Ba1ab3D088B9B062237D223B"
CTOKEN = "0x6666666666666666666666666666666666666666"
TOKEN = "0x7777777777777777777777777777777777777777"


def test_predict_airdrop_matches_create2():
    # `cast create2` over the same factory, implementation, salt and EIP-1167 init code.
    campaign = "0x4444444444444444444444444444444444444444444444444444444444444444"
    assert predict_airdrop(FACTORY, IMPLEMENTATION, CREATOR, campaign) == AIRDROP


def test_checksum_is_eip55():
    assert checksum(AIRDROP.lower()) == AIRDROP


def test_selector_of_merkle_root():
    assert selector("merkleRoot()") == "0x2eb4a7ab"


def test_resolve_rpc_prefers_the_flag():
    assert resolve_rpc(1, "https://flag", {"ETH_RPC_URL": "https://env"}) == "https://flag"


def test_resolve_rpc_then_reads_eth_rpc_url():
    assert resolve_rpc(1, None, {"ETH_RPC_URL": "https://env"}) == "https://env"


def test_resolve_rpc_then_reads_the_config(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"rpc": {"1": "https://config"}}))
    monkeypatch.setattr(airdrops, "CONFIG_PATH", config)
    assert resolve_rpc(1, None, {}) == "https://config"


def test_resolve_rpc_without_any_names_every_way_to_set_one(tmp_path, monkeypatch):
    monkeypatch.setattr(airdrops, "CONFIG_PATH", tmp_path / "absent.json")
    with pytest.raises(click.UsageError, match="--rpc-url, set ETH_RPC_URL"):
        resolve_rpc(1, None, {})


def word(value: int | str) -> str:
    """A 32-byte ABI word."""
    raw = value[2:].lower() if isinstance(value, str) else f"{value:x}"
    return "0x" + raw.rjust(64, "0")


class StubRpc:
    """Answers JSON-RPC by method and call target, like a node over one deployed airdrop."""

    def __init__(self, deployed: bool = True):
        self.deployed = deployed
        self.calls = {
            (AIRDROP, selector("deploymentBlockNumber()")): word(100),
            (AIRDROP, selector("token()")): word(CTOKEN),
            (AIRDROP, selector("merkleRoot()")): word("0x" + "ab" * 32),
            (AIRDROP, selector("paused()")): word(0),
            (CTOKEN, selector("underlying()")): word(TOKEN),
            (CTOKEN, selector("rate()")): word(10**12),
            (TOKEN, selector("decimals()")): word(18),
        }
        claimant = word("0x" + "01" * 20)
        self.logs = {
            CTOKEN: [{"topics": [WRAP, word(AIRDROP)], "data": word(599_330 * 10**18) + "00" * 32}],
            AIRDROP: [
                {"topics": [ROOT_INITIALIZED, word("0x" + "aa" * 32)], "data": "0x"},
                {"topics": [ROOT_SET, word("0x" + "aa" * 32), word("0x" + "ab" * 32)], "data": "0x"},
                {"topics": [CLAIMED, claimant, word("0x" + "cc" * 32)], "data": "0x"},
                {"topics": [CLAIMED, claimant, word("0x" + "cd" * 32)], "data": "0x"},
            ],
        }

    def request(self, method, params):
        if method == "eth_getCode":
            return "0x6080" if self.deployed else "0x"
        if method == "eth_call":
            return self.calls[(params[0]["to"], params[0]["data"])]
        if method == "eth_getLogs":
            return self.logs.get(params[0]["address"], []) if params[0]["fromBlock"] == hex(100) else []
        raise AssertionError(method)


@pytest.fixture
def airdrop():
    chain = Chain("stub")
    stub = StubRpc()
    chain.request = stub.request
    return read_airdrop(chain, AIRDROP, head=105)


def test_read_airdrop_sums_the_wraps(airdrop):
    assert airdrop.as_record()["funded"] == "599330"


def test_read_airdrop_counts_the_first_root_and_each_replacement(airdrop):
    assert airdrop.roots_set == 2


def test_read_airdrop_counts_claims(airdrop):
    assert airdrop.claims == 2


def test_read_airdrop_counts_claimants_once(airdrop):
    assert airdrop.claimants == 1


def test_read_airdrop_reads_the_live_root(airdrop):
    assert airdrop.root == "0x" + "ab" * 32


def test_read_airdrop_reports_an_undeployed_airdrop():
    chain = Chain("stub")
    chain.request = StubRpc(deployed=False).request
    assert read_airdrop(chain, AIRDROP, head=105).deployed is False


def test_logs_read_in_chunks(monkeypatch):
    chain = Chain("stub")
    ranges = []
    chain.request = lambda method, params: ranges.append((params[0]["fromBlock"], params[0]["toBlock"])) or []
    monkeypatch.setattr(airdrops, "LOG_CHUNK", 10)
    chain.logs(AIRDROP, [], 0, 25)
    assert ranges == [("0x0", "0x9"), ("0xa", "0x13"), ("0x14", "0x19")]


def test_expected_funding_rounds_down_to_the_wrappers_rate():
    assert expected_funding(10**18 + 5, 18, 18, 10**12) == 10**18


def test_expected_funding_scales_the_mocks_decimals_to_the_real_tokens():
    assert expected_funding(1_000_000, 6, 18, 1) == 10**18


def test_read_airdrop_reads_the_wrappers_rate(airdrop):
    assert airdrop.rate == 10**12


def test_resolve_api_key_reads_the_environment():
    assert resolve_api_key({"MERKL_API_KEY": "env-key"}) == "env-key"


def test_resolve_api_key_then_reads_the_config(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"merkl_api_key": "config-key"}))
    monkeypatch.setattr(airdrops, "CONFIG_PATH", config)
    assert resolve_api_key({}) == "config-key"


def test_resolve_api_key_is_none_without_one(tmp_path, monkeypatch):
    monkeypatch.setattr(airdrops, "CONFIG_PATH", tmp_path / "absent.json")
    assert resolve_api_key({}) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
