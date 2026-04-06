"""
OPERATION COBRA SNAKE - Kalshi Trading Bot
"""

import os
import json
import time
import base64
import logging
import requests
import datetime
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

CONFIG = {
    "KALSHI_KEY_ID": os.environ.get("KALSHI_KEY_ID", ""),
    "KALSHI_PRIVATE_KEY": os.environ.get("KALSHI_PRIVATE_KEY", "").replace(" RSA PRIVATE KEY", "\nRSA PRIVATE KEY").replace("KEY----- ", "KEY-----\n").replace(" -----END", "\n-----END").replace(" MIIE", "\nMIIE").replace("d\nM", "d M"),
    "KALSHI_BASE_URL": "https://api.elections.kalshi.com/trade-api/v2",
    "NEWS_API_KEY": os.environ.get("NEWS_API_KEY", ""),
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    "BET_SIZE_TIER1": 10,
    "BET_SIZE_TIER2": 25,
    "PROFIT_TIER_THRESHOLD": 25000,
    "ALL_TIME_PROFIT_STOP": 50000,
    "MIN_BANKROLL": 20,
    "MAX_OPEN_BETS": 3,
    "MIN_CONFIDENCE": 80,
    "CASH_OUT_TARGET": 0.75,
    "STOP_LOSS_PCT": 0.15,
    "LOSS_STREAK_PAUSE": 3,
    "DEAD_HOURS_START": 2,
    "DEAD_HOURS_END": 6,
    "HEDGE_THRESHOLD": 0.80,
    "HEDGE_SIZE_PCT": 0.35,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s COBRA %(message)s")
log = logging.getLogger(__name__)

state = {
    "all_time_profit": 0,
    "starting_balance": 0,
    "current_balance": 0,
    "open_bets": {},
    "loss_streak": 0,
    "paused": False,
    "pause_reason": None,
    "total_bets": 0,
    "winning_bets": 0,
}


def load_private_key():
    key_pem = CONFIG["KALSHI_PRIVATE_KEY"]
    key_pem = key_pem.replace("\\n", "\n").strip()
    return serialization.load_pem_private_key(
        key_pem.encode(),
        password=None,
        backend=default_backend()
    )


def sign_request(method, path):
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path.split("?")[0]
    sig = load_private_key().sign(
        msg.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return {
        "KALSHI-ACCESS-KEY": CONFIG["KALSHI_KEY_ID"],
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json"
    }


def kalshi_get(path):
    r = requests.get(
        CONFIG["KALSHI_BASE_URL"] + path,
        headers=sign_request("GET", path),
        timeout=10
    )
    r.raise_for_status()
    return r.json()


def kalshi_post(path, body):
    r = requests.post(
        CONFIG["KALSHI_BASE_URL"] + path,
        headers=sign_request("POST", path),
        json=body,
        timeout=10
    )
    r.raise_for_status()
    return r.json()


def get_balance():
    try:
        data = kalshi_get("/portfolio/balance")
        balance = float(data.get("balance", 0)) / 100
        state["current_balance"] = balance
        return balance
    except Exception as e:
        log.error("Balance check failed: " + str(e))
        return state["current_balance"]


def get_latest_news():
    try:
        r = requests.get(
            "https://newsdata.io/api/1/latest",
            params={
                "apikey": CONFIG["NEWS_API_KEY"],
                "language": "en",
                "category": "politics,sports,business,world",
                "size": 10
            },
            timeout=10
        )
        results = r.json().get("results", [])
        headlines = []
        for a in results:
            headlines.append({
                "title": a.get("title", ""),
                "description": a.get("description", ""),
                "source": a.get("source_id", "")
            })
        return headlines
    except Exception:
        return []


def get_open_markets(limit=100):
    try:
        path = "/markets?status=open&limit=" + str(limit)
        return kalshi_get(path).get("markets", [])
    except Exception:
        return []


def ask_claude(news, market):
    try:
        prompt = "You are Operation Cobra Snake, an elite prediction market trading AI.\n"
        prompt += "MARKET: " + str(market.get("title", "")) + "\n"
        prompt += "TICKER: " + str(market.get("ticker", "")) + "\n"
        prompt += "YES PRICE: " + str(market.get("yes_ask_dollars", "?")) + "\n"
        prompt += "NO PRICE: " + str(market.get("no_ask_dollars", "?")) + "\n"
        prompt += "CLOSES: " + str(market.get("close_time", "?")) + "\n"
        prompt += "VOLUME: " + str(market.get("volume", 0)) + "\n"
        prompt += "NEWS: " + json.dumps(news[:5]) + "\n"
        prompt += "BALANCE: $" + str(round(state["current_balance"], 2)) + "\n"
        prompt += "PROFIT: $" + str(round(state["all_time_profit"], 2)) + "\n"
        prompt += "Only recommend bet at 80+ confidence based on news.\n"
        prompt += "Respond ONLY with this exact JSON:\n"
        prompt += "{\"bet\":true,\"side\":\"yes\",\"confidence\":85,\"reasoning\":\"reason here\",\"hedge\":false,\"hedge_side\":\"no\"}"

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CONFIG["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15
        )
        text = r.json()["content"][0]["text"]
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        return {"bet": False, "confidence": 0, "reasoning": str(e)}


def get_bet_size():
    if state["all_time_profit"] >= CONFIG["PROFIT_TIER_THRESHOLD"]:
        return CONFIG["BET_SIZE_TIER2"]
    return CONFIG["BET_SIZE_TIER1"]


def place_bet(ticker, side, size):
    try:
        markets = get_open_markets()
        market = None
        for m in markets:
            if m["ticker"] == ticker:
                market = m
                break
        if not market:
            return None
        price = float(market.get(side + "_ask_dollars", 0.50))
        count = max(1, int(size / price))
        result = kalshi_post("/portfolio/orders", {
            "ticker": ticker,
            "side": side,
            "action": "buy",
            "count": count,
            "type": "market"
        })
        order_id = result.get("order", {}).get("order_id")
        if order_id:
            state["open_bets"][order_id] = {
                "ticker": ticker,
                "side": side,
                "count": count,
                "entry_price": price,
                "hedge_placed": False
            }
            state["total_bets"] += 1
            log.info("BET PLACED: " + side.upper() + " on " + ticker + " $" + str(size))
        return order_id
    except Exception as e:
        log.error("Bet failed: " + str(e))
        return None


def close_bet(ticker, side, count):
    try:
        close_side = "no" if side == "yes" else "yes"
        kalshi_post("/portfolio/orders", {
            "ticker": ticker,
            "side": close_side,
            "action": "buy",
            "count": count,
            "type": "market"
        })
        log.info("CLOSED: " + ticker)
    except Exception as e:
        log.error("Close failed: " + str(e))


def monitor_positions():
    if not state["open_bets"]:
        return
    markets = get_open_markets()
    market_map = {}
    for m in markets:
        market_map[m["ticker"]] = m

    for order_id in list(state["open_bets"].keys()):
        bet = state["open_bets"][order_id]
        ticker = bet["ticker"]
        side = bet["side"]
        entry = bet["entry_price"]
        count = bet["count"]

        if ticker not in market_map:
            del state["open_bets"][order_id]
            continue

        market = market_map[ticker]
        current = float(market.get(side + "_ask_dollars", entry))
        change = current - entry
        log.info("WATCHING " + ticker + " Entry:" + str(round(entry, 2)) + " Now:" + str(round(current, 2)))

        if current >= CONFIG["CASH_OUT_TARGET"]:
            log.info("CASHING OUT " + ticker + " at " + str(current))
            close_bet(ticker, side, count)
            state["all_time_profit"] += change * count
            state["winning_bets"] += 1
            state["loss_streak"] = 0
            del state["open_bets"][order_id]

        elif current >= CONFIG["HEDGE_THRESHOLD"] and not bet["hedge_placed"]:
            hedge_side = "no" if side == "yes" else "yes"
            hedge_size = get_bet_size() * CONFIG["HEDGE_SIZE_PCT"]
            place_bet(ticker, hedge_side, hedge_size)
            bet["hedge_placed"] = True

        elif change <= -CONFIG["STOP_LOSS_PCT"]:
            try:
                close_time = market.get("close_time", "")
                closes_at = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                mins_left = (closes_at - now).total_seconds() / 60
                if mins_left < 30:
                    log.info("CUTTING " + ticker + " " + str(round(mins_left)) + "min left")
                    close_bet(ticker, side, count)
                    state["all_time_profit"] += change * count
                    state["loss_streak"] += 1
                    del state["open_bets"][order_id]
                else:
                    log.info("HOLDING " + ticker + " " + str(round(mins_left)) + "min left game still live")
            except Exception:
                pass


def safety_check():
    balance = get_balance()
    if balance < CONFIG["MIN_BANKROLL"]:
        log.warning("Balance too low: $" + str(round(balance, 2)))
        return False
    if state["all_time_profit"] >= CONFIG["ALL_TIME_PROFIT_STOP"]:
        state["paused"] = True
        state["pause_reason"] = "$50000 HIT! Cash out and re-deposit $100 to restart."
        return False
    if state["loss_streak"] >= CONFIG["LOSS_STREAK_PAUSE"]:
        log.warning("3 losses in a row - pausing 1 hour")
        time.sleep(3600)
        state["loss_streak"] = 0
    hour = datetime.datetime.now().hour
    if CONFIG["DEAD_HOURS_START"] <= hour < CONFIG["DEAD_HOURS_END"]:
        log.info("Dead hours - sleeping")
        return False
    if len(state["open_bets"]) >= CONFIG["MAX_OPEN_BETS"]:
        return False
    return True


def cobra_loop():
    log.info("OPERATION COBRA SNAKE ACTIVATED")
    log.info("Balance: $" + str(round(get_balance(), 2)))
    state["starting_balance"] = state["current_balance"]

    while True:
        try:
            if state["paused"]:
                log.warning("PAUSED: " + str(state["pause_reason"]))
                time.sleep(60)
                continue

            if not safety_check():
                time.sleep(30)
                continue

            monitor_positions()

            news = get_latest_news()
            all_markets = get_open_markets()
            markets = []
            for m in all_markets:
                if m.get("volume", 0) > 50 and m.get("yes_ask_dollars"):
                    markets.append(m)

            log.info("Scanning " + str(len(markets)) + " markets with " + str(len(news)) + " headlines")

            for market in markets[:10]:
                if len(state["open_bets"]) >= CONFIG["MAX_OPEN_BETS"]:
                    break
                decision = ask_claude(news, market)
                if decision.get("bet") and decision.get("confidence", 0) >= CONFIG["MIN_CONFIDENCE"]:
                    log.info("OPPORTUNITY: " + str(market["title"]) + " " + str(decision["confidence"]) + "% " + str(decision["reasoning"]))
                    place_bet(market["ticker"], decision["side"], get_bet_size())

            if state["total_bets"] > 0:
                win_rate = round(state["winning_bets"] / state["total_bets"] * 100)
            else:
                win_rate = 0

            log.info("Balance:$" + str(round(state["current_balance"], 2)) + " PnL:$" + str(round(state["all_time_profit"], 2)) + " WinRate:" + str(win_rate) + "% OpenBets:" + str(len(state["open_bets"])))
            time.sleep(60)

        except KeyboardInterrupt:
            log.info("Cobra Snake signing off.")
            break
        except Exception as e:
            log.error("Loop error: " + str(e))
            time.sleep(30)


if __name__ == "__main__":
    cobra_loop()
