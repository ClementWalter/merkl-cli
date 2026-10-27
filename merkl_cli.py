#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.5.0",
#     "requests==2.34.2",
#     "pycryptodome==4.2.0",
#     "rich==15.0.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Merkl CLI: read incentive campaigns from Merkl's public v4 API.

Read-only and unauthenticated: https://api.merkl.xyz/v4 serves every public
campaign. Merkl's own `search` does not match opportunity names, so `--match`
filters client-side on the opportunity name, the target token and the reward
token, with the opportunity data requested alongside each listing.

Cancelling a campaign overrides it with a target-less config, which Merkl then
lists as type INVALID under "Invalid Campaigns <creator>". The campaign paid out
until the cancel, so each INVALID campaign is read back from its override history
(`/overrides/{id}/previous-configs`, else its creation config by
`campaignEncodingHash`): its last valid type, target, opportunity and APR, when it
was cancelled and the end it was planned to run to.

A confidential campaign pays in a Merkl mock, so Merkl cannot say whether holders
were paid. `--airdrops` reads the TokenOps reward airdrop that pays it on chain
(see airdrops.py): deployed or not, its root, the real tokens wrapped into it and
the claims it served.

Output conventions:
- default: one compact line per campaign, the full campaign id tab-separated last
- --json:  normalized records (scaled amounts, UTC ISO times, derived status)
- --raw:   Merkl's answer, untouched (debugging only)
- --pretty: a table for a human at a terminal, grouped by target token
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import click
import requests
from rich.console import Console
from rich.table import Table

import airdrops

log = logging.getLogger("merkl-cli")

API_BASE = "https://api.merkl.xyz/v4"
# A campaign's page in Merkl's app, under the opportunity Merkl files it in now.
APP_CAMPAIGN = "https://app.merkl.xyz/opportunities/{opportunity}?campaignId={campaign}"
# Merkl's maximum page size.
PAGE_SIZE = 100
# A listing still full after this many pages is a filter too broad to read whole.
MAX_PAGES = 50
TIMEOUT_SECONDS = 20
RETRIES = 3
STATUSES = ("live", "soon", "past")
# Merkl answers about 0.2 s per call and allows 4200 a minute: 8 at once stays far below.
RESOLVE_WORKERS = 8


class MerklClient:
    """Merkl's HTTP API over one session: bounded retries on 429/5xx, paginated listings."""

    def __init__(self, session: requests.Session | None = None, base: str = API_BASE):
        self.session = session or requests.Session()
        # Merkl's CDN answers 403 to the default Python user agent.
        self.session.headers["User-Agent"] = "merkl-cli"
        self.base = base

    def get(
        self, path: str, params: dict[str, Any], missing_ok: bool = False, headers: dict[str, str] | None = None
    ) -> Any:
        """One GET, retried on 429/5xx honoring `Retry-After`; any other failure is one line.

        With `missing_ok`, a 404 answers None.
        """
        url = f"{self.base}/{path}"
        for attempt in range(1, RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, timeout=TIMEOUT_SECONDS, headers=headers)
            except requests.RequestException as exc:
                if attempt == RETRIES:
                    raise click.ClickException(f"Merkl unreachable: {exc}") from exc
                log.warning("GET %s failed (%s), retrying", path, exc)
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == RETRIES:
                    raise click.ClickException(f"Merkl {resp.status_code} on {path} after {RETRIES} tries")
                wait = retry_after(resp.headers.get("Retry-After"), default=2**attempt)
                log.warning("GET %s -> %s, retrying in %ss", path, resp.status_code, wait)
                time.sleep(wait)
                continue
            if missing_ok and resp.status_code == 404:
                return None
            if not resp.ok:
                raise click.ClickException(f"Merkl {resp.status_code} on {path}: {resp.text[:300]}")
            return resp.json()
        raise AssertionError("unreachable")

    def campaigns(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Every page of `GET /campaigns` for `params`, each campaign once."""
        seen: dict[tuple[str, int], dict[str, Any]] = {}
        for page in range(MAX_PAGES):
            batch = self.get("campaigns", {**params, "items": PAGE_SIZE, "page": page})
            for campaign in batch:
                # Pages can repeat a campaign; a campaign id is unique per distribution chain.
                seen.setdefault((campaign["campaignId"], campaign["distributionChainId"]), campaign)
            if len(batch) < PAGE_SIZE:
                return list(seen.values())
        raise click.ClickException(
            f"still full after {MAX_PAGES} pages of {PAGE_SIZE}; narrow it with --creator, --target or --chain"
        )

    def creator_reward(self, campaign_id: str, creator: str, chain_id: int, api_key: str) -> int:
        """What Merkl scores to the campaign's own creator, in base units of the reward token.

        Merkl hands a campaign's unspent budget back to its creator as a reward (`end_campaign`).
        Only the creator's API key may read it: the leaderboard of a confidential campaign is hidden.
        """
        answer = self.get(
            f"rewards/campaign/{campaign_id}/mine",
            {"recipient": creator, "chainId": chain_id},
            headers={"X-API-Key": api_key},
        )
        if answer.get("recipient", "").lower() != creator.lower():
            raise click.ClickException(f"Merkl answered {campaign_id}'s creator reward for {answer.get('recipient')}")
        return sum(int(b["amount"]) for b in answer.get("breakdowns", []))

    def history(self, campaign: dict[str, Any]) -> list[dict[str, Any]]:
        """Every config the campaign has had, oldest first: its creation config, then each override.

        Merkl keys the history by its internal `id`. A campaign never overridden has no history,
        so its creation config is read by `campaignEncodingHash` instead.
        """
        entries = self.get(f"overrides/{campaign['id']}/previous-configs", {}, missing_ok=True) or []
        if not entries and campaign.get("campaignEncodingHash"):
            created = self.get(f"config/hash/{campaign['campaignEncodingHash']}", {}, missing_ok=True)
            entries = [] if created is None else [{"config": created, "overrideTimestamp": None}]
        return sorted(entries, key=lambda e: e.get("overrideTimestamp") or 0)

    def resolve_cancelled(self, campaigns: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """The last valid config of each INVALID campaign, by campaign id; unresolvable ones are absent."""
        invalid = [c for c in campaigns if c.get("type") == "INVALID"]
        with ThreadPoolExecutor(RESOLVE_WORKERS) as pool:
            histories = list(pool.map(self.history, invalid))
        resolved = {}
        for campaign, entries in zip(invalid, histories):
            found = last_valid(entries)
            if found is None:
                log.warning("campaign %s is INVALID and no config of it names a target", campaign["campaignId"])
            else:
                resolved[campaign["campaignId"]] = found
        return resolved


def last_valid(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest config in `entries` (oldest first) that names a target, and when it stopped applying.

    Returns `config`, its `opportunity` (absent for a creation config read by hash) and
    `cancelled_at`: the timestamp of the first target-less override after it, or None.
    """
    valid_at = None
    for index, entry in enumerate(entries):
        if (entry.get("config") or {}).get("targetToken"):
            valid_at = index
    if valid_at is None:
        return None
    later = [e.get("overrideTimestamp") for e in entries[valid_at + 1 :] if not (e.get("config") or {}).get("targetToken")]
    entry = entries[valid_at]
    return {
        "config": entry["config"],
        "opportunity": entry.get("Opportunity") or {},
        "cancelled_at": later[0] if later else None,
    }


def retry_after(header: str | None, default: float) -> float:
    """The wait a `Retry-After` header asks for, capped at 60 s; `default` when absent or unparsable."""
    try:
        return min(max(float(header), 0.0), 60.0) if header else default
    except ValueError:
        return default


def status_at(start: int, end: int, now: int) -> str:
    """Merkl's campaign status at `now`: `soon` before the start, `past` from the end."""
    if now < start:
        return "soon"
    return "live" if now < end else "past"


def iso(ts: int | None) -> str | None:
    """A Unix timestamp as an ISO-8601 UTC string."""
    return None if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def scaled(raw: str | int, decimals: int) -> Decimal:
    """A token amount in base units, in whole tokens."""
    return Decimal(int(raw)).scaleb(-decimals)


def normalize(campaign: dict[str, Any], now: int, cancelled: dict[str, Any] | None = None) -> dict[str, Any]:
    """The fields a reader needs, flattened: amounts scaled, times in UTC, status derived.

    For a cancelled campaign, `cancelled` is its `last_valid` config: type, target, opportunity
    and APR come from it, and the window stays the one Merkl paid out over.
    """
    params = campaign.get("params") or {}
    opportunity = campaign.get("Opportunity") or {}
    campaign_type = campaign.get("type")
    if cancelled is not None:
        params = cancelled["config"]
        opportunity = cancelled["opportunity"]
        campaign_type = opportunity.get("type") or f"campaignType {params.get('campaignType')}"
    settings = (params.get("distributionMethodParameters") or {}).get("distributionSettings") or {}
    reward = campaign.get("rewardToken") or {}
    decimals = int(reward.get("decimals", 18))
    apr = settings.get("targetAPR")
    return {
        "campaign_id": campaign["campaignId"],
        "type": campaign_type,
        # INVALID when Merkl no longer computes it: cancelled (see cancelled_at) or rejected.
        "merkl_type": campaign.get("type"),
        # Merkl pays a cancelled campaign up to its cancel; one INVALID without a cancel, never.
        "status": "invalid"
        if campaign.get("type") == "INVALID" and (cancelled is None or cancelled["cancelled_at"] is None)
        else status_at(campaign["startTimestamp"], campaign["endTimestamp"], now),
        "distribution_chain_id": campaign.get("distributionChainId"),
        "compute_chain_id": campaign.get("computeChainId"),
        "creator": campaign.get("creatorAddress"),
        "target_token": params.get("targetToken"),
        "target_symbol": params.get("symbolTargetToken"),
        "opportunity": opportunity.get("name"),
        "reward_token": reward.get("address"),
        "reward_symbol": reward.get("symbol"),
        "reward_decimals": decimals,
        "amount": format(scaled(campaign["amount"], decimals).normalize(), "f"),
        "amount_raw": str(campaign["amount"]),
        "apr_mode": settings.get("mode"),
        "target_apr": None if apr is None else float(apr),
        "start": iso(campaign["startTimestamp"]),
        "end": iso(campaign["endTimestamp"]),
        "computed_until": iso((campaign.get("campaignStatus") or {}).get("computedUntil")),
        "cancelled_at": None if cancelled is None else iso(cancelled["cancelled_at"]),
        "planned_end": None if cancelled is None else iso(cancelled["config"].get("endTimestamp")),
        "has_overrides": bool(campaign.get("hasOverrides")),
        "hide_leaderboard": bool(campaign.get("hideLeaderboard")),
        "created_at": campaign.get("createdAt"),
        "merkl_url": APP_CAMPAIGN.format(opportunity=campaign.get("opportunityId"), campaign=campaign["campaignId"]),
    }


def borrow_from_siblings(records: list[dict[str, Any]]) -> None:
    """Fill what a history config lacks from a valid campaign on the same target.

    A config names its target by address and its type by number only; a creation config read by
    hash has no opportunity either. A valid campaign on the same target names all three.
    """
    named = {}
    for record in records:
        if record["merkl_type"] != "INVALID" and record["target_token"]:
            named.setdefault(record["target_token"].lower(), record)
    for record in records:
        sibling = named.get((record["target_token"] or "").lower())
        if sibling is None or record["merkl_type"] != "INVALID":
            continue
        record["target_symbol"] = record["target_symbol"] or sibling["target_symbol"]
        record["opportunity"] = record["opportunity"] or sibling["opportunity"]
        if record["type"].startswith("campaignType "):
            record["type"] = sibling["type"]


def matches(record: dict[str, Any], needle: str) -> bool:
    """Whether `needle` occurs, case-insensitively, in the record's names, symbols or addresses."""
    haystack = " ".join(
        str(record[key] or "")
        for key in ("opportunity", "target_symbol", "target_token", "reward_symbol", "campaign_id", "type")
    )
    return needle.lower() in haystack.lower()


def short_time(value: str | None) -> str:
    """An ISO UTC time as `YYYY-MM-DD HH:MM`."""
    return "-" if value is None else value[:16].replace("T", " ")


def format_amount(value: str) -> str:
    """A whole-token amount with thousands separators, two decimals at most."""
    amount = Decimal(value)
    return f"{amount:,.0f}" if amount == amount.to_integral() else f"{amount:,.2f}"


def airdrop_marker(record: dict[str, Any]) -> list[str]:
    """The airdrop's state, for a confidential campaign read with `--airdrops`."""
    drop = record.get("airdrop")
    if drop is None:
        return []
    if not drop["deployed"]:
        return [f"airdrop {drop['address']} not deployed"]
    funding = "funded in full" if drop["funded_in_full"] else f"{format_amount(drop['shortfall'])} short"
    return [
        (
            f"airdrop {drop['address']} funded {format_amount(drop['funded'])} {record['reward_symbol']}"
            f" ({drop['funded_share']}, {funding}), {drop['roots_set']} roots, {drop['claims']} claims"
        )
    ]


def invalid_marker(record: dict[str, Any]) -> list[str]:
    """`cancelled <time>` for a cancelled campaign."""
    if record["cancelled_at"]:
        return [f"cancelled {short_time(record['cancelled_at'])}"]
    return []


def format_line(record: dict[str, Any]) -> str:
    """One campaign as one line; the full campaign id goes last, after a tab."""
    apr = "" if record["target_apr"] is None else f"{record['apr_mode']} {record['target_apr'] * 100:g}%"
    fields = [
        record["status"].upper(),
        record["type"] or "-",
        record["target_symbol"] or "-",
        f"{format_amount(record['amount'])} {record['reward_symbol']}",
        apr or "-",
        f"{short_time(record['start'])} → {short_time(record['end'])} UTC",
        f"scored {short_time(record['computed_until'])}",
        f"created {short_time(record['created_at'])}",
        *invalid_marker(record),
        *airdrop_marker(record),
        record["opportunity"] or "-",
    ]
    return "  ".join(fields) + "\t" + record["campaign_id"]


def format_config(entry: dict[str, Any]) -> str:
    """One config of a campaign's history: when it applied from, type, target, APR and window."""
    config = entry.get("config") or {}
    settings = (config.get("distributionMethodParameters") or {}).get("distributionSettings") or {}
    apr = settings.get("targetAPR")
    kind = (entry.get("Opportunity") or {}).get("type") or f"campaignType {config.get('campaignType')}"
    since = entry.get("overrideTimestamp")
    return "  ".join([
        # Merkl stamps the creation config 0.
        "created" if not since else f"override {short_time(iso(since))}",
        kind,
        config.get("targetToken") or "no target",
        "-" if apr is None else f"{settings.get('mode')} {float(apr) * 100:g}%",
        f"{short_time(iso(config.get('startTimestamp')))} → {short_time(iso(config.get('endTimestamp')))} UTC",
    ])


@dataclass(frozen=True)
class Services:
    """What the commands read through: Merkl, and an RPC per chain id for `--airdrops`."""

    merkl: MerklClient
    chain: Callable[[int], airdrops.Chain]
    # The creator's Merkl API key, or None: without it the budget check counts the whole budget.
    api_key: Callable[[], str | None] = lambda: None
    airdrop_config: Callable[[], airdrops.AirdropConfig] = airdrops.load_airdrop_config


def attach_airdrops(services: Services, records: list[dict[str, Any]]) -> None:
    """Add each confidential record's reward airdrop under `airdrop`; other records get None.

    A deployed airdrop must hold the budget less what Merkl scores to the campaign's creator: that
    part is handed back, never claimed, and kept out of the roots.
    """
    config = services.airdrop_config()
    confidential = [r for r in records if r["type"] in config.campaign_types]
    for record in records:
        record["airdrop"] = None
    found: dict[str, airdrops.Airdrop] = {}
    for chain_id in sorted({r["distribution_chain_id"] for r in confidential}):
        on_chain = [r for r in confidential if r["distribution_chain_id"] == chain_id]
        found |= airdrops.airdrops_for(services.chain(chain_id), on_chain, config.factories)
    deployed = [r for r in confidential if r["campaign_id"] in found and found[r["campaign_id"]].deployed]
    api_key = services.api_key() if deployed else None
    if deployed and api_key is None:
        log.warning(
            "no Merkl API key: each airdrop is checked against its whole budget; set MERKL_API_KEY or "
            "merkl_api_key in %s to net out what Merkl hands back to the creator",
            airdrops.CONFIG_PATH,
        )
    with ThreadPoolExecutor(RESOLVE_WORKERS) as pool:
        rewards = dict(
            zip(
                [r["campaign_id"] for r in deployed],
                pool.map(
                    lambda r: None
                    if api_key is None
                    else services.merkl.creator_reward(r["campaign_id"], r["creator"], r["distribution_chain_id"], api_key),
                    deployed,
                ),
            )
        )
    for record in confidential:
        airdrop = found.get(record["campaign_id"])
        if airdrop is None:
            continue
        record["airdrop"] = drop = airdrop.as_record()
        if not airdrop.deployed:
            continue
        reward = rewards[record["campaign_id"]]
        budget = airdrops.expected_funding(
            int(record["amount_raw"]), record["reward_decimals"], airdrop.underlying_decimals, airdrop.rate
        )
        # Merkl encrypts the creator's leaf rounded down to the wrapper's rate, like every leaf.
        withheld = 0 if reward is None else airdrops.expected_funding(
            reward, record["reward_decimals"], airdrop.underlying_decimals, airdrop.rate
        )
        expected = budget - withheld
        whole = Decimal(10) ** airdrop.underlying_decimals
        drop["creator_reward"] = None if reward is None else format((Decimal(withheld) / whole).normalize(), "f")
        drop["expected"] = format((Decimal(expected) / whole).normalize(), "f")
        drop["shortfall"] = format((Decimal(expected - airdrop.funded_raw) / whole).normalize(), "f")
        drop["funded_in_full"] = airdrop.funded_raw >= expected
        drop["funded_share"] = f"{Decimal(airdrop.funded_raw) / expected:.0%}" if expected else "-"
        if not drop["funded_in_full"]:
            log.warning(
                "airdrop %s of campaign %s holds %s %s of the %s it owes holders: %s short",
                drop["address"],
                record["campaign_id"],
                drop["funded"],
                record["reward_symbol"],
                drop["expected"],
                drop["shortfall"],
            )


STATUS_STYLES = {"live": "bold green", "soon": "cyan", "past": "dim", "invalid": "red"}


def etherscan_link(address: str, chain_id: int | None) -> str:
    """A short address, linked to its explorer page on mainnet."""
    short = f"{address[:6]}…{address[-4:]}"
    return f"[link=https://etherscan.io/address/{address}]{short}[/link]" if chain_id == 1 else short


def airdrop_cell(record: dict[str, Any]) -> str:
    """The airdrop column: blank for a public campaign or without --airdrops."""
    drop = record.get("airdrop")
    if drop is None:
        return ""
    link = etherscan_link(drop["address"], record["distribution_chain_id"])
    if not drop["deployed"]:
        return f"{link} [red]not deployed[/red]"
    handed_back = (
        f", {format_amount(drop['creator_reward'])} back to the creator" if drop.get("creator_reward") not in (None, "0") else ""
    )
    funding = (
        f"[green]funded in full[/green] ({format_amount(drop['funded'])}{handed_back})"
        if drop["funded_in_full"]
        else f"[red]{format_amount(drop['shortfall'])} short[/red] ({drop['funded_share']} of {format_amount(drop['expected'])}{handed_back})"
    )
    return f"{link} {funding}, {drop['claims']} claims, {drop['roots_set']} roots"


def day_or_time(value: str | None) -> str:
    """An ISO UTC time as `YYYY-MM-DD`, with `HH:MM` only when it is not midnight."""
    short = short_time(value)
    return short[:10] if short.endswith("00:00") else short


def render_pretty(records: list[dict[str, Any]], console: Console | None = None) -> None:
    """Records as one table per target token, titled with its side and opportunity, in window order."""
    console = console or Console()
    with_airdrops = any("airdrop" in r for r in records)
    by_target: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_target.setdefault(record["target_symbol"] or record["target_token"] or "no target", []).append(record)
    for target, group in sorted(by_target.items()):
        types = sorted({r["type"] or "?" for r in group})
        settled_by_airdrop = with_airdrops and any(r.get("airdrop") for r in group)
        opportunity = next((r["opportunity"] for r in group if r["opportunity"]), "")
        table = Table(
            title=f"[bold]{target}[/bold] · {', '.join(types)} · [dim]{opportunity}[/dim]",
            title_justify="left",
            header_style="bold",
        )
        for column in ("Status", "Budget", "APR", "Window (UTC)", "Created", "Campaign"):
            table.add_column(column, no_wrap=True, justify="right" if column in ("Budget", "APR") else "left")
        if settled_by_airdrop:
            table.add_column("Airdrop", no_wrap=True)
        for record in sorted(group, key=lambda r: (r["start"] or "", r["created_at"] or "")):
            status = record["status"]
            label = f"[{STATUS_STYLES.get(status, '')}]{status}[/]"
            if record["cancelled_at"]:
                label = f"[yellow]cancelled {day_or_time(record['cancelled_at'])}[/yellow]"
            row = [
                label,
                f"{format_amount(record['amount'])} {record['reward_symbol']}",
                "" if record["target_apr"] is None else f"{record['target_apr'] * 100:g}%",
                f"{day_or_time(record['start'])} → {day_or_time(record['end'])}",
                short_time(record["created_at"])[:10],
                f"[link={record['merkl_url']}]{record['campaign_id'][:10]}[/link]",
            ]
            if settled_by_airdrop:
                row.append(airdrop_cell(record))
            table.add_row(*row)
        console.print(table)
        console.print()


def emit(records: Iterable[dict[str, Any]]) -> None:
    """Pretty JSON on stdout."""
    click.echo(json.dumps(list(records), indent=2, ensure_ascii=False))


def is_hex(value: str, size: int) -> bool:
    """Whether `value` is 0x followed by `size` bytes of hex."""
    return value.startswith("0x") and len(value) == 2 + 2 * size and all(c in "0123456789abcdefABCDEF" for c in value[2:])


def address(_ctx, _param, values: tuple[str, ...]) -> tuple[str, ...]:
    """Click callback: each value is a 0x-prefixed 20-byte hex address."""
    for value in values:
        if not is_hex(value, 20):
            raise click.BadParameter(f"{value!r} is not an address; pass 0x followed by 40 hex characters")
    return values


def campaign_id(_ctx, _param, values: tuple[str, ...]) -> tuple[str, ...]:
    """Click callback: each value is an on-chain campaign id, 0x followed by 64 hex characters."""
    for value in values:
        if not is_hex(value, 32):
            raise click.BadParameter(
                f"{value!r} is not a campaign id; pass the 0x… hash of 64 hex characters that `merkl campaigns` prints last"
            )
    return values


@click.group(
    epilog="\b\nExamples:\n  merkl campaigns --help\n  merkl campaigns --creator <creator> --match <text>\n  merkl campaign <campaign-id>",
)
@click.option("--debug", is_flag=True, help="Log HTTP calls to stderr.")
@click.option("--rpc-url", help="RPC for --airdrops. Default: $ETH_RPC_URL, then ~/.config/merkl-cli/config.json.")
@click.pass_context
def cli(ctx: click.Context, debug: bool, rpc_url: str | None) -> None:
    """Read Merkl incentive campaigns. Read-only; no authentication."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING, stream=sys.stderr, format="%(levelname)s %(message)s"
    )
    # Tests pass their own services through `obj`.
    ctx.obj = ctx.obj or Services(
        merkl=MerklClient(),
        chain=lambda chain_id: airdrops.Chain(airdrops.resolve_rpc(chain_id, rpc_url, dict(os.environ))),
        api_key=lambda: airdrops.resolve_api_key(dict(os.environ)),
    )


@cli.command(
    epilog="\b\nExamples:\n"
    "  merkl campaigns --creator <creator> --match <text>\n"
    "  merkl campaigns --creator <creator> --match <text> --airdrops --pretty\n"
    "  merkl campaigns --target <token> --target <other-token> --status live\n"
    "  merkl campaigns --creator <creator> --type <TYPE> --chain 1 --json | jq -r '.[].campaign_id'\n"
    "  merkl campaigns --creator <creator> --created-after 2026-10-01\n"
    "\n"
    "Addresses are 0x + 40 hex characters; <TYPE> is a Merkl campaign type.",
)
@click.option("--creator", multiple=True, callback=address, help="Creator address. Repeatable.")
@click.option("--target", multiple=True, callback=address, help="Token whose holders are rewarded (Merkl's mainParameter). Repeatable.")
@click.option("--chain", type=int, help="Distribution chain id, such as 1.")
@click.option("--status", "statuses", multiple=True, type=click.Choice(STATUSES), help="live, soon or past. Repeatable. Default: all.")
@click.option("--type", "campaign_type", help="Merkl campaign type, such as ERC20LOGPROCESSOR.")
@click.option("--match", "needle", help="Case-insensitive text in the opportunity name, target or reward token (client-side).")
@click.option("--created-after", type=click.DateTime(["%Y-%m-%d", "%Y-%m-%dT%H:%M"]), help="UTC date or date-time.")
@click.option("--with-subcampaigns", is_flag=True, help="Keep Merkl's sub-campaigns.")
@click.option("--limit", type=int, default=0, help="Show at most N campaigns, newest first. 0: all.")
@click.option("--airdrops", "with_airdrops", is_flag=True, help="Read each confidential campaign's reward airdrop on chain.")
@click.option("--json", "as_json", is_flag=True, help="Normalized JSON.")
@click.option("--raw", is_flag=True, help="Merkl's records, untouched.")
@click.option("--pretty", is_flag=True, help="A table for a human: a section per target token, colours, short ids.")
@click.pass_obj
def campaigns(
    services: Services,
    creator: tuple[str, ...],
    target: tuple[str, ...],
    chain: int | None,
    statuses: tuple[str, ...],
    campaign_type: str | None,
    needle: str | None,
    created_after: datetime | None,
    with_subcampaigns: bool,
    limit: int,
    with_airdrops: bool,
    as_json: bool,
    raw: bool,
    pretty: bool,
) -> None:
    """List campaigns, newest first, filtered by creator, target, chain, status, type or text."""
    merkl = services.merkl
    if not (creator or target or chain or campaign_type):
        raise click.UsageError(
            "too broad: pass --creator, --target, --chain or --type\n"
            "  merkl campaigns --creator <creator-address>"
        )
    base: dict[str, Any] = {"withOpportunity": "true", "excludeSubCampaigns": str(not with_subcampaigns).lower()}
    if creator:
        base["creatorAddresses"] = list(creator)
    if chain is not None:
        base["distributionChainIds"] = chain
    if statuses:
        base["status"] = ",".join(s.upper() for s in statuses)
    if created_after is not None:
        base["createdAfter"] = int(created_after.replace(tzinfo=UTC).timestamp() * 1000)
    typed = {**base, "type": campaign_type} if campaign_type else base
    # Merkl takes one mainParameter per request.
    listed = [c for t in target for c in merkl.campaigns({**typed, "mainParameter": t})] if target else merkl.campaigns(typed)
    # A cancelled campaign keeps neither its type nor its target on Merkl: only its creator finds it.
    if creator and (target or campaign_type not in (None, "INVALID")):
        listed += merkl.campaigns({**base, "type": "INVALID"})
    elif target or campaign_type not in (None, "INVALID"):
        log.warning("cancelled campaigns are listed under their creator only; add --creator to include them")
    cancelled = merkl.resolve_cancelled(listed)
    now = int(time.time())
    pairs = [(normalize(c, now, cancelled.get(c["campaignId"])), c) for c in listed]
    borrow_from_siblings([r for r, _ in pairs])
    if target:
        wanted = {t.lower() for t in target}
        pairs = [p for p in pairs if (p[0]["target_token"] or "").lower() in wanted]
    if campaign_type not in (None, "INVALID"):
        pairs = [p for p in pairs if p[0]["type"] == campaign_type]
    if statuses:
        # Merkl calls a rejected INVALID campaign live within its window; it pays nothing.
        pairs = [p for p in pairs if p[0]["status"] in statuses]
    if needle:
        pairs = [p for p in pairs if matches(p[0], needle)]
    pairs.sort(key=lambda p: (p[0]["created_at"] or "", p[0]["campaign_id"]), reverse=True)
    if limit:
        pairs = pairs[:limit]
    if with_airdrops:
        attach_airdrops(services, [r for r, _ in pairs])
    log.info("%d campaigns", len(pairs))
    if raw:
        emit(c for _, c in pairs)
    elif as_json:
        emit(r for r, _ in pairs)
    elif pretty:
        render_pretty([r for r, _ in pairs])
    else:
        for record, _ in pairs:
            click.echo(format_line(record))


@cli.command(
    epilog="\b\nExamples:\n"
    "  merkl campaign <campaign-id>\n"
    "  merkl campaign <campaign-id> <other-campaign-id> --chain 1 --json\n"
    "  merkl campaign <campaign-id> --airdrops\n"
    "  merkl campaign <campaign-id> --raw | jq '.[0].params'\n"
    "\n"
    "A campaign id is 0x + 64 hex characters, printed last by `merkl campaigns`.",
)
@click.argument("campaign_ids", nargs=-1, required=True, callback=campaign_id)
@click.option("--chain", type=int, help="Distribution chain id, when the id exists on several chains.")
@click.option("--airdrops", "with_airdrops", is_flag=True, help="Read each confidential campaign's reward airdrop on chain.")
@click.option("--json", "as_json", is_flag=True, help="Normalized JSON.")
@click.option("--raw", is_flag=True, help="Merkl's records, untouched.")
@click.pass_obj
def campaign(
    services: Services, campaign_ids: tuple[str, ...], chain: int | None, with_airdrops: bool, as_json: bool, raw: bool
) -> None:
    """Show one or more campaigns by on-chain campaign id (the 0x… hash, not Merkl's internal id)."""
    merkl = services.merkl
    params: dict[str, Any] = {"campaignIds": list(campaign_ids), "withOpportunity": "true"}
    if chain is not None:
        params["distributionChainIds"] = chain
    found = merkl.campaigns(params)
    missing = sorted({i.lower() for i in campaign_ids} - {c["campaignId"].lower() for c in found})
    if missing:
        raise click.ClickException(f"no campaign {', '.join(missing)} on Merkl" + ("" if chain is None else f" chain {chain}"))
    now = int(time.time())
    if raw:
        emit(found)
        return
    cancelled = merkl.resolve_cancelled(found)
    records = [normalize(c, now, cancelled.get(c["campaignId"])) for c in found]
    if with_airdrops:
        attach_airdrops(services, records)
    if as_json:
        emit(records)
        return
    for record, source in zip(records, found):
        settings = (source.get("params") or {}).get("distributionMethodParameters") or {}
        status = source.get("campaignStatus") or {}
        click.echo(record["campaign_id"])
        for key, value in record.items():
            if key not in ("campaign_id", "airdrop") and value not in (None, ""):
                click.echo(f"  {key}: {value}")
        if record.get("airdrop"):
            click.echo("  airdrop:")
            for key, value in record["airdrop"].items():
                click.echo(f"    {key}: {value}")
        click.echo(f"  distribution: {json.dumps(settings, separators=(',', ':'))}")
        error = f" error: {status['error']}" if status.get("error") else ""
        click.echo(f"  compute: {status.get('status')} (delay {status.get('delay')} s){error}")
        if source.get("hasOverrides"):
            click.echo("  history:")
            for entry in merkl.history(source):
                click.echo(f"    {format_config(entry)}")


if __name__ == "__main__":
    cli(prog_name="merkl")
