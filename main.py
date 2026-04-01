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

            # v10 SDK: stake field is a Balance object
            # Try multiple field patterns for alpha amount
            try:
                alpha_amount = float(info.stake)
            except:
                alpha_amount = 0.0

            # TAO equivalent value — try stake_as_tao first, fall back to tao_value
            tao_value = 0.0
            for field in ['stake_as_tao', 'tao_value', 'value']:
                val = getattr(info, field, None)
                if val is not None:
                    try:
                        tao_value = float(val)
                        if tao_value > 0:
                            break
                    except:
                        pass

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
                hotkey = str(getattr(info, 'hotkey_ss58', '') or '')
                if hotkey and len(hotkey) > 8:
                    subnet_map[netuid]["validators"].append(hotkey[:8] + "…")

        alpha_positions = []
        for netuid, s in subnet_map.items():
            price_tao = s["taoTotal"] / s["alphaTotal"] if s["alphaTotal"] > 0 and s["taoTotal"] > 0 else 0.0
            alpha_positions.append({
                "netuid": netuid,
                "name": s["name"],
                "alphaAmount": round(s["alphaTotal"], 6),
                "alphaPriceTao": round(price_tao, 8),
                "taoValue": round(s["taoTotal"], 6),
                "validators": list(set(s["validators"]))
            })

        # Sort by TAO value descending
        alpha_positions.sort(key=lambda x: x["taoValue"], reverse=True)

        # Debug: log first stake_info fields to help diagnose taoValue=0 issue
        debug_fields = []
        if stake_info:
            debug_fields = [f for f in dir(stake_info[0]) if not f.startswith('_')]

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
                "stakeInfoFields": debug_fields[:15]  # first 15 field names for diagnosis
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
