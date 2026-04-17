from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import bittensor as bt

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

subtensor = None

def get_subtensor():
    global subtensor
    if subtensor is None:
        subtensor = bt.Subtensor(network="finney")
    return subtensor

@app.get("/health")
def health():
    return {"ok": True}

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


def _get_metagraph_info(sub, netuid):
    """Fetch per-UID metagraph data for a subnet. Tries the canonical
    methods in bittensor 10.x in order, returns None if none work.
    """
    for fn_name in ("get_metagraph_info", "get_subnet_state"):
        fn = getattr(sub, fn_name, None)
        if not fn:
            continue
        try:
            info = fn(netuid)
            if info is not None:
                return info
        except Exception:
            continue
    return None


@app.get("/whales/{netuid}")
async def whales(netuid: int, limit: int = 50):
    """Top holders on a subnet, aggregated by coldkey.

    Queried from subtensor directly — replaces the Taostats
    stake_balance/latest/v1 path the whales worker previously used. Only
    covers registered UIDs (validators/miners); pure delegators without a
    UID of their own are not represented. If chain queries fail the
    whales worker should fall back to its Taostats path.
    """
    if netuid < 0:
        raise HTTPException(status_code=400, detail="Invalid netuid")
    try:
        sub = get_subtensor()
        info = _get_metagraph_info(sub, netuid)
        if info is None:
            raise HTTPException(status_code=503, detail="metagraph query unavailable")

        hotkeys = list(getattr(info, "hotkeys", []) or [])
        coldkeys = list(getattr(info, "coldkeys", []) or [])
        # alpha stake per UID — field name varies across bittensor versions
        raw_stake = (
            getattr(info, "alpha_stake", None)
            or getattr(info, "S", None)
            or getattr(info, "stake", None)
            or []
        )
        stakes = [_balance_to_float(x) for x in raw_stake]

        # Pool price for this subnet — so we can return TAO values alongside alpha
        price = 0.0
        try:
            subnet_info = sub.subnet(netuid)
            price = _balance_to_float(getattr(subnet_info, "price", None))
            if price <= 0:
                tao_in = _balance_to_float(getattr(subnet_info, "tao_in", None))
                alpha_in = _balance_to_float(getattr(subnet_info, "alpha_in", None))
                price = (tao_in / alpha_in) if alpha_in > 0 else 0.0
        except Exception:
            pass

        # Aggregate by coldkey across all UIDs (one coldkey can run multiple
        # hotkeys in a subnet — whale view treats them as a single holder).
        by_coldkey = {}
        n = min(len(hotkeys), len(coldkeys), len(stakes))
        for i in range(n):
            ck = coldkeys[i]
            alpha = stakes[i]
            if not ck or alpha <= 0:
                continue
            entry = by_coldkey.get(ck)
            if entry is None:
                entry = {
                    "coldkey": ck,
                    "alpha": 0.0,
                    "tao": 0.0,
                    "hotkey_names": [],
                    "record_count": 0,
                }
                by_coldkey[ck] = entry
            entry["alpha"] += alpha
            entry["record_count"] += 1
            hk = hotkeys[i]
            if hk and hk not in entry["hotkey_names"]:
                entry["hotkey_names"].append(hk)

        holders = list(by_coldkey.values())
        for h in holders:
            h["tao"] = round(h["alpha"] * price, 6)
            h["alpha"] = round(h["alpha"], 6)
        holders.sort(key=lambda x: x["tao"], reverse=True)

        return {
            "ok": True,
            "netuid": netuid,
            "holders": holders[:max(1, min(limit, 200))],
            "total_holders": len(holders),
            "_debug": {
                "source": "subtensor-onchain",
                "uids_seen": n,
                "price_tao": price,
            },
        }

    except HTTPException:
        raise
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
