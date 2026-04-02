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

@app.get("/wallet")
async def wallet(address: str):
    if not address or not address.startswith("5") or len(address) < 47:
        raise HTTPException(status_code=400, detail="Invalid SS58 address")
    try:
        sub = get_subtensor()
        stake_info = sub.get_stake_info_for_coldkey(coldkey_ss58=address)

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
            alpha_positions.append({
                "netuid": netuid,
                "name": s["name"],
                "alphaAmount": round(s["alphaTotal"], 6),
                "alphaPriceTao": 0.0,
                "taoValue": 0.0,
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
                "alphaSubnets": len(alpha_positions)
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/subnet-identity/{netuid}")
async def subnet_identity(netuid: int):
    try:
        sub = get_subtensor()
        result = sub.substrate.query(
            module="SubtensorModule",
            storage_function="SubnetIdentitiesV3",
            params=[netuid]
        )

        # SubnetIdentitiesV3 returns a dict directly, not a ScaleType with .value
        raw = None
        if result is not None:
            if isinstance(result, dict):
                raw = result
            elif hasattr(result, 'value') and result.value is not None:
                raw = result.value
            elif hasattr(result, 'serialize'):
                raw = result.serialize()

        if raw is None:
            return {"ok": True, "netuid": netuid, "logo_url": None, "name": None, "raw": str(result)[:200]}

        logo_url = (raw.get("logo_url") or raw.get("image_url") or
                    raw.get("icon_url") or raw.get("logo") or
                    raw.get("logoUrl") or "")
        name = (raw.get("subnet_name") or raw.get("name") or
                raw.get("subnetName") or raw.get("SubnetName") or "")

        return {
            "ok": True,
            "netuid": netuid,
            "logo_url": logo_url or None,
            "name": name or None,
            "raw_keys": list(raw.keys()) if isinstance(raw, dict) else None,
            "raw": str(raw)[:300]
        }

    except Exception as e:
        return {"ok": True, "netuid": netuid, "logo_url": None, "name": None, "error": str(e)[:200]}
