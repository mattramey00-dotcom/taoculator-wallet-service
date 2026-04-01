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

        # Inspect first record's fields for debugging
        debug_fields = []
        if stake_info:
            obj = stake_info[0]
            debug_fields = [f for f in dir(obj) if not f.startswith('_')]

        for info in stake_info:
            netuid = int(info.netuid)

            # alpha amount — the raw stake on this subnet
            alpha_amount = 0.0
            for field in ['stake', 'alpha', 'amount']:
                val = getattr(info, field, None)
                if val is not None:
                    try:
                        alpha_amount = float(val)
                        if alpha_amount > 0:
                            break
                    except:
                        pass

            # tao equivalent — try every known field name in SDK v10
            tao_value = 0.0
            for field in ['tao', 'tao_value', 'stake_as_tao', 'value_as_tao', 'tao_worth']:
                val = getattr(info, field, None)
                if val is not None:
                    try:
                        tv = float(val)
                        if tv > 0:
                            tao_value = tv
                            break
                    except:
                        pass

            if netuid == 0:
                # Root stake — alpha_amount IS tao for netuid 0
                root_stake_tao += tao_value if tao_value > 0 else alpha_amount
            elif alpha_amount > 0.000001:
                if netuid not in subnet_map:
                    subnet_map[netuid] = {
                        "netuid": netuid,
                        "name": f"SN{netuid}",
                        "alphaTotal": 0.0,
                        "taoTotal": 0.0,
                    }
                subnet_map[netuid]["alphaTotal"] += alpha_amount
                subnet_map[netuid]["taoTotal"] += tao_value

        # For subnets where taoTotal is still 0, try sim_swap to get TAO value
        for netuid, s in subnet_map.items():
            if s["taoTotal"] == 0.0 and s["alphaTotal"] > 0:
                try:
                    result = sub.sim_swap(
                        netuid=netuid,
                        amount=bt.Balance.from_tao(s["alphaTotal"]),
                        is_buy=False  # selling alpha → TAO
                    )
                    if result and float(result) > 0:
                        s["taoTotal"] = float(result)
                except:
                    pass

        alpha_positions = []
        for netuid, s in subnet_map.items():
            price_tao = s["taoTotal"] / s["alphaTotal"] if s["alphaTotal"] > 0 and s["taoTotal"] > 0 else 0.0
            alpha_positions.append({
                "netuid": netuid,
                "name": s["name"],
                "alphaAmount": round(s["alphaTotal"], 6),
                "alphaPriceTao": round(price_tao, 8),
                "taoValue": round(s["taoTotal"], 6),
                "validators": []
            })

        alpha_positions.sort(key=lambda x: x["taoValue"], reverse=True)

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
                "stakeInfoFields": debug_fields[:20]
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
