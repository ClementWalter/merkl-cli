#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0", "click>=8.1", "requests>=2.31", "pycryptodome>=3.20", "rich>=13"]
# ///
"""Unit tests for merkl_cli: normalization, filters, the HTTP client and the commands. No network."""

import io
import json
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from rich.console import Console
from test_airdrops import FACTORY, StubRpc

sys.path.insert(0, str(Path(__file__).parent.parent))
import airdrops
import merkl_cli
from merkl_cli import (
    MerklClient,
    Services,
    borrow_from_siblings,
    cli,
    format_config,
    format_line,
    last_valid,
    matches,
    normalize,
    render_pretty,
    retry_after,
    status_at,
)

NOW = 1_792_800_000  # 2026-10-23 UTC
CREATOR = "0x3333333333333333333333333333333333333333"


def listed(campaign_id: str, **overrides):
    """A campaign as `GET /v4/campaigns?withOpportunity=true` lists it, trimmed to the fields read."""
    record = {
        "campaignId": campaign_id,
        "opportunityId": "1234567890",
        "type": "CONFIDENTIAL",
        "distributionChainId": 1,
        "computeChainId": 1,
        "creatorAddress": CREATOR,
        "amount": "599330000000000000000000",
        "startTimestamp": 1_791_763_201,
        "endTimestamp": 1_794_527_940,
        "createdAt": "2026-10-23T13:39:13.535Z",
        "hasOverrides": False,
        "campaignStatus": {"computedUntil": 1_791_763_201, "status": "SUCCESS", "delay": 0, "error": ""},
        "rewardToken": {"address": "0x8888888888888888888888888888888888888888", "symbol": "RWD", "decimals": 18},
        "params": {
            "targetToken": "0x9999999999999999999999999999999999999999",
            "symbolTargetToken": "cvUSDC",
            "distributionMethodParameters": {
                "distributionMethod": "ERC4626_APR",
                "distributionSettings": {"mode": "FIX_APR", "targetAPR": "0.08"},
            },
        },
        "Opportunity": {"name": "Deposit into Example Vault"},
    }
    return {**record, **overrides}


EXAMPLE = listed("0x" + "7c" * 32)
# The campaign whose airdrop is 0x7bF3…223B, the airdrop test_airdrops' stub node serves.
EXAMPLE_LIVE = listed("0x4444444444444444444444444444444444444444444444444444444444444444")
OTHER = listed(
    "0x" + "11" * 32,
    createdAt="2026-09-15T06:36:30.118Z",
    params={"targetToken": "0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa", "symbolTargetToken": "cvUSDT"},
    Opportunity={"name": "Deposit into Another Vault"},
)
CANCELLED = listed(
    "0x" + "5c" * 32,
    id="1795720567874744505",
    type="INVALID",
    endTimestamp=1_792_579_431,
    params={},
    Opportunity={"name": f"Invalid Campaigns {CREATOR}"},
)
VAULT = "0x5555555555555555555555555555555555555555"


def config(target, apr, end=1_794_152_790):
    """A campaign config as Merkl's override history stores it."""
    settings = {} if apr is None else {"mode": "FIX_APR", "targetAPR": apr}
    return {
        "campaignType": 18 if target else 0,
        "startTimestamp": 1_791_819_990,
        "endTimestamp": end,
        **({"targetToken": target} if target else {}),
        "distributionMethodParameters": {"distributionSettings": settings},
    }


PUBLIC_EXAMPLE = {"type": "ERC20LOGPROCESSOR", "name": "Supply to Example Vault"}
# The override history of CANCELLED, as `/overrides/{id}/previous-configs` answers it (newest first).
HISTORY = [
    {"config": config(None, None, end=1_792_579_431), "overrideTimestamp": 1_792_493_063, "Opportunity": {"type": "INVALID"}},
    {"config": config(VAULT, "0.103"), "overrideTimestamp": 1_792_165_500, "Opportunity": PUBLIC_EXAMPLE},
    {"config": config(VAULT, "0.125"), "overrideTimestamp": 0, "Opportunity": PUBLIC_EXAMPLE},
]
OLDEST_FIRST = sorted(HISTORY, key=lambda e: e["overrideTimestamp"])


class Response:
    """What the stub session answers: a status, JSON, headers."""

    def __init__(self, status: int, body, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = json.dumps(body)
        self.headers = headers or {}

    def json(self):
        return self._body


class StubSession:
    """Answers each GET with the next of `answers`, recording the params it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.headers = {}

    def get(self, url, params, timeout, headers=None):
        self.calls.append({**params, "headers": headers or {}})
        return self.answers.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(merkl_cli.time, "sleep", lambda _s: None)


# Campaigns of type CONFIDENTIAL settle through airdrops of test_airdrops' factory.
AIRDROP_CONFIG = airdrops.AirdropConfig(campaign_types=frozenset({"CONFIDENTIAL"}), factories={1: FACTORY})


def services(client, chain=None, api_key=None):
    """The commands' services over a stub Merkl, `chain` for every chain id, and `api_key`."""
    return Services(
        merkl=client, chain=lambda _chain_id: chain, api_key=lambda: api_key, airdrop_config=lambda: AIRDROP_CONFIG
    )


def run(*answers, args):
    """`merkl <args>` over a stub Merkl, returning click's result."""
    client = MerklClient(session=StubSession(*answers))
    return CliRunner().invoke(cli, args, obj=services(client))


@pytest.mark.parametrize(("now", "expected"), [(0, "soon"), (1_791_763_201, "live"), (1_794_527_940, "past")])
def test_status_at(now, expected):
    assert status_at(1_791_763_201, 1_794_527_940, now) == expected


@pytest.mark.parametrize(("header", "expected"), [(None, 4), ("7", 7.0), ("999", 60.0), ("soon", 4)])
def test_retry_after(header, expected):
    assert retry_after(header, default=4) == expected


@pytest.fixture
def record():
    return normalize(EXAMPLE, NOW)


def test_normalize_scales_the_amount(record):
    assert record["amount"] == "599330"


def test_normalize_reads_the_target_apr(record):
    assert record["target_apr"] == 0.08


def test_normalize_derives_the_status(record):
    assert record["status"] == "live"


def test_normalize_writes_times_in_utc(record):
    assert record["start"] == "2026-10-12T00:00:01Z"


def test_normalize_keeps_the_opportunity_name(record):
    assert record["opportunity"] == "Deposit into Example Vault"


def test_normalize_leaves_no_apr_on_a_campaign_without_one():
    assert normalize(OTHER, NOW)["target_apr"] is None


@pytest.mark.parametrize("needle", ["example", "EXAMPLE", "cvusdc", "rwd"])
def test_matches_names_and_symbols(record, needle):
    assert matches(record, needle)


def test_matches_nothing_else(record):
    assert not matches(record, "another")


def test_format_line_ends_with_the_full_campaign_id(record):
    assert format_line(record).endswith("\t0x" + "7c" * 32)


def test_client_reads_every_page():
    full = [listed(f"0x{i:064x}") for i in range(100)]
    client = MerklClient(session=StubSession(Response(200, full), Response(200, [EXAMPLE])))
    assert len(client.campaigns({})) == 101


def test_client_keeps_a_repeated_campaign_once():
    full = [EXAMPLE] * 100
    client = MerklClient(session=StubSession(Response(200, full), Response(200, [])))
    assert len(client.campaigns({})) == 1


def test_client_retries_a_rate_limit():
    client = MerklClient(session=StubSession(Response(429, {}, {"Retry-After": "1"}), Response(200, [EXAMPLE])))
    assert len(client.campaigns({})) == 1


def test_client_reports_a_refusal_in_one_line():
    client = MerklClient(session=StubSession(Response(400, {"title": "Validation Error"})))
    with pytest.raises(click.ClickException, match="Merkl 400 on campaigns"):
        client.campaigns({})


def test_campaigns_json_is_valid_json():
    result = run(Response(200, [EXAMPLE, OTHER]), args=["campaigns", "--creator", CREATOR, "--json"])
    assert len(json.loads(result.stdout)) == 2


def test_campaigns_match_keeps_the_matching_campaigns():
    result = run(Response(200, [EXAMPLE, OTHER]), args=["campaigns", "--creator", CREATOR, "--match", "example", "--json"])
    assert [r["campaign_id"] for r in json.loads(result.stdout)] == ["0x" + "7c" * 32]


def test_campaigns_match_finds_a_cancelled_campaign_by_its_last_valid_config():
    result = run(
        Response(200, [OTHER, CANCELLED]),
        Response(200, HISTORY),
        args=["campaigns", "--creator", CREATOR, "--match", "example", "--json"],
    )
    assert [r["campaign_id"] for r in json.loads(result.stdout)] == ["0x" + "5c" * 32]


def test_campaigns_by_type_also_read_the_creators_cancelled_campaigns():
    session = StubSession(Response(200, []), Response(200, []))
    CliRunner().invoke(cli, ["campaigns", "--creator", CREATOR, "--type", "ERC20LOGPROCESSOR"], obj=services(MerklClient(session=session)))
    assert session.calls[1]["type"] == "INVALID"


def test_campaigns_by_target_alone_warn_that_cancelled_ones_need_the_creator(caplog):
    run(Response(200, []), args=["campaigns", "--target", VAULT])
    assert "add --creator" in caplog.text


def test_last_valid_takes_the_newest_config_with_a_target():
    assert last_valid(OLDEST_FIRST)["config"]["distributionMethodParameters"]["distributionSettings"]["targetAPR"] == "0.103"


def test_last_valid_dates_the_cancel():
    assert last_valid(OLDEST_FIRST)["cancelled_at"] == 1_792_493_063


def test_last_valid_finds_nothing_without_a_target():
    assert last_valid([HISTORY[0]]) is None


@pytest.fixture
def cancelled_record():
    return normalize(CANCELLED, NOW, last_valid(OLDEST_FIRST))


def test_a_cancelled_campaign_takes_its_type_from_the_last_valid_config(cancelled_record):
    assert cancelled_record["type"] == "ERC20LOGPROCESSOR"


def test_a_cancelled_campaign_keeps_merkls_type(cancelled_record):
    assert cancelled_record["merkl_type"] == "INVALID"


def test_a_cancelled_campaign_is_past_once_its_cut_window_ends(cancelled_record):
    assert cancelled_record["status"] == "past"


def test_a_cancelled_campaign_shows_the_end_it_was_planned_to(cancelled_record):
    assert cancelled_record["planned_end"] == "2026-11-08T15:46:30Z"


def test_a_cancelled_campaign_line_says_when(cancelled_record):
    assert "cancelled 2026-10-20 10:44" in format_line(cancelled_record)


def test_an_invalid_campaign_never_overridden_is_invalid():
    created = [{"config": config(VAULT, "0.12"), "overrideTimestamp": None}]
    assert normalize(CANCELLED, NOW, last_valid(created))["status"] == "invalid"


def test_borrow_from_siblings_names_the_target():
    records = [normalize(EXAMPLE, NOW), {**normalize(CANCELLED, NOW), "target_token": EXAMPLE["params"]["targetToken"].lower(), "target_symbol": None}]
    borrow_from_siblings(records)
    assert records[1]["target_symbol"] == "cvUSDC"


def test_history_falls_back_to_the_creation_config():
    client = MerklClient(session=StubSession(Response(404, {}), Response(200, config(VAULT, "0.12"))))
    assert client.history({**CANCELLED, "campaignEncodingHash": "0x" + "f8" * 32})[0]["config"]["targetToken"] == VAULT


def test_format_config_reads_a_zero_stamp_as_the_creation():
    assert format_config(OLDEST_FIRST[0]).startswith("created")


def test_campaigns_list_newest_first():
    result = run(Response(200, [OTHER, EXAMPLE]), args=["campaigns", "--creator", CREATOR, "--json"])
    assert json.loads(result.stdout)[0]["campaign_id"] == "0x" + "7c" * 32


def test_campaigns_ask_merkl_for_the_creator():
    client = MerklClient(session=StubSession(Response(200, [])))
    CliRunner().invoke(cli, ["campaigns", "--creator", CREATOR], obj=services(client))
    assert client.session.calls[0]["creatorAddresses"] == [CREATOR]


def test_campaigns_ask_once_per_target():
    session = StubSession(Response(200, []), Response(200, []))
    target = "0x9999999999999999999999999999999999999999"
    CliRunner().invoke(cli, ["campaigns", "--target", target, "--target", CREATOR], obj=services(MerklClient(session=session)))
    assert [call["mainParameter"] for call in session.calls] == [target, CREATOR]


def test_campaigns_refuse_an_unfiltered_listing():
    assert run(args=["campaigns"]).exit_code == 2


def test_campaigns_refuse_a_malformed_address():
    assert "is not an address" in run(args=["campaigns", "--creator", "0x1234"]).stderr


def test_campaign_reports_an_unknown_id():
    result = run(Response(200, []), args=["campaign", "0x" + "ab" * 32])
    assert "no campaign 0x" in result.stderr


def test_campaign_raw_returns_merkls_record():
    result = run(Response(200, [EXAMPLE]), args=["campaign", "0x" + "7c" * 32, "--raw"])
    assert json.loads(result.stdout) == [EXAMPLE]


def test_campaign_refuses_a_malformed_id():
    assert "is not a campaign id" in run(args=["campaign", "0xdead"]).stderr


def airdrops_run(budget_raw: str, creator_reward: str | None = None):
    """`merkl campaigns --airdrops --json` over a stub node whose airdrop holds 599,330 RWD.

    With `creator_reward`, Merkl scores that much of the budget back to the creator.
    """
    chain = airdrops.Chain("stub")
    stub = StubRpc()
    calls = {"eth_chainId": "0x1", "eth_blockNumber": hex(105)}
    implementation = "0x" + "22" * 20
    stub.calls[(FACTORY, airdrops.selector("merkleImplementation()"))] = "0x" + implementation[2:].rjust(64, "0")
    chain.request = lambda method, params: calls[method] if method in calls else stub.request(method, params)
    answers = [Response(200, [{**EXAMPLE_LIVE, "amount": budget_raw}])]
    if creator_reward is not None:
        mine = {"recipient": CREATOR, "breakdowns": [{"reason": "end_campaign", "amount": creator_reward}]}
        answers.append(Response(200, mine))
    client = MerklClient(session=StubSession(*answers))
    key = None if creator_reward is None else "creator-key"
    args = ["campaigns", "--creator", CREATOR, "--airdrops", "--json"]
    result = CliRunner().invoke(cli, args, obj=services(client, chain, api_key=key))
    return json.loads(result.stdout)[0]["airdrop"]


def test_airdrops_report_a_budget_wrapped_in_full():
    assert airdrops_run("599330000000000000000000")["funded_in_full"] is True


def test_airdrops_report_a_shortfall():
    assert airdrops_run("600000000000000000000000")["shortfall"] == "670"


def test_airdrops_warn_about_a_short_airdrop(caplog):
    airdrops_run("600000000000000000000000")
    assert "670 short" in caplog.text


def test_campaigns_status_live_leaves_out_a_rejected_campaign():
    rejected = {**CANCELLED, "endTimestamp": NOW + 86_400}
    created = [{"config": {"targetToken": VAULT, "campaignType": 231}, "overrideTimestamp": 0}]
    result = run(Response(200, [rejected]), Response(200, created), args=["campaigns", "--creator", CREATOR, "--status", "live", "--json"])
    assert json.loads(result.stdout) == []


def pretty(records):
    """What --pretty prints for `records`, without colour."""
    out = io.StringIO()
    render_pretty(records, Console(file=out, width=200, color_system=None))
    return out.getvalue()


def test_pretty_titles_each_table_with_its_target_side_and_opportunity(record):
    assert "cvUSDC · CONFIDENTIAL · Deposit into Example Vault" in pretty([record])


def test_pretty_says_when_a_cancelled_campaign_stopped(cancelled_record):
    assert "cancelled 2026-10-20 10:44" in pretty([{**cancelled_record, "target_symbol": "vUSDC"}])


def test_pretty_shows_an_undeployed_airdrop(record):
    drop = {"address": "0x7bF3f94fb31377D4Ba1ab3D088B9B062237D223B", "deployed": False}
    assert "0x7bF3…223B not deployed" in pretty([{**record, "airdrop": drop}])


def test_normalize_links_the_campaign_page(record):
    assert record["merkl_url"] == f"https://app.merkl.xyz/opportunities/1234567890?campaignId=0x{'7c' * 32}"


def test_pretty_links_the_campaign_column(record):
    out = io.StringIO()
    render_pretty([record], Console(file=out, width=200, force_terminal=True))
    assert record["merkl_url"] in out.getvalue()


def test_airdrops_count_the_budget_handed_back_to_the_creator_as_not_owed():
    assert airdrops_run("1000000000000000000000000", creator_reward="400670000000000000000000")["funded_in_full"] is True


def test_airdrops_report_what_went_back_to_the_creator():
    assert airdrops_run("1000000000000000000000000", creator_reward="400670000000000000000000")["creator_reward"] == "400670"


def test_airdrops_round_the_creator_reward_down_to_the_wrappers_rate():
    # 0.5e12 base units is below one wrapper unit, so none of it is withheld.
    assert airdrops_run("599330000000000000000000", creator_reward="500000000000")["creator_reward"] == "0"


def test_airdrops_without_an_api_key_say_the_whole_budget_is_counted(caplog):
    airdrops_run("600000000000000000000000")
    assert "no Merkl API key" in caplog.text


def test_creator_reward_sends_the_api_key():
    session = StubSession(Response(200, {"recipient": CREATOR, "breakdowns": []}))
    MerklClient(session=session).creator_reward("0x" + "7c" * 32, CREATOR, 1, "creator-key")
    assert session.calls[0]["headers"] == {"X-API-Key": "creator-key"}


def test_creator_reward_sums_the_breakdowns():
    breakdowns = [{"amount": "5"}, {"amount": "7"}]
    session = StubSession(Response(200, {"recipient": CREATOR, "breakdowns": breakdowns}))
    assert MerklClient(session=session).creator_reward("0x" + "7c" * 32, CREATOR, 1, "k") == 12


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
