# merkl-cli

**Visibility: public.**

Read [Merkl](https://merkl.xyz) incentive campaigns from the terminal: who
created them, what they reward, their budgets, APR targets, windows, scoring
progress, overrides and cancellations. Ships one command, `merkl`. Read-only,
over Merkl's public v4 API, no key needed.

It also reads, on chain, the confidential Merkle airdrop that pays a campaign
settling through one, to tell whether holders were really paid.

## Install

`merkl` is a [PEP 723](https://peps.python.org/pep-0723/) `uv` script, so its
dependencies resolve on first run:

```bash
git clone https://github.com/ClementWalter/merkl-cli.git
ln -sfn "$PWD/merkl-cli/bin/merkl" ~/.local/bin/merkl
```

Requires [uv](https://docs.astral.sh/uv/).

## Use

```bash
merkl campaigns --creator <creator-address> --match <text>
merkl campaigns --creator <creator-address> --match <text> --pretty
merkl campaigns --target <token-address> --status live --json
merkl campaign <campaign-id>
```

Every command takes `--json` (normalized) and `--raw` (Merkl's answer);
`campaigns` also takes `--pretty`, a table per target token.

`--airdrops` reads each airdrop-settled campaign's airdrop on chain: whether it
exists, how much of the real token was wrapped into it against what holders can
be owed, its roots and claims. It needs `~/.config/merkl-cli/config.json`:

```json
{
  "rpc": {"1": "<rpc-url>"},
  "merkl_api_key": "<creator api key, optional>",
  "airdrops": {"campaign_types": ["<MERKL_TYPE>"], "factories": {"1": "<factory-address>"}}
}
```

See [SKILL.md](SKILL.md) for every option and the API's traps.

## Test

```bash
uv run tests/test_merkl_cli.py
uv run tests/test_airdrops.py
```
