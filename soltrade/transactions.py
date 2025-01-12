# transactions.py
import asyncio
import base64
import json
from typing import Optional

import httpx
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solders import message
from solana.rpc.types import TxOpts
from solana.rpc.core import RPCException

from soltrade.log import log_general, log_transaction
from soltrade.config import config
from soltrade.market_position import market

###############################################################################
#                           Utility Functions
###############################################################################

def find_transaction_error(txid: Signature, client) -> Optional[dict]:
    """
    Returns the 'err' field from the transaction metadata, if any.
    If the transaction is not found or there's no data yet, returns None.
    """
    raw_response = client.get_transaction(
        txid,
        max_supported_transaction_version=0
    ).to_json()

    # If raw_response is None or an empty string, just return None
    if not raw_response:
        # Could log a warning or debug here as well
        return None

    try:
        parsed = json.loads(raw_response)
        # Check if parsed["result"] or parsed["result"]["meta"] might be None
        # before you try to subscript.
        if not parsed.get("result"):
            return None
        if not parsed["result"].get("meta"):
            return None

        return parsed["result"]["meta"]["err"]  # Could be None if no error
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # If the JSON structure is unexpected, handle gracefully
        # e.g. log an error and return None
        log_general.warning(f"Failed to parse transaction data: {raw_response}, error: {e}")
        return None

def find_last_valid_block_height(client) -> int:
    """
    Returns the last valid block height from the given Solana client.
    """
    json_response = client.get_latest_blockhash(commitment="confirmed").to_json()
    parsed_response = json.loads(json_response)["result"]["value"]["lastValidBlockHeight"]
    return parsed_response

###############################################################################
#                   Core Jupiter Transaction Logic
###############################################################################

async def create_exchange(
    input_amount: float,
    from_token_mint: str,
    to_token_mint: str,
    from_token_decimals: int,
    to_token_decimals: int,
    slippage_bps: int
) -> dict:
    """
    Calls Jupiter's 'quote' endpoint to get a swap quote.

    :param input_amount: Amount (in float) of from_token to swap.
    :param from_token_mint: The token mint we are swapping from.
    :param to_token_mint: The token mint we are swapping into.
    :param from_token_decimals: Decimals for the from_token.
    :param to_token_decimals: Decimals for the to_token (helpful for logs).
    :param slippage_bps: Slippage tolerance in basis points.
    :return: The JSON response from Jupiter's quote endpoint.
    """
    log_transaction.info(
        f"Creating exchange quote for {input_amount} units of {from_token_mint} -> {to_token_mint}"
    )

    # Convert human-readable float to integer lamports/tokens
    input_amount_int = int(input_amount * (10**from_token_decimals))

    api_link = (
        f"https://quote-api.jup.ag/v6/quote?"
        f"inputMint={from_token_mint}&outputMint={to_token_mint}&"
        f"amount={input_amount_int}&slippageBps={slippage_bps}"
    )
    log_transaction.debug(f"Jupiter Quote API Link: {api_link}")

    async with httpx.AsyncClient() as client:
        response = await client.get(api_link)
        response.raise_for_status()
        quote_data = response.json()

    # For Jupiter v6, we typically expect fields like 'outAmount', 'routePlan', etc.
    # You can do minimal checks here:
    if "outAmount" not in quote_data or "routePlan" not in quote_data:
        log_general.error(f"Unexpected quote response: {quote_data}")
    return quote_data


async def create_transaction(
    quote: dict,
    user_public_key: str,
    wrap_unwrap_sol: bool = True,
    compute_unit_price_micro_lamports: int = 20 * 14000
) -> dict:
    """
    Calls Jupiter's 'swap' endpoint to create a transaction from a quote.
    Returns a JSON with the serialized transaction in base64 form.

    :param quote: The quote dict from `create_exchange`.
    :param user_public_key: The user's (trader's) Solana public key.
    :param wrap_unwrap_sol: Whether to wrap/unwrap SOL automatically.
    :param compute_unit_price_micro_lamports: Additional compute budget cost to pay.
    :return: The JSON containing a base64-encoded transaction.
    """
    log_transaction.info(f"Creating swap transaction from quote: {quote}")

    parameters = {
        "quoteResponse": quote,
        "userPublicKey": user_public_key,
        "wrapUnwrapSOL": wrap_unwrap_sol,
        "computeUnitPriceMicroLamports": compute_unit_price_micro_lamports
    }

    async with httpx.AsyncClient() as client:
        response = await client.post("https://quote-api.jup.ag/v6/swap", json=parameters)
        response.raise_for_status()
        swap_data = response.json()

    # Now we expect "swapTransaction" in the swap response
    if "swapTransaction" not in swap_data:
        log_general.error(f"Unexpected swap response: {swap_data}")
    return swap_data


def send_transaction(
    swap_transaction_b64: str,
    keypair,
    client,
    opts: TxOpts
) -> Signature:
    """
    Decodes, signs, and sends a base64-encoded transaction to the Solana network.

    :param swap_transaction_b64: base64-encoded transaction from Jupiter.
    :param keypair: The Keypair object used to sign the transaction.
    :param client: The RPC client for sending the transaction.
    :param opts: TxOpts (preflight, block height, etc.)
    :return: The transaction signature (txid).
    """
    raw_txn = VersionedTransaction.from_bytes(base64.b64decode(swap_transaction_b64))

    # sign_message is used for the message bytes
    sig = keypair.sign_message(message.to_bytes_versioned(raw_txn.message))
    signed_txn = VersionedTransaction.populate(raw_txn.message, [sig])

    result = client.send_raw_transaction(bytes(signed_txn), opts)
    txid = result.value
    log_transaction.info(f"Soltrade TxID: {txid}")
    return txid

###############################################################################
#                            High-Level Swap Function
###############################################################################

async def perform_swap(
    sent_amount: float,
    from_token_mint: str,
    to_token_mint: str,
    from_token_decimals: int,
    to_token_decimals: int,
    slippage_bps: int,
    user_public_key,
    keypair,
    client,
    max_retries: int = 3
) -> bool:
    """
    High-level function to perform a swap from one token to another using Jupiter.
    1) Calls `create_exchange` to get a quote.
    2) Calls `create_transaction` to get a swap transaction.
    3) Signs & sends the transaction, and waits for confirmation.
    4) Retries if needed, up to `max_retries`.

    :param sent_amount: The float amount of from_token to be sent.
    :param from_token_mint: Mint address of the token being sent.
    :param to_token_mint: Mint address of the token to receive.
    :param from_token_decimals: Number of decimals for the from_token.
    :param to_token_decimals: Number of decimals for the to_token (useful for logging).
    :param slippage_bps: Slippage tolerance in basis points (1% => 100 bps).
    :param user_public_key: The trader's public key.
    :param keypair: Keypair used to sign the transaction.
    :param client: A Solana RPC client for sending the transaction.
    :param max_retries: How many times to retry the entire swap process.
    :return: True if swap succeeded, False otherwise.
    """
    log_general.info(
        f"Initiating swap: {sent_amount} of {from_token_mint} -> {to_token_mint}"
    )

    tx_error = None
    is_tx_successful = False
    swap_txid = None

    for attempt in range(max_retries):
        if is_tx_successful:
            break  # no need to retry if we already succeeded

        try:
            # 1) Get the quote
            quote = await create_exchange(
                input_amount=sent_amount,
                from_token_mint=from_token_mint,
                to_token_mint=to_token_mint,
                from_token_decimals=from_token_decimals,
                to_token_decimals=to_token_decimals,
                slippage_bps=slippage_bps
            )

            # Instead of checking "data" in `quote`, let's ensure
            # the fields we need (e.g., "outAmount") are there:
            if not quote or "outAmount" not in quote:
                raise RuntimeError(f"Invalid quote from Jupiter: {quote}")

            # 2) Create the transaction
            swap_data = await create_transaction(
                quote=quote,
                user_public_key=str(user_public_key),
                wrap_unwrap_sol=True,
                compute_unit_price_micro_lamports=20 * 14000
            )
            if "swapTransaction" not in swap_data:
                raise RuntimeError(f"Invalid swap data from Jupiter: {swap_data}")

            # 3) Sign & send
            opts = TxOpts(
                skip_preflight=False,
                preflight_commitment="confirmed",
                last_valid_block_height=find_last_valid_block_height(client)
            )
            swap_txid = send_transaction(
                swap_transaction_b64=swap_data["swapTransaction"],
                keypair=keypair,
                client=client,
                opts=opts
            )

        except Exception as e:
            # Possibly an RPCException or network error.
            if isinstance(e, RPCException):
                log_general.warning(
                    f"RPC error on attempt {attempt}: {e}. Retrying..."
                )
                continue
            else:
                # If it's some other exception, log and break.
                log_general.error(f"Swap attempt {attempt} failed: {e}")
                break

        # 4) Wait up to 3 times for confirmation
        for check_attempt in range(3):
            await asyncio.sleep(2)  # real usage might be 10-30s
            tx_error = find_transaction_error(swap_txid, client)
            if not tx_error:
                # No error => success
                is_tx_successful = True
                break
            else:
                log_general.warning(
                    f"Transaction not confirmed yet on check {check_attempt}, waiting..."
                )

    # Final outcome
    if not is_tx_successful or tx_error:
        log_general.error(
            f"Swap failed after {max_retries} attempts. Last error: {tx_error}"
        )
        return False

    # If success, log the final amounts. Jupiter normally returns "outAmount" in the quote.
    if "outAmount" in quote:
        bought_amount = int(quote["outAmount"]) / (10**to_token_decimals)
        log_transaction.info(
            f"Swapped {sent_amount} from {from_token_mint} to {bought_amount:.6f} of {to_token_mint} (txid: {swap_txid})"
        )
    else:
        log_transaction.info(
            f"Swap completed, but 'outAmount' not found in quote: {quote}"
        )
    return True
