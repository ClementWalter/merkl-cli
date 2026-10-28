"""The reward airdrop that pays a confidential Merkl campaign, read on chain.

Some Merkl campaign types pay in a Merkl mock of the reward token and settle through a
confidential (ERC-7984) Merkle airdrop instead, so Merkl's data says nothing about real
payment. Holders are paid by a TokenOps `MerkleConfidentialAirdrop` clone whose address
Merkl itself derives from a reward-airdrop factory, the campaign's creator and the
campaign id, and binds its encrypted rewards to. Which campaign types settle this way,
and through which factory on each chain, is configuration (`airdrops` in the config file, or
`$MERKL_AIRDROP_TYPES` and `$MERKL_AIRDROP_FACTORIES`). That clone is what this module reads: whether it exists, its
Merkle root, how much of the real token was wrapped into it (plaintext `Wrap` events
on its confidential token), how many roots it has had and how many claims it served.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import click
import requests
from Crypto.Hash import keccak

log = logging.getLogger("merkl-cli")

# Most providers' free tiers cap eth_getLogs at 10k blocks.
LOG_CHUNK = 10_000
RETRIES = 3
TIMEOUT_SECONDS = 20
# Airdrops read at once: each is about ten RPC calls.
READ_WORKERS = 4
CONFIG_PATH = Path.home() / ".config" / "merkl-cli" / "config.json"
# Where a Claude Desktop user sets the values that the config file holds in a terminal.
SETTINGS_HINT = "the Merkl extension's settings (Claude Desktop → Settings → Extensions → Merkl)"


def keccak256(data: bytes) -> bytes:
    """Keccak-256 of `data`."""
    return keccak.new(digest_bits=256, data=data).digest()


def selector(signature: str) -> str:
    """A function's 4-byte selector as hex."""
    return "0x" + keccak256(signature.encode()).hex()[:8]


def topic(signature: str) -> str:
    """An event's topic 0."""
    return "0x" + keccak256(signature.encode()).hex()


def word(address: str) -> str:
    """An address left-padded to a 32-byte topic."""
    return "0x" + address[2:].lower().rjust(64, "0")


WRAP = topic("Wrap(address,uint256,bytes32)")
ROOT_SET = topic("MerkleRootSet(bytes32,bytes32)")
ROOT_INITIALIZED = topic("MerkleAirdropInitialized(bytes32,bool)")
CLAIMED = topic("Claimed(address,address,bytes32)")
CLAIMED_AND_UNWRAP = topic("ClaimedAndUnwrapInitiated(address,address,bytes32,bytes32)")


def predict_airdrop(factory: str, implementation: str, creator: str, campaign_id: str) -> str:
    """The EIP-1167 clone address the factory deploys for `campaign_id` created by `creator`.

    The salt is `keccak256(abi.encode(creator, campaignId))`, the CREATE2 deployer the factory.
    """
    init_code = bytes.fromhex(
        "3d602d80600a3d3981f3363d3d373d3d3d363d73" + implementation[2:].lower() + "5af43d82803e903d91602b57fd5bf3"
    )
    salt = keccak256(bytes.fromhex(creator[2:].lower().rjust(64, "0") + campaign_id[2:].lower()))
    digest = keccak256(b"\xff" + bytes.fromhex(factory[2:]) + salt + keccak256(init_code))
    return checksum("0x" + digest[12:].hex())


def checksum(address: str) -> str:
    """The EIP-55 checksummed form of `address`."""
    lower = address[2:].lower()
    digest = keccak256(lower.encode()).hex()
    return "0x" + "".join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(lower))


def setting(env: Mapping[str, str], name: str) -> str | None:
    """`env[name]` when set; Claude Desktop passes an unfilled optional setting as its raw `${user_config.…}` template."""
    value = (env.get(name) or "").strip()
    return None if not value or value.startswith("${") else value


def configured(*keys: str) -> str | None:
    """The value at `keys` in the config file, if the file and the value exist."""
    if not CONFIG_PATH.is_file():
        return None
    value: Any = json.loads(CONFIG_PATH.read_text())
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, str) and value else None


def reveal(value: str) -> str:
    """`value`, or the secret it names when it is a 1Password reference (`op://…`)."""
    if not value.startswith("op://"):
        return value
    if shutil.which("op") is None:
        raise click.ClickException(f"{CONFIG_PATH} names {value}, and the 1Password CLI `op` is not installed")
    read = subprocess.run(["op", "read", value], capture_output=True, text=True, check=False)
    if read.returncode != 0:
        raise click.ClickException(f"op read {value} failed: {read.stderr.strip()}")
    return read.stdout.strip()


def resolve_rpc(chain_id: int, flag: str | None, env: dict[str, str]) -> str:
    """The RPC URL for `chain_id`: `--rpc-url`, then `$ETH_RPC_URL`, then the config file."""
    if flag:
        return flag
    if setting(env, "ETH_RPC_URL"):
        return setting(env, "ETH_RPC_URL")
    value = configured("rpc", str(chain_id))
    if value is None:
        raise click.UsageError(
            f"--airdrops reads chain {chain_id} and needs an RPC: fill 'Ethereum RPC URL' in {SETTINGS_HINT}, "
            "pass --rpc-url, set ETH_RPC_URL, or add\n"
            f'  {{"rpc": {{"{chain_id}": "<url or op://vault/item/field>"}}}}  to {CONFIG_PATH}'
        )
    return reveal(value)


@dataclass(frozen=True)
class AirdropConfig:
    """Which campaign types settle through a confidential airdrop, and its factory on each chain."""

    campaign_types: frozenset[str]
    factories: dict[int, str]


AIRDROP_CONFIG_EXAMPLE = '{"airdrops": {"campaign_types": ["<MERKL_TYPE>"], "factories": {"1": "<factory-address>"}}}'


def parse_factories(value: str) -> dict[str, str]:
    """`1:0xabc…,8453:0xdef…` as `{"1": "0xabc…", "8453": "0xdef…"}`."""
    pairs = [item.split(":", 1) for item in value.split(",") if item.strip()]
    if not pairs or any(len(pair) != 2 for pair in pairs):
        raise click.UsageError(f"MERKL_AIRDROP_FACTORIES is {value!r}; write it as <chain-id>:<factory-address>, comma-separated")
    return {chain.strip(): address.strip() for chain, address in pairs}


def load_airdrop_config(env: Mapping[str, str] | None = None) -> AirdropConfig:
    """`$MERKL_AIRDROP_TYPES` and `$MERKL_AIRDROP_FACTORIES`, else the config file's `airdrops` section."""
    env = os.environ if env is None else env
    types, factories = setting(env, "MERKL_AIRDROP_TYPES"), setting(env, "MERKL_AIRDROP_FACTORIES")
    if types and factories:
        section: Any = {"campaign_types": [t.strip() for t in types.split(",") if t.strip()], "factories": parse_factories(factories)}
    else:
        section = json.loads(CONFIG_PATH.read_text()).get("airdrops") if CONFIG_PATH.is_file() else None
    if not (isinstance(section, dict) and section.get("campaign_types") and section.get("factories")):
        raise click.UsageError(
            "--airdrops needs to know which campaigns settle through an airdrop, and its factory: fill "
            f"'Airdrop campaign types' and 'Airdrop factories' in {SETTINGS_HINT}, set MERKL_AIRDROP_TYPES and "
            f"MERKL_AIRDROP_FACTORIES, or add\n  {AIRDROP_CONFIG_EXAMPLE}  to {CONFIG_PATH}"
        )
    return AirdropConfig(
        campaign_types=frozenset(section["campaign_types"]),
        factories={int(chain): checksum(address) for chain, address in section["factories"].items()},
    )


def resolve_api_key(env: dict[str, str]) -> str | None:
    """The Merkl API key: `$MERKL_API_KEY`, then `merkl_api_key` in the config file; None without."""
    value = setting(env, "MERKL_API_KEY") or configured("merkl_api_key")
    return None if value is None else reveal(value)


class Chain:
    """JSON-RPC reads on one chain: eth_call, chunked eth_getLogs, bounded retries."""

    def __init__(self, url: str, session: requests.Session | None = None):
        self.url = url
        self.session = session or requests.Session()

    def request(self, method: str, params: list[Any]) -> Any:
        """One JSON-RPC call; an RPC error is one line, a transport failure is retried."""
        for attempt in range(1, RETRIES + 1):
            try:
                resp = self.session.post(
                    self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=TIMEOUT_SECONDS
                )
            except requests.RequestException as exc:
                if attempt == RETRIES:
                    raise click.ClickException(f"RPC unreachable: {exc}") from exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == RETRIES:
                    raise click.ClickException(f"RPC {resp.status_code} on {method} after {RETRIES} tries")
                time.sleep(2**attempt)
                continue
            body = resp.json()
            if "error" in body:
                raise click.ClickException(f"RPC {method}: {body['error'].get('message')}")
            return body["result"]
        raise AssertionError("unreachable")

    def chain_id(self) -> int:
        """The chain the RPC serves."""
        return int(self.request("eth_chainId", []), 16)

    def head(self) -> int:
        """The latest block number."""
        return int(self.request("eth_blockNumber", []), 16)

    def has_code(self, address: str) -> bool:
        """Whether a contract is deployed at `address`."""
        return self.request("eth_getCode", [address, "latest"]) not in ("0x", "0x0")

    def call(self, to: str, signature: str) -> str:
        """The 32-byte result word of a no-argument view, as hex."""
        return self.request("eth_call", [{"to": to, "data": selector(signature)}, "latest"])

    def address_of(self, to: str, signature: str) -> str:
        """A no-argument view returning an address."""
        return checksum("0x" + self.call(to, signature)[-40:])

    def uint_of(self, to: str, signature: str) -> int:
        """A no-argument view returning a uint."""
        return int(self.call(to, signature), 16)

    def logs(self, address: str, topics: list[Any], from_block: int, to_block: int) -> list[dict[str, Any]]:
        """Every log matching `topics` on `address`, read in chunks of `LOG_CHUNK` blocks."""
        found = []
        for start in range(from_block, to_block + 1, LOG_CHUNK):
            end = min(start + LOG_CHUNK - 1, to_block)
            found += self.request(
                "eth_getLogs", [{"address": address, "topics": topics, "fromBlock": hex(start), "toBlock": hex(end)}]
            )
        return found


@dataclass(frozen=True)
class Airdrop:
    """What a confidential campaign's reward airdrop holds and did, read at one head."""

    address: str
    deployed: bool
    root: str | None = None
    roots_set: int = 0
    token: str | None = None
    underlying: str | None = None
    underlying_decimals: int = 18
    # The wrapper wraps only multiples of `rate` base units of the underlying.
    rate: int = 1
    funded_raw: int = 0
    wraps: int = 0
    claims: int = 0
    claimants: int = 0
    paused: bool = False

    def as_record(self) -> dict[str, Any]:
        """The airdrop as JSON fields; `funded` in whole tokens of the underlying."""
        return {
            "address": self.address,
            "deployed": self.deployed,
            "root": self.root,
            "roots_set": self.roots_set,
            "token": self.token,
            "underlying": self.underlying,
            "funded": format(Decimal(self.funded_raw).scaleb(-self.underlying_decimals).normalize(), "f"),
            "funded_raw": str(self.funded_raw),
            "wraps": self.wraps,
            "claims": self.claims,
            "claimants": self.claimants,
            "paused": self.paused,
        }


def expected_funding(budget_raw: int, budget_decimals: int, underlying_decimals: int, rate: int) -> int:
    """What an airdrop must hold to pay its whole budget, in base units of the real token.

    The budget is in the Merkl mock's units; the real token may count decimals differently. The
    wrapper takes only multiples of its `rate`, so a remainder below one is never sent.
    """
    scaled = budget_raw * 10**underlying_decimals // 10**budget_decimals
    return scaled - scaled % rate


def read_airdrop(chain: Chain, address: str, head: int) -> Airdrop:
    """The airdrop at `address`: its root, its funding in the real token, its roots and claims."""
    if not chain.has_code(address):
        return Airdrop(address=address, deployed=False)
    deployed_at = chain.uint_of(address, "deploymentBlockNumber()")
    token = chain.address_of(address, "token()")
    root = chain.call(address, "merkleRoot()")
    wraps = chain.logs(token, [WRAP, word(address)], deployed_at, head)
    events = chain.logs(address, [[ROOT_INITIALIZED, ROOT_SET, CLAIMED, CLAIMED_AND_UNWRAP]], deployed_at, head)
    claims = [e for e in events if e["topics"][0] in (CLAIMED, CLAIMED_AND_UNWRAP)]
    underlying = chain.address_of(token, "underlying()")
    return Airdrop(
        address=address,
        deployed=True,
        root=None if int(root, 16) == 0 else root,
        roots_set=sum(1 for e in events if e["topics"][0] in (ROOT_INITIALIZED, ROOT_SET)),
        token=token,
        underlying=underlying,
        underlying_decimals=chain.uint_of(underlying, "decimals()"),
        rate=chain.uint_of(token, "rate()"),
        funded_raw=sum(int(e["data"][2:66], 16) for e in wraps),
        wraps=len(wraps),
        claims=len(claims),
        claimants=len({e["topics"][1] for e in claims}),
        paused=chain.uint_of(address, "paused()") == 1,
    )


def airdrops_for(chain: Chain, campaigns: Iterable[dict[str, Any]], factories: dict[int, str]) -> dict[str, Airdrop]:
    """Each confidential campaign's airdrop, by campaign id, on the chain `chain` serves."""
    served = chain.chain_id()
    factory = factories.get(served)
    if factory is None:
        raise click.ClickException(f"no airdrop factory configured on chain {served}; configured: {sorted(factories)}")
    implementation = chain.address_of(factory, "merkleImplementation()")
    head = chain.head()
    here = []
    for record in campaigns:
        if record["distribution_chain_id"] == served:
            here.append(record)
        else:
            log.warning("campaign %s pays on chain %s; the RPC serves %s", record["campaign_id"], record["distribution_chain_id"], served)
    addresses = [predict_airdrop(factory, implementation, r["creator"], r["campaign_id"]) for r in here]
    with ThreadPoolExecutor(READ_WORKERS) as pool:
        read = list(pool.map(lambda a: read_airdrop(chain, a, head), addresses))
    return {r["campaign_id"]: airdrop for r, airdrop in zip(here, read)}
