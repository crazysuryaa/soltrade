# example_usage.py
import asyncio
from soltrade.transactions import perform_swap
from soltrade.config import config

async def main():
    # Example usage with parameters from config
    user_keypair = config().keypair
    solana_client = config().client

    # Let's say you want to swap 1.0 of from_token => to_token
    sent_amount = 0.01

    # e.g., these are from config, or from a dictionary or environment
    from_token_mint = config().primary_mint
    to_token_mint = config().secondary_mint
    from_token_decimals = 9 
    to_token_decimals = 6
    slippage_bps = config().slippage
    user_pubkey = config().public_address

    success = await perform_swap(
        sent_amount=sent_amount,
        from_token_mint=from_token_mint,
        to_token_mint=to_token_mint,
        from_token_decimals=from_token_decimals,
        to_token_decimals=to_token_decimals,
        slippage_bps=slippage_bps,
        user_public_key=user_pubkey,
        keypair=user_keypair,
        client=solana_client,
        max_retries=3
    )

    print(f"Swap status: {'Success' if success else 'Failure'}")

if __name__ == "__main__":
    asyncio.run(main())
