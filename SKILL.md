---
name: merkl-cli
description: >-
  Read Merkl incentive campaigns from the terminal with the bundled `merkl`
  command, over Merkl's public v4 API. Use to list the campaigns a wallet
  created, the campaigns on a token, their budgets, APR targets, windows,
  scoring progress, overrides and cancellations, or to show one campaign in
  full; and with --airdrops, whether a campaign that settles through a
  confidential Merkle airdrop was really paid: the airdrop on chain, the real
  tokens wrapped into it, its roots and claims. Read-only. Every read supports
  --json and --raw, and `campaigns` a --pretty table. Triggers - "merkl
  campaigns", "which campaigns did we create", "campaigns on this token",
  "merkl campaign 0x…", "is Merkl scoring it yet", "was this campaign
  cancelled", "was this campaign paid", "is the airdrop funded".
allowed-tools:
  - Bash
  - Read
---

# Merkl CLI

Read-only access to Merkl campaigns through `https://api.merkl.xyz/v4`.

## How to invoke

Invoke it as **`merkl`**, on `$PATH` through a symlink in `~/.local/bin` onto
this repo's `bin/merkl`, so a `git pull` or an uncommitted edit takes effect at
once. It is a PEP 723 `uv` script; dependencies resolve on first run. If `merkl`
is missing, run `<skill-dir>/bin/merkl` or link it:

```bash
ln -sfn <skill-dir>/bin/merkl ~/.local/bin/merkl
```

## Configuration

Listing campaigns needs nothing: Merkl serves every public campaign without a
key. `--airdrops` reads `~/.config/merkl-cli/config.json`:

```json
{
  "rpc": {"1": "<rpc-url or op://vault/item/field>"},
  "merkl_api_key": "<creator api key or op://vault/item/field>",
  "airdrops": {
    "campaign_types": ["<MERKL_TYPE>"],
    "factories": {"1": "<airdrop-factory-address>"}
  }
}
```

- `airdrops` (required): the Merkl campaign types that settle through a
  confidential airdrop, and the airdrop factory Merkl derives their airdrops
  from, per chain id.
- `rpc` (required): `--rpc-url` and `$ETH_RPC_URL` take precedence. Public RPCs
  do not work: they cap `eth_getLogs` at 50 to 10k blocks or refuse history
  without a token (measured 2026-10). Logs are read in 10k-block chunks.
- `merkl_api_key` (optional, or `$MERKL_API_KEY`): the campaign creator's key,
  sent as `X-API-Key`. Without it the budget check compares each airdrop with
  its whole budget and says so on stderr.
- A value starting with `op://` is read with `op read` (1Password CLI).

## Output conventions

- Default: one line per campaign, newest first: status, type, target symbol,
  budget, APR target, window (UTC), how far Merkl has scored it, creation time,
  opportunity name, then a tab and the **full campaign id**.
- `--json`: normalized records. Amounts in whole tokens (`amount`) and base units
  (`amount_raw`), times as UTC ISO strings, `target_apr` as a fraction (0.08 is
  8%). `status` is `live`, `soon` or `past` from the window, or `invalid` for a
  campaign Merkl lists as INVALID without ever having paid it. `merkl_type` is
  the type Merkl lists now; `type` is the real one (see cancelled campaigns
  below). A cancelled campaign carries `cancelled_at` and `planned_end`.
  `merkl_url` is the campaign's page in Merkl's app.
- `--raw`: Merkl's records untouched, with the opportunity attached.
- `--pretty` (on `campaigns`): for a human at a terminal, never for parsing. One
  table per target token, titled with its type and opportunity, rows in window
  order, colours, short ids; each campaign id links to its Merkl page, each
  airdrop address to Etherscan (terminal hyperlinks, as in iTerm2).
- stdout carries data only; warnings and `--debug` HTTP logs go to stderr.

## Commands

```bash
# Every campaign a wallet created, filtered by text in the opportunity name or token symbols
merkl campaigns --creator <creator> --match <text>
# Campaigns on one or more tokens (Merkl's mainParameter)
merkl campaigns --target <token> --target <other-token>
# Server-side filters: --chain, --status live|soon|past (repeatable), --type, --created-after
merkl campaigns --creator <creator> --status live --type <MERKL_TYPE>
merkl campaigns --creator <creator> --created-after 2026-10-01
merkl campaigns --creator <creator> --json | jq -r '.[].campaign_id'
# One or more campaigns in full, by on-chain campaign id (batch them in one call)
merkl campaign <campaign-id> <other-campaign-id>
merkl campaign <campaign-id> --raw | jq '.[0].params'
# The same listing as tables for a human to read
merkl campaigns --creator <creator> --match <text> --airdrops --pretty
# Was each airdrop-settled campaign paid? Its airdrop, read on chain
merkl campaigns --creator <creator> --type <MERKL_TYPE> --airdrops
merkl campaign <campaign-id> --airdrops
# The live airdrop-settled campaigns no holder can claim yet
merkl campaigns --creator <creator> --type <MERKL_TYPE> --status live --airdrops --json \
  | jq -r '.[] | select(.airdrop.deployed | not) | .campaign_id'
```

Addresses are `0x` + 40 hex characters, campaign ids `0x` + 64.

With `--airdrops`, each record of a configured type gains an `airdrop` object:
`address`, `deployed`, `root`, `roots_set` (the first root and each
replacement), `token` (the confidential wrapper), `underlying` (the real token),
`funded` / `funded_raw` (real tokens wrapped into it), `wraps`, `claims`,
`claimants`, `paused`, and the budget check: `creator_reward` (what Merkl scores
back to the campaign's creator), `expected` (what holders can be owed: the
budget less `creator_reward`, both in the real token and rounded down to a
multiple of the wrapper's `rate()`, the only amounts the wrapper takes),
`shortfall` (`expected − funded`; negative means over), `funded_in_full` and
`funded_share`. Every deployed airdrop short of `expected` is also listed on
stderr. Other records get `airdrop: null`. In text, the line ends with
`airdrop <address> funded <n> <symbol> (<share>, funded in full | <n> short),
<roots> roots, <claims> claims` or `airdrop <address> not deployed`.

`campaigns` refuses to run without `--creator`, `--target`, `--chain` or
`--type`: Merkl lists tens of thousands of campaigns.

## Recipes

| Question | Command |
| --- | --- |
| What did we create on one vault, under any type? | `merkl campaigns --creator <creator> --match <vault name>` |
| What did we create today? | `merkl campaigns --creator <creator> --created-after <date>` |
| Is Merkl scoring a new campaign yet? | `merkl campaign <id>`: `computed_until` still at `start` means not yet |
| Was a campaign edited or cancelled? | `merkl campaign <id>`: `history:` lists the creation config and every override |
| What did a cancelled campaign pay? | its window ends at the cancel: `start` → `end`, against `planned_end` |
| Was an airdrop-settled campaign paid? | `merkl campaign <id> --airdrops`: `deployed`, `funded_in_full`, `claims` |
| Which of them are still unpaid? | the last command under Commands |
| Do two campaigns on paired tokens match? | `merkl campaigns --target <token> --target <paired-token> --status live` |

## Gotchas

- **Merkl's `search` parameter does not match opportunity names**, so `--match`
  filters client-side on the opportunity name, target and reward symbols, target
  address, type and campaign id.
- **Cancelled campaigns are listed as `INVALID`, and they did pay.** Cancelling
  overrides a campaign with a target-less config that ends it about a day later.
  From then on Merkl lists it as type `INVALID` under "Invalid Campaigns
  <creator>", with no target, while it paid out until the cancel. The CLI reads
  each `INVALID` campaign back from `/v4/overrides/{id}/previous-configs` (or its
  creation config at `/v4/config/hash/{campaignEncodingHash}`) and reports its
  last valid type, target, APR and opportunity, `cancelled_at` and
  `planned_end`. Names a history config lacks (symbol, opportunity, type name)
  are taken from a valid campaign on the same target in the same listing.
- **Only the creator finds a cancelled campaign.** Merkl's `type` and
  `mainParameter` filters see `INVALID`, not the original type or target, so
  `--type` and `--target` add the creator's `INVALID` campaigns when `--creator`
  is given, and warn when it is not.
- An `INVALID` campaign with no override in its history was never paid by Merkl:
  `status: invalid`, `dailyRewards` 0, scored straight to its end.
- **An airdrop-settled campaign's budget can be a mock.** Such a campaign pays in
  a Merkl mock of the reward token, so Merkl's amounts and statuses say nothing
  about payment. Holders are paid by a TokenOps `MerkleConfidentialAirdrop`
  clone at the address Merkl derives from the factory, the campaign's creator
  and the campaign id (EIP-1167 clone, CREATE2 salt
  `keccak256(abi.encode(creator, campaignId))`), the same address Merkl encrypts
  the rewards for. `--airdrops` reads that address: `not deployed` means no
  holder can claim yet; `funded` is the sum of plaintext `Wrap` events into it,
  since the balance itself is encrypted.
- **Merkl hands the unspent budget back to the creator.** When a fixed-APR
  campaign ends below its budget, Merkl scores the rest to its creator as a
  reward (`reason: end_campaign` on `/v4/rewards/campaign/{id}/mine`). The
  creator never claims it from the airdrop, so an airdrop funded net of it is
  funded in full; that is why `expected` subtracts it.
- The leaderboards of confidential campaigns are hidden: `/v4/rewards` answers
  401 "Leaderboard is hidden" without the creator's API key.
- A campaign id is the on-chain hash (`0x` + 64 hex), unique per distribution
  chain. Merkl's numeric `id` is internal; the CLI never takes it.
- `--raw` amounts are base units of the reward token. A Merkl mock reward token
  has the real token's decimals.
- Measured 2026-10: a 100-item page answers in about 0.2 s, with the opportunity
  attached for about 50 ms more. The listing rate limit is 4200 requests a minute
  (`x-ratelimit-limit`).
