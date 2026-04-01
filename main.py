import os
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import bittensor as bt

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Read-only subtensor connection — no wallet, no keys, just chain queries
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

        # get_stake_info_for_coldkey returns all stakes for this coldkey across all subnets
        # This is a single batch chain call — no rate limits, no API key needed
        stake_info = sub.get_stake_info_for_coldkey(coldkey_ss58=address)

        root_stake_tao = 0.0
        subnet_map = {}

        for info in stake_info:
            netuid = int(info.netuid)
            # stake is a Balance object — .tao gives TAO float value
            alpha_amount = float(info.stake.tao) if hasattr(info.stake, 'tao') else float(info.stake)
            # tao_value = alpha converted to TAO equivalent
            tao_value = float(info.stake_as_tao.tao) if hasattr(info, 'stake_as_tao') and hasattr(info.stake_as_tao, 'tao') else 0.0

            if netuid == 0:
                root_stake_tao += tao_value if tao_value > 0 else alpha_amount
            elif alpha_amount > 0.000001:
                if netuid not in subnet_map:
                    subnet_map[netuid] = {
                        "netuid": netuid,
                        "name": f"SN{netuid}",
                        "alphaTotal": 0.0,
                        "taoTotal": 0.0,
                        "validators": []
                    }
                subnet_map[netuid]["alphaTotal"] += alpha_amount
                subnet_map[netuid]["taoTotal"] += tao_value
                hotkey = str(info.hotkey_ss58) if hasattr(info, 'hotkey_ss58') else ""
                if hotkey:
                    subnet_map[netuid]["validators"].append(hotkey[:8] + "…")

        alpha_positions = []
        for netuid, s in subnet_map.items():
            alpha_price_tao = s["taoTotal"] / s["alphaTotal"] if s["alphaTotal"] > 0 else 0
            alpha_positions.append({
                "netuid": netuid,
                "name": s["name"],
                "alphaAmount": s["alphaTotal"],
                "alphaPriceTao": alpha_price_tao,
                "taoValue": s["taoTotal"],
                "validators": list(set(s["validators"]))
            })

        return {
            "ok": True,
            "address": address,
            "taoBalance": 0.0,  # free balance requires separate System.Account query
            "rootStake": root_stake_tao,
            "totalTao": root_stake_tao,
            "alphaPositions": alpha_positions,
            "_debug": {
                "source": "subtensor-onchain",
                "stakeRecords": len(stake_info),
                "alphaSubnets": len(alpha_positions)
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
