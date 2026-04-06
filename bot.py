"""
🐍 OPERATION COBRA SNAKE — Kalshi Trading Bot
Stealth. Fast. Profitable.
"""

import os
import json
import time
import hmac
import hashlib
import base64
import logging
import requests
import datetime
from threading import Timer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

CONFIG = {
    "KALSHI_KEY_ID": os.environ.get("KALSHI_KEY_ID", ""),
    "KALSHI_PRIVATE_KEY": os.environ.get("KALSHI_PRIVATE_KEY", ""),
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s 🐍 %(message)s')
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
    return serialization.load_pem_private_key(key_pem.encode(), password=None, backend=default_backend())

def sign_request(method, path):
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path.split("?")[0]
    sig = load_private_key().sign(msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": CONFIG["KALSHI_KEY_ID"], "KALSHI-ACCESS-TIMESTAMP": ts, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(), "Content-Type": "application/json"}

def kalshi_get(path):
    r = requests.get(CONFIG["KALSHI_BASE_URL"] + path, headers=sign_request("GET", path), timeout=10)
    r.raise_for_status()
    return r.json()

def kalshi_post(path, body):
    r = requests.post(CONFIG["KALSHI_BASE_URL"] + path, headers=sign_request("POST", path), json=body, timeout=10)
    r.raise_for_status()
    return r.json()

def get_balance():
    try:
        data = kalshi_get("/portfolio/balance")
        state["current_balance"] = float(data.get("balance", 0)) / 100
        return state["current_balance"]
    except Exception as e:
        log.error(f"Balance check failed: {e}")
        return state["current_balance"]

def get_open_positions():
    try:
        return kalshi_get("/portfolio/positions").get("market_positions", [])
    except:
        return []

def get_latest_news():
    try:
        r = requests.get("https://newsdata.io/api/1/latest", params={"apikey": CONFIG["NEWS_API_KEY"], "language": "en", "category": "politics,sports,business,world", "size": 10}, timeout=10)
        return [{"title": a.get("title",""), "description": a.get("description",""), "source": a.get("source_id","")} for a in r.json().get("results", [])]
    except:
        return []

def get_open_markets(limit=100):
    try:
        return kalshi_get(f"/markets?status=open&limit={limit}").get("markets", [])
    except:
        return []

def ask_claude(news, market):
    try:
        prompt = f"""You are Operation Cobra Snake — elite prediction market AI.
MARKET: {market.get('title','')}
TICKER: {market.get('ticker','')}
YES PRICE: {market.get('yes_ask_dollars','?')}
NO PRICE: {market.get('no_ask_dollars','?')}
CLOSES: {market.get('close_time','?')}
VOLUME: {market.get('volume',0)}
NEWS: {json.dumps(news[:5])}
BALANCE: ${state['current_balance']:.2f}
ALL TIME PROFIT: ${state['all_time_profit']:.2f}

Analyze if news creates mispriced opportunity. Only recommend at 80%+ confidence.
Respond ONLY in JSON:
{{"bet":true/false,"side":"yes"/"no","confidence":0-100,"reasoning":"brief","hedge":true/false,"hedge_side":"yes"/"no"}}"""
        r = requests.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": CONFIG["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"}, json={"model": "claude-sonnet-4-20250514", "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]}, timeout=15)
        text = r.json()["content"][0]["text"].replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        return {"bet": False, "confidence": 0, "reasoning": str(e)}

def get_bet_size():
    return CONFIG["BET_SIZE_TIER2"] if state["all_time_profit"] >= CONFIG["PROFIT_TIER_THRESHOLD"] else CONFIG["BET_SIZE_TIER1"]

def place_bet(ticker, side, size):
    try:
        markets = get_open_markets()
        market = next((m for m in markets if m["ticker"] == ticker), None)
        if not market: return None
        price = float(market.get(f"{side}_ask_dollars", 0.50))
        count = max(1, int(size / price))
        result = kalshi_post("/portfolio/orders", {"ticker": ticker, "side": side, "action": "buy", "count": count, "type": "market"})
        order_id = result.get("order", {}).get("order_id")
        if order_id:
            state["open_bets"][order_id] = {"ticker": ticker, "side": side, "count": count, "entry_price": price, "hedge_placed": False}
            state["total_bets"] += 1
            log.info(f"✅ BET PLACED: {side.upper()} on {ticker} | ${size} | {count} contracts @ {price}")
        return order_id
    except Exception as e:
        log.error(f"Bet failed: {e}")

def close_bet(ticker, side, count):
    try:
        close_side = "no" if side == "yes" else "yes"
        kalshi_post("/portfolio/orders", {"ticker": ticker, "side": close_side, "action": "buy", "count": count, "type": "market"})
        log.info(f"💰 CLOSED: {ticker}")
    except Exception as e:
        log.error(f"Close failed: {e}")

def monitor_positions():
    if not state["open_bets"]: return
    markets = get_open_markets()
    market_map = {m["ticker"]: m for m in markets}
    for order_id, bet in list(state["open_bets"].items()):
        ticker, side, entry = bet["ticker"], bet["side"], bet["entry_price"]
        if ticker not in market_map: del state["open_bets"][order_id]; continue
        market = market_map[ticker]
        current = float(market.get(f"{side}_ask_dollars", entry))
        change = current - entry
        count = bet["count"]
        log.info(f"👀 {ticker} | Entry:{entry:.2f} Now:{current:.2f} Change:{change:+.2f}")
        if current >= CONFIG["CASH_OUT_TARGET"]:
            log.info(f"🎉 CASHING OUT {ticker} at {current:.2f}")
            close_bet(ticker, side, count)
            state["all_time_profit"] += change * count
            state["winning_bets"] += 1
            state["loss_streak"] = 0
            del state["open_bets"][order_id]
        elif current >= CONFIG["HEDGE_THRESHOLD"] and not bet["hedge_placed"]:
            hedge_side = "no" if side == "yes" else "yes"
            place_bet(ticker, hedge_side, get_bet_size() * CONFIG["HEDGE_SIZE_PCT"])
            bet["hedge_placed"] = True
        elif change <= -CONFIG["STOP_LOSS_PCT"]:
            close_time = market.get("close_time","")
            try:
                closes_at = datetime.datetime.fromisoformat(close_time.replace("Z","+00:00"))
                mins_left = (closes_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60
                if mins_left < 30:
                    log.info(f"🛑 CUTTING {ticker} | {mins_left:.0f}min left")
                    close_bet(ticker, side, count)
                    state["all_time_profit"] += change * count
                    state["loss_streak"] += 1
                    del state["open_bets"][order_id]
                else:
                    log.info(f"⏳ Holding {ticker} — {mins_left:.0f}min left, game still live")
            except: pass

def safety_check():
    balance = get_balance()
    if balance < CONFIG["MIN_BANKROLL"]:
        state["paused"] = True
        state["pause_reason"] = "💸 Bankroll too low — bot stopped"
        return False
    if state["all_time_profit"] >= CONFIG["ALL_TIME_PROFIT_STOP"]:
        state["paused"] = True
        state["pause_reason"] = "🎉 $50,000 HIT! Cash out and re-deposit $100 to restart."
        return False
    if state["loss_streak"] >= CONFIG["LOSS_STREAK_PAUSE"]:
        log.warning("😤 3 losses in a row — pausing 1 hour")
        time.sleep(3600)
        state["loss_streak"] = 0
    hour = datetime.datetime.now().hour
    if CONFIG["DEAD_HOURS_START"] <= hour < CONFIG["DEAD_HOURS_END"]:
        log.info("😴 Dead hours — sleeping")
        return False
    if len(state["open_bets"]) >= CONFIG["MAX_OPEN_BETS"]:
        return False
    return True

def cobra_loop():
    log.info("🐍 Operation Cobra Snake — ACTIVATED")
    log.info(f"💰 Balance: ${get_balance():.2f}")
    state["starting_balance"] = state["current_balance"]
    while True:
        try:
            if state["paused"]:
                log.warning(f"🛑 PAUSED: {state['pause_reason']}")
                time.sleep(60)
                continue
            if not safety_check():
                time.sleep(30)
                continue
            monitor_positions()
            news = get_latest_news()
            markets = [m for m in get_open_markets() if m.get("volume",0) > 50 and m.get("yes_ask_dollars")]
            log.info(f"🔍 {len(markets)} markets | 📰 {len(news)} headlines")
            for market in markets[:10]:
                if len(state["open_bets"]) >= CONFIG["MAX_OPEN_BETS"]: break
                decision = ask_claude(news, market)
                if decision.get("bet") and decision.get("confidence",0) >= CONFIG["MIN_CONFIDENCE"]:
                    log.info(f"⚡ {market['title']} | {decision['confidence']}% | {decision['reasoning']}")
                    place_bet(market["ticker"], decision["side"], get_bet_size())
            win_rate = (state["winning_bets"]/state["total_bets"]*100) if state["total_bets"] > 0 else 0
            log.info(f"📊 Balance:${state['current_balance']:.2f} | P&L:${state['all_time_profit']:+.2f} | WinRate:{win_rate:.0f}% | OpenBets:{len(state['open_bets'])}")
            time.sleep(60)
        except KeyboardInterrupt:
            log.info("🐍 Cobra Snake signing off.")
            break
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    cobra_loop()
