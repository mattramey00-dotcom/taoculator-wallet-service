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
            storage_function="SubnetIdentity",
            params=[netuid]
        )
        if result is None or result.value is None:
            return {"ok": True, "netuid": netuid, "logo_url": None, "name": None}

        val = result.value
        # SubnetIdentity returns a struct with name, logo_url, etc.
        logo_url = None
        name = None
        if isinstance(val, dict):
            logo_url = val.get("logo_url") or val.get("image_url") or val.get("icon_url")
            name = val.get("subnet_name") or val.get("name")
        elif hasattr(val, "logo_url"):
            logo_url = str(val.logo_url) if val.logo_url else None
            name = str(val.subnet_name) if hasattr(val, "subnet_name") else None

        return {"ok": True, "netuid": netuid, "logo_url": logo_url, "name": name, "raw": str(val)[:200]}

    except Exception as e:
        return {"ok": True, "netuid": netuid, "logo_url": None, "name": None, "error": str(e)[:100]}
