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
                    subnet_map[netuid] = {"alphaTotal": 0.0}
                subnet_map[netuid]["alphaTotal"] += alpha_amount

        # Fetch pool reserves for all subnets at once using batch query
        netuids = list(subnet_map.keys())
        pool_errors = []
        tao_in_map = {}
        alpha_in_map = {}

        for netuid in netuids:
            try:
                r_tao = sub.substrate.query(
                    module="SubtensorModule",
                    storage_function="SubnetTaoIn",
                    params=[netuid]
                )
                r_alpha = sub.substrate.query(
                    module="SubtensorModule",
                    storage_function="SubnetAlphaIn",
                    params=[netuid]
                )
                if r_tao is not None:
                    tao_in_map[netuid] = float(r_tao.value) / 1e9
                if r_alpha is not None:
                    alpha_in_map[netuid] = float(r_alpha.value) / 1e9
            except Exception as e:
                pool_errors.append(f"SN{netuid}:{str(e)[:40]}")

        alpha_positions = []
        for netuid, s in subnet_map.items():
            alpha_amount = s["alphaTotal"]
            tao_value = 0.0
            price_tao = 0.0

            tao_in = tao_in_map.get(netuid, 0)
            alpha_in = alpha_in_map.get(netuid, 0)

            if tao_in > 0 and alpha_in > 0:
                price_tao = tao_in / alpha_in
                # AMM constant product formula for sell: tao_out = tao_in * alpha_in / (alpha_in + alpha_amount) ... 
                # Use spot price for display (accurate for small positions)
                tao_value = alpha_amount * price_tao

            alpha_positions.append({
                "netuid": netuid,
                "name": f"SN{netuid}",
                "alphaAmount": round(alpha_amount, 6),
                "alphaPriceTao": round(price_tao, 8),
                "taoValue": round(tao_value, 6),
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
                "source": "subtensor-onchain-pool",
                "stakeRecords": len(stake_info),
                "alphaSubnets": len(alpha_positions),
                "poolErrors": pool_errors[:5],
                "samplePool": {
                    "taoIn": tao_in_map.get(netuids[0]) if netuids else None,
                    "alphaIn": alpha_in_map.get(netuids[0]) if netuids else None,
                    "netuid": netuids[0] if netuids else None
                }
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
