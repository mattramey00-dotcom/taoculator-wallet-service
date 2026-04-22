import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import bittensor as bt

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

subtensor = None

def get_subtensor():
    """Return a cached bt.Subtensor client.

    Defaults to the public finney entrypoint. If SUBTENSOR_URL is set in the
    environment, use that instead — this lets us swap to a more reliable
    provider (e.g. OnFinality's authenticated WSS endpoint with its 400k/day
    free-tier quota) without code changes. Format expected:
        wss://apikey-<KEY>@bittensor-finney.api.onfinality.io/public-ws
    or any other fully-qualified WSS/HTTPS substrate endpoint.
    """
    global subtensor
    if subtensor is None:
        network = os.environ.get("SUBTENSOR_URL", "").strip() or "finney"
        subtensor = bt.Subtensor(network=network)
    return subtensor

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/all-subnets")
async def all_subnets_endpoint():
    """Pool reserves + price for every active subnet, straight from chain.

    Used by taoculator-snapshots' hourly cron as a Taostats-free source for
    the subnet_snapshots table (tao_in_pool, alpha_in_pool, price per netuid).
    Response shape matches what the cron needs one-for-one so the worker can
    swap sources with a minimal code change.
    """
    try:
        sub = get_subtensor()
        subnets = sub.all_subnets()
        out = []
        for info in (subnets or []):
            try:
                n = int(getattr(info, "netuid", -1))
                if n < 0:
                    continue
                tao_in = _balance_to_float(getattr(info, "tao_in", None))
                alpha_in = _balance_to_float(getattr(info, "alpha_in", None))
                price = _balance_to_float(getattr(info, "price", None))
                if price <= 0 and alpha_in > 0:
                    price = tao_in / alpha_in
                out.append({
                    "netuid": n,
                    "tao_in_pool": round(tao_in, 6),
                    "alpha_in_pool": round(alpha_in, 6),
                    "price": round(price, 9),
                })
            except Exception:
                continue
        return {
            "ok": True,
            "count": len(out),
            "subnets": out,
            "_debug": {"source": "subtensor-onchain"},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _balance_to_float(b):
    """Convert a bittensor Balance / Decimal / number to float TAO."""
    if b is None:
        return 0.0
    if hasattr(b, "tao"):
        try:
            return float(b.tao)
        except Exception:
            pass
    try:
        return float(b)
    except Exception:
        return 0.0


def _fetch_pool_prices(sub):
    """Query all subnet pools once and return {netuid: price_in_tao}.

    Price = tao_in_pool / alpha_in_pool for each subnet AMM. This is the
    same computation Taostats performs server-side; doing it here keeps
    wallet valuation fully on-chain and drops the frontend's dependency
    on the /taostats worker for current prices.
    """
    prices = {}
    try:
        subnets = sub.all_subnets()
    except Exception:
        return prices
    for info in (subnets or []):
        try:
            n = int(getattr(info, "netuid", -1))
            if n <= 0:
                continue
            # bittensor 10.x exposes a `price` field on DynamicInfo already.
            # Fall back to tao_in/alpha_in if it's missing or zero.
            p = _balance_to_float(getattr(info, "price", None))
            if p <= 0:
                tao_in = _balance_to_float(getattr(info, "tao_in", None))
                alpha_in = _balance_to_float(getattr(info, "alpha_in", None))
                p = (tao_in / alpha_in) if alpha_in > 0 else 0.0
            if p > 0:
                prices[n] = p
        except Exception:
            continue
    return prices


@app.get("/wallet")
async def wallet(address: str):
    if not address or not address.startswith("5") or len(address) < 47:
        raise HTTPException(status_code=400, detail="Invalid SS58 address")
    try:
        sub = get_subtensor()
        stake_info = sub.get_stake_info_for_coldkey(coldkey_ss58=address)
        pool_prices = _fetch_pool_prices(sub)

        root_stake_tao = 0.0
        subnet_map = {}

        for info in stake_info:
            netuid = int(info.netuid)
            try:
                alpha_amount = float(info.stake)
            except:
                alpha_amount = 0.0

            if netuid == 0:
                root_stake_tao += alpha_amount
            elif alpha_amount > 0.000001:
                if netuid not in subnet_map:
                    subnet_map[netuid] = {"netuid": netuid, "name": f"SN{netuid}", "alphaTotal": 0.0}
                subnet_map[netuid]["alphaTotal"] += alpha_amount

        alpha_positions = []
        for netuid, s in subnet_map.items():
            amt = round(s["alphaTotal"], 6)
            price = float(pool_prices.get(netuid, 0.0) or 0.0)
            alpha_positions.append({
                "netuid": netuid,
                "name": s["name"],
                "alphaAmount": amt,
                "alphaPriceTao": price,
                "taoValue": round(amt * price, 6),
                "validators": []
            })

        alpha_positions.sort(key=lambda x: x["alphaAmount"], reverse=True)

        return {
            "ok": True,
            "address": address,
            "taoBalance": 0.0,
            "rootStake": round(root_stake_tao, 6),
            "totalTao": round(root_stake_tao, 6),
            "alphaPositions": alpha_positions,
            "_debug": {
                "source": "subtensor-onchain",
                "stakeRecords": len(stake_info),
                "alphaSubnets": len(alpha_positions),
                "pricedSubnets": sum(1 for p in alpha_positions if p["alphaPriceTao"] > 0)
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Recent on-chain activity (stake/unstake ticker) ─────────────────────
# Reads the last N blocks of events from subtensor, filters for
# stake-related extrinsics, returns a normalized ticker feed. Pure chain
# query — no Taostats, no rate limit.

_STAKE_EVENT_MODULE = "SubtensorModule"
_STAKE_EVENT_NAMES = {
    "StakeAdded": "stake",
    "StakeRemoved": "unstake",
    # Older subnet-dynamic naming variants
    "StakeTransferred": "transfer",
    "AlphaStaked": "stake",
    "AlphaUnstaked": "unstake",
}


def _normalize_event(block_number, ev):
    """Turn a substrate event dict into a ticker row, or None if not of interest."""
    try:
        ev_module = ev.get("module_id") or ev.get("event_module") or ""
        ev_name = ev.get("event_id") or ev.get("event_name") or ""
        if ev_module != _STAKE_EVENT_MODULE:
            return None
        kind = _STAKE_EVENT_NAMES.get(ev_name)
        if kind is None:
            return None
        # Attributes are returned as a list of {name, value} dicts OR as a list
        # of raw values — handle both shapes defensively across bittensor versions.
        attrs = ev.get("attributes") or ev.get("params") or []
        # Subtensor StakeAdded/StakeRemoved signature is 5-tuple:
        #   (coldkey, hotkey, tao_amount_rao, alpha_amount_rao, netuid)
        # Older variants may be 4-tuple or named-dict; handle defensively.
        named = {}
        if attrs and isinstance(attrs[0], dict):
            for a in attrs:
                named[a.get("name") or a.get("type")] = a.get("value")
        else:
            # Positional mapping based on StakeAdded's 5-arg signature
            vals = list(attrs)
            if len(vals) >= 1: named["coldkey"] = vals[0]
            if len(vals) >= 2: named["hotkey"] = vals[1]
            if len(vals) >= 3: named["tao_amount"] = vals[2]
            if len(vals) >= 5:
                # 5-tuple: [ck, hk, tao, alpha, netuid]
                named["alpha_amount"] = vals[3]
                named["netuid"] = vals[4]
            elif len(vals) == 4:
                # Older 4-tuple: [ck, hk, tao, netuid]
                named["netuid"] = vals[3]
        coldkey = named.get("coldkey") or named.get("who")
        hotkey = named.get("hotkey")
        netuid = named.get("netuid")
        amount_rao = (
            named.get("tao_amount")
            or named.get("amount")
            or named.get("stake")
            or 0
        )
        alpha_rao = named.get("alpha_amount") or 0
        try:
            amount_tao = float(amount_rao) / 1e9
        except Exception:
            amount_tao = 0.0
        try:
            alpha_amount = float(alpha_rao) / 1e9
        except Exception:
            alpha_amount = 0.0
        try:
            netuid = int(netuid) if netuid is not None else None
        except Exception:
            netuid = None
        # Sanity: real subnets are small integers; anything else is a mis-decode
        if netuid is not None and (netuid < 0 or netuid > 1024):
            netuid = None
        return {
            "block": block_number,
            "kind": kind,
            "coldkey": coldkey,
            "hotkey": hotkey,
            "netuid": netuid,
            "tao": round(amount_tao, 6),
            "alpha": round(alpha_amount, 6),
        }
    except Exception:
        return None


@app.get("/recent-events")
async def recent_events(blocks: int = 20, min_tao: float = 0.0, limit: int = 100):
    """Return recent stake/unstake activity from the last N blocks.

    Only reads chain — no third-party APIs. N is capped at 50 to bound
    latency on a free-tier instance (each block = 1 substrate call).
    """
    blocks = max(1, min(int(blocks or 20), 50))
    limit = max(1, min(int(limit or 100), 500))
    try:
        sub = get_subtensor()
        substrate = sub.substrate
        head_hash = substrate.get_chain_head()
        head_num = substrate.get_block_number(head_hash)
        rows = []
        for offset in range(blocks):
            bn = head_num - offset
            if bn < 0:
                break
            try:
                bh = substrate.get_block_hash(bn)
                events = substrate.get_events(bh)
            except Exception:
                continue
            for ev in (events or []):
                # substrate-interface returns objects with .value dict or raw dicts
                payload = ev.value if hasattr(ev, "value") else ev
                if isinstance(payload, dict) and "event" in payload:
                    payload = payload["event"]
                norm = _normalize_event(bn, payload)
                if norm is None:
                    continue
                if min_tao and norm["tao"] < min_tao:
                    continue
                rows.append(norm)
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        return {
            "ok": True,
            "head_block": head_num,
            "blocks_scanned": min(blocks, head_num + 1),
            "count": len(rows),
            "events": rows,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def decode_field(val):
    """Convert byte array or string to decoded string."""
    if val is None:
        return None
    if isinstance(val, (list, tuple)):
        try:
            return bytes(val).decode('utf-8').strip('\x00').strip() or None
        except:
            return None
    if isinstance(val, str):
        return val.strip() or None
    return str(val).strip() or None

@app.get("/subnet-identity/{netuid}")
async def subnet_identity(netuid: int):
    try:
        sub = get_subtensor()
        result = sub.substrate.query(
            module="SubtensorModule",
            storage_function="SubnetIdentitiesV3",
            params=[netuid]
        )

        raw = None
        if result is not None:
            if isinstance(result, dict):
                raw = result
            elif hasattr(result, 'value') and result.value is not None:
                raw = result.value
            elif hasattr(result, 'serialize'):
                raw = result.serialize()

        if raw is None:
            return {"ok": True, "netuid": netuid, "logo_url": None, "name": None}

        logo_url = decode_field(raw.get("logo_url") or raw.get("image_url") or raw.get("icon_url"))
        name = decode_field(raw.get("subnet_name") or raw.get("name") or raw.get("subnetName"))

        return {
            "ok": True,
            "netuid": netuid,
            "logo_url": logo_url,
            "name": name
        }

    except Exception as e:
        return {"ok": True, "netuid": netuid, "logo_url": None, "name": None, "error": str(e)[:200]}


# In-memory cache of the validator coldkey set. Populated by /validator-coldkeys
# (the metagraph walk is expensive, ~60-90s for a full sweep, so we cache it on
# the service instance and refresh on demand via ?refresh=1).
_VALIDATOR_CACHE = {"coldkeys": None, "ts": 0, "subnets_scanned": 0, "partial": False}


@app.get("/validator-coldkeys")
async def validator_coldkeys(refresh: int = 0, max_seconds: int = 75):
    """Return the authoritative set of validator/miner coldkeys from chain.

    Walks every subnet's metagraph and collects the unique `coldkeys` list
    (these are the ss58 addresses that own registered hotkeys — i.e. people
    running validators or miners, not pure investors).

    Used by the taoculator-signals worker to separate validator self-stake
    from real investor conviction when computing the smart-money cohort.

    Response:
        { ok, coldkeys: [...], count, subnets_scanned, partial, cached_at }

    The walk takes ~60-90s on Finney because each get_metagraph_info is an
    RPC round-trip. We cache the result on the service instance and refresh
    only when `refresh=1` is passed. `max_seconds` bounds the walk so we
    don't exceed Render's request timeout — if we hit the budget we return
    partial=True and the caller decides whether to retry.
    """
    import time as _time

    now = _time.time()
    if not refresh and _VALIDATOR_CACHE["coldkeys"] is not None:
        return {
            "ok": True,
            "coldkeys": _VALIDATOR_CACHE["coldkeys"],
            "count": len(_VALIDATOR_CACHE["coldkeys"]),
            "subnets_scanned": _VALIDATOR_CACHE["subnets_scanned"],
            "partial": _VALIDATOR_CACHE["partial"],
            "cached_at": int(_VALIDATOR_CACHE["ts"]),
            "age_seconds": int(now - _VALIDATOR_CACHE["ts"]),
            "source": "cache",
        }

    try:
        sub = get_subtensor()
        subnets = sub.all_subnets() or []
        # Deadline-aware walk: stop pulling new metagraphs once we've spent
        # max_seconds. We always finish whatever subnet we started.
        start = _time.time()
        deadline = start + max_seconds
        unique = set()
        scanned = 0
        partial = False
        for info in subnets:
            if _time.time() > deadline:
                partial = True
                break
            try:
                n = int(getattr(info, "netuid", -1))
                if n < 0:
                    continue
                mg = sub.get_metagraph_info(n)
                cks = getattr(mg, "coldkeys", None) or []
                for ck in cks:
                    if not ck:
                        continue
                    s = str(ck).strip().lower()
                    if s:
                        unique.add(s)
                scanned += 1
            except Exception:
                # Skip bad subnets; don't let one bad RPC sink the whole walk
                continue

        coldkey_list = sorted(unique)
        _VALIDATOR_CACHE["coldkeys"] = coldkey_list
        _VALIDATOR_CACHE["subnets_scanned"] = scanned
        _VALIDATOR_CACHE["partial"] = partial
        _VALIDATOR_CACHE["ts"] = now
        return {
            "ok": True,
            "coldkeys": coldkey_list,
            "count": len(coldkey_list),
            "subnets_scanned": scanned,
            "partial": partial,
            "duration_seconds": round(_time.time() - start, 1),
            "source": "fresh",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
