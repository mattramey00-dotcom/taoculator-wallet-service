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
    # build marker — bump to force/verify a Render redeploy
    return {"ok": True, "build": "conv-agg-v2"}


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


# ── Conviction protocol (BIT-0011, subtensor PR #2599, May 2026) ────────
# Subnet owners auto-lock 100% of their 18% emission share to their own
# hotkey; anyone else can voluntarily lock alpha to the owner's hotkey or
# to a challenger. The chain exposes a `subnet_king` concept = whichever
# hotkey has the most locked alpha on that subnet. When king != owner, a
# community member or rival has overtaken the owner — the actionable
# signal we want to surface.
#
# IMPORTANT: this is NOT the same as the client-side "Tenure" feature in
# index.html. Tenure = how long a coldkey has held a position (behavioural,
# derived from StakeEvent timestamps). Conviction = explicit on-chain lock
# via lock_stake extrinsic. Different data path, different meaning.
#
# Storage shape (from subtensor lib.rs at b05822e3):
#   HotkeyLock: StorageDoubleMap<(netuid, hotkey), LockState>
#     LockState { locked_mass, unlocked_mass, conviction, last_update }
#   SubnetOwnerHotkey: StorageMap<netuid, hotkey>
#   SubnetOwner: StorageMap<netuid, coldkey>


def _decode_lock_state(raw):
    """Pull a LockState dict from a HotkeyLock storage value. Defensive
    across substrate-interface return shapes."""
    if raw is None:
        return None
    if hasattr(raw, "value") and raw.value is not None:
        raw = raw.value
    if hasattr(raw, "serialize"):
        try:
            raw = raw.serialize()
        except Exception:
            pass
    if not isinstance(raw, dict):
        return None
    return {
        "locked_mass": raw.get("locked_mass") or raw.get("lockedMass") or 0,
        "unlocked_mass": raw.get("unlocked_mass") or raw.get("unlockedMass") or 0,
        "conviction": raw.get("conviction") or 0,
        "last_update": raw.get("last_update") or raw.get("lastUpdate") or 0,
    }


def _alpha_to_float(v):
    """LockState's locked_mass/unlocked_mass are AlphaBalance (u64 in RAO).
    Divide by 1e9. Guard against already-decoded floats."""
    try:
        n = float(v)
    except Exception:
        return 0.0
    return n / 1e9 if n > 1e6 else n


def _u64f64_to_float(v):
    """Conviction is U64F64 — raw integer / 2^64."""
    try:
        n = float(v)
    except Exception:
        return 0.0
    return n / (2 ** 64) if n > 1e12 else n


def _ss58_from_key(k):
    """Decode a query_map AccountId32 key into an SS58 string.

    substrate-interface returns the second-dimension key of a DoubleMap
    iteration as a 1-tuple of a 32-tuple of u8 (e.g. `((90, 50, 232, ...),)`).
    A plain `str()` of that is unusable as a hotkey — callers need the SS58.
    Bittensor's mainnet uses the generic Substrate prefix (42).
    """
    v = k.value if hasattr(k, "value") else k
    if isinstance(v, tuple) and len(v) == 1:
        v = v[0]
    if isinstance(v, (tuple, list)):
        try:
            v = bytes(v)
        except Exception:
            return str(v)
    if isinstance(v, (bytes, bytearray)) and len(v) == 32:
        try:
            from substrateinterface.utils.ss58 import ss58_encode
            return ss58_encode(bytes(v), ss58_format=42)
        except Exception:
            try:
                from scalecodec.utils.ss58 import ss58_encode
                return ss58_encode(bytes(v), ss58_format=42)
            except Exception:
                return str(v)
    return str(v)


def _conviction_bits_to_float(c):
    """Conviction v2 stores conviction as U64F64 wrapped as {'bits': u128}.
    bits / 2^64 yields the value in RAO (it matures toward locked_mass); divide
    by 1e9 to return alpha, matching lockedAlpha's scale."""
    if isinstance(c, dict):
        c = c.get("bits", 0)
    try:
        n = float(c)
    except Exception:
        return 0.0
    return (n / (2 ** 64)) / 1e9


# Conviction v2 (mainnet ~2026-07) moved locks off the 2-param HotkeyLock into a
# 3-key NMap SubtensorModule.Lock keyed (hotkey, netuid, coldkey) — the netuid is
# the middle, Identity-hashed key, so it can't be used as a query_map prefix. The
# whole map is small (a few hundred entries network-wide), so scan it once, group
# by netuid, and cache for 120s. Value shape:
#   {locked_mass: u64 RAO, conviction: {bits: u128}, last_update: block}
_LOCK_CACHE = {"by_netuid": None, "ts": 0}


def _load_all_locks(substrate):
    import time as _time
    now = _time.time()
    if _LOCK_CACHE["by_netuid"] is not None and (now - _LOCK_CACHE["ts"]) < 120:
        return _LOCK_CACHE["by_netuid"]
    # netuid -> hotkey -> aggregated lock. Key order is (coldkey, netuid, hotkey);
    # the non-int components are [coldkey, hotkey]. Conviction locks alpha toward
    # a HOTKEY (multiple coldkeys can lock to the same hotkey), so aggregate by the
    # LAST account (hotkey) — the owner's hotkey is then the "king".
    agg = {}
    it = substrate.query_map(module="SubtensorModule", storage_function="Lock")
    for key_obj, lock_val in it:
        key = key_obj.value if hasattr(key_obj, "value") else key_obj
        comps = list(key) if isinstance(key, (tuple, list)) else [key]
        nid = next((c for c in comps if isinstance(c, int)), None)
        if nid is None:
            continue
        val = lock_val.value if hasattr(lock_val, "value") else lock_val
        if not isinstance(val, dict):
            continue
        locked = _alpha_to_float(val.get("locked_mass", 0))
        if locked <= 0:
            continue
        accts = [c for c in comps if not isinstance(c, int)]
        if not accts:
            continue
        hotkey = _ss58_from_key(accts[-1])
        conv = _conviction_bits_to_float(val.get("conviction"))
        hk_map = agg.setdefault(nid, {})
        e = hk_map.setdefault(hotkey, {"hotkey": hotkey, "lockedAlpha": 0.0, "conviction": 0.0})
        e["lockedAlpha"] += locked
        e["conviction"] += conv
    by_netuid = {nid: list(hkmap.values()) for nid, hkmap in agg.items()}
    _LOCK_CACHE["by_netuid"] = by_netuid
    _LOCK_CACHE["ts"] = now
    return by_netuid


@app.get("/conviction/metadata")
async def conviction_metadata():
    """Diagnostic — what conviction/lock storage actually exists on chain?

    The pallet PR #2599 was merged to subtensor `main` 2026-05-01 but a
    follow-up PR #2656 ("Temporarily disable stake locking to allow
    conviction redesign") and PR #2658 ("Conviction v2") are open. So
    mainnet finney may be running an older runtime without HotkeyLock,
    OR may eventually expose conviction under a different storage name.

    This endpoint probes (a) candidate storage paths by attempting a
    one-shot query and (b) walks the chain metadata for any storage
    entries matching "lock" or "conviction". When v2 ships we'll see the
    actual name here and can fix /conviction/{netuid} without guessing.
    """
    try:
        sub = get_subtensor()
        substrate = sub.substrate

        # Runtime version — bumps every chain upgrade.
        runtime_version = None
        runtime_name = None
        try:
            rv = substrate.get_runtime_version()
            if isinstance(rv, dict):
                runtime_version = rv.get("specVersion") or rv.get("spec_version")
                runtime_name = rv.get("specName") or rv.get("spec_name")
        except Exception:
            pass

        # Candidate probe — try a single-key query against each. ValueQuery
        # storage returns a default (not an error) if the storage exists but
        # the key is unknown, so the absence of an exception is "exists".
        probes = [
            ("SubtensorModule", "HotkeyLock"),
            ("SubtensorModule", "Lock"),
            ("SubtensorModule", "Locks"),
            ("SubtensorModule", "Conviction"),
            ("SubtensorModule", "ConvictionLock"),
            ("SubtensorModule", "LockedAlpha"),
            ("SubtensorModule", "MaturityRate"),
            ("SubtensorModule", "UnlockRate"),
            ("Conviction", "HotkeyLock"),
            ("Conviction", "Lock"),
            ("Lock", "HotkeyLock"),
        ]
        probe_results = []
        for module_name, storage_name in probes:
            try:
                substrate.query(module=module_name, storage_function=storage_name, params=[0])
                probe_results.append({"module": module_name, "storage": storage_name, "exists": True})
            except Exception as e:
                msg = str(e)[:120]
                probe_results.append({
                    "module": module_name,
                    "storage": storage_name,
                    "exists": False,
                    "error": msg,
                })

        # Metadata walk — find every storage function whose name contains
        # "lock" or "conviction". Substrate-interface 1.x exposes metadata
        # in a few different shapes depending on version; try several.
        matches = []
        try:
            md = substrate.get_metadata()
            value = getattr(md, "value", None) or md
            # V14: value = {"magicNumber": ..., "metadata": {"V14": {"pallets": [...]}}}
            pallets = None
            if isinstance(value, dict):
                inner = value.get("metadata") or value
                if isinstance(inner, dict):
                    for v in ("V14", "V13", "V12"):
                        if v in inner:
                            pallets = inner[v].get("pallets")
                            break
            if pallets is None:
                pallets = getattr(md, "pallets", None) or []

            for p in pallets:
                pname = p.get("name") if isinstance(p, dict) else getattr(p, "name", None)
                storage = p.get("storage") if isinstance(p, dict) else getattr(p, "storage", None)
                if not storage:
                    continue
                entries = (
                    storage.get("entries") if isinstance(storage, dict)
                    else getattr(storage, "entries", None) or []
                )
                for e in entries:
                    ename = e.get("name") if isinstance(e, dict) else getattr(e, "name", None)
                    if not ename:
                        continue
                    lo = ename.lower()
                    if "lock" in lo or "conviction" in lo:
                        matches.append({"pallet": pname, "storage": ename})
        except Exception as e:
            matches = [{"error": f"metadata walk failed: {str(e)[:200]}"}]

        return {
            "ok": True,
            "runtime_version": runtime_version,
            "runtime_name": runtime_name,
            "probes": probe_results,
            "matches": matches,
            "_debug": {
                "source": "subtensor-onchain",
                "convictionMetadataEndpoint": True,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conviction/{netuid}")
async def conviction(netuid: int):
    """Return the conviction king + owner for a subnet.

    King = hotkey with the most alpha locked toward it (HotkeyLock entry
    with the largest locked_mass on this netuid). Sorted by locked_mass
    rather than the U64F64 conviction score because locked_mass is the
    leading indicator — conviction matures off it on a 62-day half-life,
    so a fresh huge lock is a signal even before conviction has caught up.
    Both values are returned so the caller can choose.

    Owner = SubnetOwnerHotkey(netuid). Auto-locked emissions accumulate
    here every block, so for most subnets the owner will also be the king.
    The actionable case is king != owner.
    """
    if netuid < 0 or netuid > 1024:
        raise HTTPException(status_code=400, detail="Invalid netuid")
    try:
        sub = get_subtensor()
        substrate = sub.substrate

        owner_hotkey = None
        owner_coldkey = None
        try:
            r = substrate.query("SubtensorModule", "SubnetOwnerHotkey", [netuid])
            if r is not None:
                v = r.value if hasattr(r, "value") else r
                if v:
                    owner_hotkey = str(v)
        except Exception:
            pass
        try:
            r = substrate.query("SubtensorModule", "SubnetOwner", [netuid])
            if r is not None:
                v = r.value if hasattr(r, "value") else r
                if v:
                    owner_coldkey = str(v)
        except Exception:
            pass

        # Conviction locks now live in SubtensorModule.Lock (see _load_all_locks).
        # The old 2-param HotkeyLock is legacy/empty for subnets that locked under
        # Conviction v2 (e.g. Lium/SN51 had 0 there but ~387k α here).
        king_hotkey = None
        king_locked = 0.0
        king_conviction = 0.0
        total_locked = 0.0
        total_conviction = 0.0
        hotkey_count = 0
        rows = []
        try:
            rows = list(_load_all_locks(substrate).get(netuid, []))
            for r in rows:
                hotkey_count += 1
                total_locked += r["lockedAlpha"]
                total_conviction += r["conviction"]
                if r["lockedAlpha"] > king_locked:
                    king_hotkey = r["hotkey"]
                    king_locked = r["lockedAlpha"]
                    king_conviction = r["conviction"]
        except Exception as e:
            return {
                "ok": False,
                "netuid": netuid,
                "error": f"Lock query failed: {str(e)[:200]}",
                "_debug": {"source": "subtensor-onchain", "convictionEndpoint": True},
            }

        rows.sort(key=lambda r: r["lockedAlpha"], reverse=True)
        top = [
            {
                "hotkey": r["hotkey"],
                "lockedAlpha": round(r["lockedAlpha"], 6),
                "conviction": round(r["conviction"], 6),
            }
            for r in rows[:5]
        ]

        return {
            "ok": True,
            "netuid": netuid,
            "kingHotkey": king_hotkey,
            "kingLockedAlpha": round(king_locked, 6),
            "kingConviction": round(king_conviction, 6),
            "ownerHotkey": owner_hotkey,
            "ownerColdkey": owner_coldkey,
            "kingIsOwner": (king_hotkey is not None and owner_hotkey is not None and king_hotkey == owner_hotkey),
            "totalLockedAlpha": round(total_locked, 6),
            "totalConviction": round(total_conviction, 6),
            "hotkeyCount": hotkey_count,
            "top": top,
            "_debug": {
                "source": "subtensor-onchain",
                "storageMap": "SubtensorModule.Lock",
                "convictionEndpoint": True,
                "ss58Decoded": True,
            },
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conviction/probe/{netuid}")
async def conviction_probe(netuid: int):
    """Diagnostic: Conviction v2 moved locks off the 2-param HotkeyLock into a
    3-param SubtensorModule.Lock. Dump raw entries for both so we can see the
    key order + value shape before wiring /conviction to read Lock. Remove once
    /conviction is fixed."""
    sub = get_subtensor()
    substrate = sub.substrate
    out = {"netuid": netuid, "hotkeyLock": {}, "lock": {}}
    try:
        cnt = 0
        samples = []
        it = substrate.query_map(module="SubtensorModule", storage_function="HotkeyLock", params=[netuid])
        for k, v in it:
            cnt += 1
            if len(samples) < 3:
                val = v.value if hasattr(v, "value") else v
                samples.append({"key": str(k.value if hasattr(k, "value") else k)[:70], "val": str(val)[:220]})
        out["hotkeyLock"] = {"count": cnt, "samples": samples}
    except Exception as e:
        out["hotkeyLock"] = {"error": str(e)[:220]}
    lock_out = {}
    # (a) metadata key structure for Lock
    try:
        pallet = substrate.metadata.get_metadata_pallet("SubtensorModule")
        for s in (getattr(pallet, "storage", None) or []):
            nm = s.value.get("name") if hasattr(s, "value") else getattr(s, "name", None)
            if nm == "Lock":
                lock_out["meta"] = str(s.value.get("type"))[:600]
                break
    except Exception as e:
        lock_out["meta_error"] = str(e)[:220]
    # (b) full-map sample (no params) — reveals the key order/shape
    try:
        cnt = 0
        samples = []
        it = substrate.query_map(module="SubtensorModule", storage_function="Lock")
        for k, v in it:
            cnt += 1
            val = v.value if hasattr(v, "value") else v
            key = k.value if hasattr(k, "value") else k
            samples.append({"key": str(key)[:150], "val": str(val)[:160], "vt": type(val).__name__})
            if cnt >= 8:
                break
        lock_out["fullSample"] = samples
    except Exception as e:
        lock_out["full_error"] = str(e)[:220]
    # (c) filtered by netuid (best-effort — may fail if netuid isn't key #1)
    try:
        cnt = 0
        samples = []
        it = substrate.query_map(module="SubtensorModule", storage_function="Lock", params=[netuid])
        for k, v in it:
            cnt += 1
            if len(samples) < 5:
                val = v.value if hasattr(v, "value") else v
                samples.append({"key": str(k.value if hasattr(k, "value") else k)[:130], "val": str(val)[:160]})
        lock_out["byNetuid"] = {"count": cnt, "samples": samples}
    except Exception as e:
        lock_out["byNetuid_error"] = str(e)[:220]
    # (d) compute this netuid's total by full-map iteration, filtering on the
    # middle (Identity-hashed u16) netuid key.
    try:
        scanned = 0
        matched = 0
        total_locked_rao = 0
        king_locked = 0
        king_accts = []
        posns = {}
        it = substrate.query_map(module="SubtensorModule", storage_function="Lock")
        for k, v in it:
            scanned += 1
            key = k.value if hasattr(k, "value") else k
            comps = list(key) if isinstance(key, (tuple, list)) else [key]
            nid = None
            for idx, c in enumerate(comps):
                if isinstance(c, int):
                    nid = c
                    posns[idx] = posns.get(idx, 0) + 1
                    break
            if nid == netuid:
                val = v.value if hasattr(v, "value") else v
                lm = val.get("locked_mass", 0) if isinstance(val, dict) else 0
                matched += 1
                total_locked_rao += lm
                if lm > king_locked:
                    king_locked = lm
                    # decode every non-int component so we can see which key
                    # position is the hotkey vs coldkey.
                    king_accts = [_ss58_from_key(c) for c in comps if not isinstance(c, int)]
            if scanned >= 60000:
                break
        lock_out["computed"] = {
            "scanned": scanned,
            "matched": matched,
            "totalLockedAlpha": round(total_locked_rao / 1e9, 4),
            "kingLockedAlpha": round(king_locked / 1e9, 4),
            "netuidKeyPositions": posns,
            "kingAccounts": king_accts,
        }
    except Exception as e:
        lock_out["computed_error"] = str(e)[:220]
    out["lock"] = lock_out
    return out


# In-memory cache of the validator coldkey set. Populated by /validator-coldkeys
# (the metagraph walk is expensive, ~60-90s for a full sweep, so we cache it on
# the service instance and refresh on demand via ?refresh=1).
_VALIDATOR_CACHE = {"coldkeys": None, "ts": 0, "subnets_scanned": 0, "partial": False}


def _do_validator_walk(max_seconds: int):
    """Synchronous metagraph walk. Called from a thread so the event loop
    stays free to serve /wallet and /health requests concurrently.

    Memory-conscious: this runs on a 512 MB Render instance and each
    per-subnet metagraph can be large (thousands of neurons on the bigger
    subnets). We extract just the coldkeys, then explicitly drop the
    metagraph reference and collect garbage so peak RSS stays ~one
    metagraph rather than letting freed-but-not-yet-collected objects pile
    up across the ~130-subnet walk and trip the OOM killer (which forces
    the Render auto-restart we're trying to avoid).
    """
    import time as _time
    import gc

    sub = get_subtensor()
    subnets = sub.all_subnets() or []
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
            # Free the metagraph (and its coldkey list) immediately so the
            # next subnet's load doesn't stack on top of this one. del drops
            # the refcount to 0 for the non-cyclic bulk; the periodic
            # gc.collect() reclaims any reference cycles substrate-interface
            # leaves behind. Collecting every few subnets bounds peak memory
            # without paying gc latency on every single iteration.
            del mg, cks
            if scanned % 8 == 0:
                gc.collect()
        except Exception:
            continue
    # Release the all_subnets snapshot and do a final sweep before we hand
    # back the (comparatively tiny) coldkey set.
    del subnets
    gc.collect()
    return {
        "coldkeys": sorted(unique),
        "subnets_scanned": scanned,
        "partial": partial,
        "duration_seconds": round(_time.time() - start, 1),
    }


@app.get("/validator-coldkeys")
async def validator_coldkeys(refresh: int = 0, max_seconds: int = 75):
    """Return the authoritative set of validator/miner coldkeys from chain.

    The walk is sync (bittensor SDK) and takes ~60-90s. We offload it to a
    worker thread via asyncio.to_thread so the single-worker uvicorn on
    Render keeps serving /wallet and /health on the event loop in parallel.
    Without this, every refresh makes the wallet service appear down to all
    other callers for the duration of the walk.

    Cached on the service instance. Pass refresh=1 to force a rebuild.
    """
    import asyncio
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
        result = await asyncio.to_thread(_do_validator_walk, max_seconds)
        _VALIDATOR_CACHE["coldkeys"] = result["coldkeys"]
        _VALIDATOR_CACHE["subnets_scanned"] = result["subnets_scanned"]
        _VALIDATOR_CACHE["partial"] = result["partial"]
        _VALIDATOR_CACHE["ts"] = now
        return {
            "ok": True,
            "coldkeys": result["coldkeys"],
            "count": len(result["coldkeys"]),
            "subnets_scanned": result["subnets_scanned"],
            "partial": result["partial"],
            "duration_seconds": result["duration_seconds"],
            "source": "fresh",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
