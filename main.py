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
        logo_url = None
        name = None
        raw_val = None

        # Try V3, V2, V1 in order
        for storage_fn in ['SubnetIdentitiesV3', 'SubnetIdentitiesV2', 'SubnetIdentities']:
            try:
                result = sub.substrate.query(
                    module="SubtensorModule",
                    storage_function=storage_fn,
                    params=[netuid]
                )
                if result is not None and result.value is not None:
                    val = result.value
                    raw_val = str(val)[:300]
                    if isinstance(val, dict):
                        logo_url = (val.get("logo_url") or val.get("image_url") or
                                   val.get("icon_url") or val.get("logo") or "")
                        name = (val.get("subnet_name") or val.get("name") or
                               val.get("subnetName") or "")
                    break
            except Exception:
                continue

        return {
            "ok": True,
            "netuid": netuid,
            "logo_url": logo_url or None,
            "name": name or None,
            "raw": raw_val
        }

    except Exception as e:
        return {"ok": True, "netuid": netuid, "logo_url": None, "name": None, "error": str(e)[:100]}
