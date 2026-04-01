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
        subnet_map = {}  # netuid -> {alphaTotal, hotkeys}

        for info in stake_info:
            netuid = int(info.netuid)
            # SDK v10: 'stake' field is a Balance object, float() gives TAO value
            try:
                alpha_amount = float(info.stake)
            except:
                alpha_amount = 0.0

            if netuid == 0:
                root_stake_tao += alpha_amount
            elif alpha_amount > 0.000001:
                if netuid not in subnet_map:
                    subnet_map[netuid] = {"alphaTotal": 0.0, "hotkey": str(getattr(info, "hotkey_ss58", ""))}
                subnet_map[netuid]["alphaTotal"] += alpha_amount

        # Get pool data to calculate TAO values
        # Use dtao pool endpoint via subtensor query for each subnet
        alpha_positions = []
        sim_errors = []

        for netuid, s in subnet_map.items():
            alpha_amount = s["alphaTotal"]
            tao_value = 0.0
            price_tao = 0.0

            try:
                # Query the subnet pool to get alpha_in and tao_in reserves
                # Then use constant-product AMM formula: tao_out = tao_in * alpha_in / (alpha_in + alpha_amount) ... 
                # Actually use get_subnet_info or query SubnetInfo
                pool_result = sub.substrate.query(
                    module="SubtensorModule",
                    storage_function="SubnetAlphaIn",
                    params=[netuid]
                )
                tao_result = sub.substrate.query(
                    module="SubtensorModule",
                    storage_function="SubnetTaoIn",
                    params=[netuid]
                )
                
                if pool_result and tao_result:
                    alpha_in = float(pool_result.value) / 1e9  # rao to TAO
                    tao_in = float(tao_result.value) / 1e9
                    
                    if alpha_in > 0 and tao_in > 0:
                        # Spot price: tao per alpha
                        price_tao = tao_in / alpha_in
                        # AMM output (constant product): dy = y * dx / (x + dx)
                        # but spot price is close enough for display
                        tao_value = alpha_amount * price_tao
            except Exception as e:
                sim_errors.append(f"SN{netuid}: {str(e)[:50]}")
                tao_value = 0.0
                price_tao = 0.0

            alpha_positions.append({
                "netuid": netuid,
                "name": f"SN{netuid}",
                "alphaAmount": round(alpha_amount, 6),
                "alphaPriceTao": round(price_tao, 8),
                "taoValue": round(tao_value, 6),
                "validators": [s["hotkey"][:10] + "…"] if s["hotkey"] else []
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
                "simErrors": sim_errors[:5]
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
