import requests
import asyncio
import pandas as pd

from apscheduler.schedulers.background import BlockingScheduler

from soltrade.transactions import perform_swap, market
from soltrade.indicators import calculate_ema, calculate_rsi, calculate_bbands
from soltrade.wallet import find_balance
from soltrade.log import log_general, log_transaction
from soltrade.config import config

market('position.json')

# Pulls the candlestick information in fifteen minute intervals
def fetch_candlestick(
    from_symbol: str,
    to_symbol: str,
    api_key: str,
    trading_interval_minutes: int,
    limit: int = 50
) -> dict:
    """
    Fetches candlestick data for `from_symbol` => `to_symbol` pair.
    """
    url = "https://min-api.cryptocompare.com/data/v2/histominute"
    headers = {'authorization': api_key}
    params = {
        'tsym': to_symbol,      # target symbol
        'fsym': from_symbol,    # base symbol
        'limit': limit,
        'aggregate': trading_interval_minutes
    }
    response = requests.get(url, headers=headers, params=params)
    data = response.json()
    if data.get('Response') == 'Error':
        log_general.error(data.get('Message'))
        raise RuntimeError("Error fetching candlestick data.")
    return data

def get_trade_amount(
    balance: float,
    max_amount: float
) -> float:
    """
    Returns how much to trade given a wallet balance and a user-defined max amount.
    Trades the lesser of (balance, max_amount).
    """
    return min(balance, max_amount)



def analyze_market(
    from_symbol: str,
    to_symbol: str,
    api_key: str,
    trading_interval_minutes: int,
    limit: int = 50
) -> Dict[str, Any]:
    """
    Fetches candlestick data, calculates technical indicators, and returns
    a dictionary of relevant market/indicator info for downstream logic.

    Returns a dictionary that might look like:
    {
        "price": <float>,
        "ema_short": <float>,
        "ema_medium": <float>,
        "rsi": <float>,
        "upper_bb": <float>,
        "lower_bb": <float>
        # plus anything else you'd like to track
    }
    """
    # 1) Fetch candlestick data
    data_json = fetch_candlestick(
        from_symbol=from_symbol,
        to_symbol=to_symbol,
        api_key=api_key,
        trading_interval_minutes=trading_interval_minutes,
        limit=limit
    )
    # 2) Convert to DataFrame
    candle_dict = data_json["Data"]["Data"]
    columns = ["close", "high", "low", "open", "time", "VF", "VT"]
    df = pd.DataFrame(candle_dict, columns=columns)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    # 3) Compute indicators
    cl = df["close"]
    price = cl.iat[-1]
    ema_short = calculate_ema(df, length=5)
    ema_medium = calculate_ema(df, length=20)
    rsi_val = calculate_rsi(df, length=14)
    upper_bb, lower_bb = calculate_bbands(df, length=14)

    # 4) Prepare the dictionary of info
    result = {
        "price": price,
        "ema_short": ema_short,
        "ema_medium": ema_medium,
        "rsi": rsi_val,
        "upper_bb": upper_bb.iat[-1],
        "lower_bb": lower_bb.iat[-1],
        "df": df
    }
    log_general.debug(f"analysis result: {result}")
    return result

def execute_trades(
    analysis_info: dict,
    from_token_mint: str,
    to_token_mint: str,
    max_amount_from: float,
    max_amount_to: float,
    stoploss: float,
    takeprofit: float
) -> None:
    """
    Uses the analysis info (indicators, price) to decide whether to buy, sell, or hold.
    Executes trades by calling `perform_swap(...)`.

    - `analysis_info`: a dictionary from `analyze_market(...)` containing price, rsi, ema, etc.
    - `from_token_mint`, `to_token_mint`: which tokens we are swapping between
    - `max_amount_from`, `max_amount_to`: user-defined max trade amounts
    - `stoploss`, `takeprofit`: current levels from the market() object or user strategy
    """

    price = analysis_info["price"]
    ema_short = analysis_info["ema_short"]
    ema_medium = analysis_info["ema_medium"]
    rsi_val = analysis_info["rsi"]
    upper_bb = analysis_info["upper_bb"]
    lower_bb = analysis_info["lower_bb"]

    # load current position info
    current_market = market()   # or market('position.json') if needed
    current_market.load_position()
    is_open = current_market.position  # bool
    stoploss_current = current_market.sl
    takeprofit_current = current_market.tp

    log_general.debug(f"""
    Market position: {is_open}
    Price: {price}
    short_ema: {ema_short}
    med_ema:   {ema_medium}
    upper_bb:  {upper_bb}
    lower_bb:  {lower_bb}
    rsi:       {rsi_val}
    stop_loss: {stoploss_current}
    takeprofit:{takeprofit_current}
    """)

    # If there's NO open position => check buy signals
    if not is_open:
        balance_from = find_balance(from_token_mint)
        input_amount = get_trade_amount(balance_from, max_amount_from)

        # Example buy condition:
        if (ema_short > ema_medium or price < lower_bb) and (rsi_val <= 31):
            if input_amount <= 0:
                log_transaction.warning("Buy signal, but insufficient balance.")
                return
            log_transaction.info("Soltrade detected a BUY signal.")

            is_swapped = asyncio.run(
                perform_swap(
                    sent_amount=input_amount,
                    from_token_mint=from_token_mint,
                    to_token_mint=to_token_mint
                )
            )

            if is_swapped:
                # Example: Set stoploss at 92.5% of current price, takeprofit at 125%.
                new_sl = price * 0.925
                new_tp = price * 1.25
                current_market.update_position(True, new_sl, new_tp)
        return

    # If there's an OPEN position => check sell signals
    else:
        balance_to = find_balance(to_token_mint)
        input_amount = get_trade_amount(balance_to, max_amount_to)

        if price <= stoploss_current or price >= takeprofit_current:
            log_transaction.info("Soltrade detected SELL: stoploss/takeprofit triggered.")
            is_swapped = asyncio.run(
                perform_swap(
                    sent_amount=input_amount,
                    from_token_mint=to_token_mint,
                    to_token_mint=from_token_mint
                )
            )
            if is_swapped:
                current_market.update_position(False, 0, 0)
            return

        if (ema_short < ema_medium or price > upper_bb) and (rsi_val >= 68):
            log_transaction.info("Soltrade detected SELL: EMA/BB threshold triggered.")
            is_swapped = asyncio.run(
                perform_swap(
                    sent_amount=input_amount,
                    from_token_mint=to_token_mint,
                    to_token_mint=from_token_mint
                )
            )
            if is_swapped:
                current_market.update_position(False, 0, 0)
            return
        

def analyze_and_trade(
    from_token_mint: str,
    to_token_mint: str,
    from_token_symbol: str,
    to_token_symbol: str,
    max_amount_from: float,
    max_amount_to: float,
    stoploss: float,
    takeprofit: float,
    trading_interval_minutes: int,
    api_key: str,
    limit: int = 50
):
    """
    Orchestrates the market analysis and then executes the trade logic.
    This function is typically scheduled to run periodically.
    """

    # Step 1: Analyze
    analysis_result = analyze_market(
        from_symbol=from_token_symbol,
        to_symbol=to_token_symbol,
        api_key=api_key,
        trading_interval_minutes=trading_interval_minutes,
        limit=limit
    )

    # Step 2: Trade
    execute_trades(
        analysis_info=analysis_result,
        from_token_mint=from_token_mint,
        to_token_mint=to_token_mint,
        max_amount_from=max_amount_from,
        max_amount_to=max_amount_to,
        stoploss=stoploss,
        takeprofit=takeprofit
    )


# This starts the trading function on a timer
def start_trading():
    log_general.info("Soltrade has now initialized the trading algorithm.")

    trading_sched = BlockingScheduler()
    trading_sched.add_job(analyze_and_trade, 'interval', seconds=config().price_update_seconds, max_instances=1)
    trading_sched.start()
    analyze_and_trade()