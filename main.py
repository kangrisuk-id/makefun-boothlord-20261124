"""Late-stage organic strength: a pump.fun paper-trading bot for agent-fun/replay/v2.

Strategy in one paragraph: most pump.fun coins are bundled launches that spike
and die within minutes. This bot ignores a coin until its bonding curve holds
real reserves of 8 SOL, then watches how the money arrived. It buys a small
fixed stake (0.05 SOL, at most five positions, never twice on the same coin)
only when a coin already sits late in its curve life (20-85 SOL of real
reserves, at least 80% of its own peak), has broad organic participation
(>= 40 trades, >= 20 distinct buyers, no single buyer dominating, the five
biggest net buyers under 65% of net inflow, recent flow still buy-tilted),
shows no launch bundle (reserves in the first ten slots stayed under 12 SOL),
its creator has never sold, and its price is holding (at least 97% of where
it stood ~150 slots earlier). One more guard sits on top: if the price is
in a vertical move (more than +4% against ~10 slots earlier) the bot waits
for a later batch instead of buying the spike — with two slots of order
latency, chasing a vertical move is how entries become knife-catches.
Exits are mechanical: take profit at +90%, stop loss at -15%, a trailing
stop that arms at +45% and fires 12% under the peak, a stale exit after
3,000 slots without a trade, and a hard exit after 8,000 slots in the
position.

Pure Python standard library. All price comparisons use exact integer
cross-multiplication on the curve's virtual reserves, so the two graded
executions agree byte for byte: no clocks, no environment reads, no random.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script dir off sys.path)

SOL = makefun_bot.LAMPORTS_PER_SOL

# --- position sizing -------------------------------------------------------
STAKE_LAMPORTS = SOL // 20            # 0.05 SOL per entry
MAX_POSITIONS = 5                     # the harness cap, and our own
BUY_SLIPPAGE_BPS = 2_000              # entries: refuse a fill 20% worse than decided
SELL_SLIPPAGE_BPS = 5_000             # exits: execution matters more than price
MAX_BUY_ATTEMPTS = 4                  # per coin; a refused entry may be retried

# --- entry filters ----------------------------------------------------------
TRACK_RQ = 8 * SOL                    # deep tracking starts at 8 SOL real reserves
MIN_RQ = 20 * SOL                     # entry band on real quote reserves ...
MAX_RQ = 85 * SOL                     # ... up to the graduation zone
PEAK_KEEP_NUM = 4                     # current reserves >= 4/5 of the coin's peak
PEAK_KEEP_DEN = 5
MIN_TRADES = 40                       # recorded trades on the curve
MIN_BUYERS = 20                       # distinct wallets that ever bought
WINDOW_SLOTS = 500                    # flow window for the buy-share test
MIN_WINDOW_TRADES = 5                 # the window must show real activity
BUY_SHARE_NUM = 52                    # window buy quote >= 52% of window quote
BUY_SHARE_DEN = 100
TOP5_LIMIT_NUM = 65                   # five biggest net buyers < 65% of net inflow
TOP5_LIMIT_DEN = 100
BIGGEST_BUY_NUM = 35                  # largest single buy <= 35% of all buy quote
BIGGEST_BUY_DEN = 100
EARLY_SLOTS = 10                      # anti-bundle window after the create slot
EARLY_RQ_LIMIT = 12 * SOL             # reserves inside it must stay <= 12 SOL
STABLE_LAG = 150                      # price reference ~150 slots back
STABLE_NUM = 97                       # price >= 97% of that reference
STABLE_DEN = 100
SPIKE_LAG = 10                        # vertical-move lookback for the entry guard
SPIKE_NUM, SPIKE_DEN = 26, 25         # +4% inside that lookback: do not buy yet
SAMPLE_KEEP = 3_000                   # slots of price samples retained per coin

# --- exits ------------------------------------------------------------------
TP_NUM, TP_DEN = 19, 10               # take profit at +90% over entry
SL_NUM, SL_DEN = 17, 20               # stop loss at -15% under entry
TRAIL_ARM_NUM, TRAIL_ARM_DEN = 29, 20  # trailing arms at +45% over entry
TRAIL_DROP_NUM, TRAIL_DROP_DEN = 22, 25  # ... and fires at 88% of the peak
STALE_SLOTS = 3_000                   # no trade for this long: leave
MAX_HOLD_SLOTS = 8_000                # never hold longer than this


class Coin:
    """Everything the strategy remembers about one mint."""

    __slots__ = (
        "mint", "creator", "created_slot", "complete",
        "early_rq_peak", "creator_sold", "tracked",
        "rq_now", "peak_rq", "trades", "last_trade_slot",
        "buyers", "flows", "window", "win_buy", "win_total",
        "biggest_buy", "total_buy_quote", "samples",
        "bought_ever", "attempts",
    )

    def __init__(self, mint, creator, created_slot):
        self.mint = mint
        self.creator = creator
        self.created_slot = created_slot
        self.complete = False
        self.early_rq_peak = 0
        self.creator_sold = False
        self.tracked = False
        self.rq_now = 0
        self.peak_rq = 0
        self.trades = 0
        self.last_trade_slot = created_slot
        self.buyers = set()
        self.flows = {}
        self.window = deque()
        self.win_buy = 0
        self.win_total = 0
        self.biggest_buy = 0
        self.total_buy_quote = 0
        self.samples = deque()
        self.bought_ever = False
        self.attempts = 0


class Position:
    """An open position: entry fill facts plus the running price peak."""

    __slots__ = ("mint", "entry_slot", "entry_quote", "entry_tokens",
                 "peak_vq", "peak_vt", "sell_pending")

    def __init__(self, mint, entry_slot, entry_quote, entry_tokens, vq, vt):
        self.mint = mint
        self.entry_slot = entry_slot
        self.entry_quote = entry_quote      # lamports the buy fill spent
        self.entry_tokens = entry_tokens    # tokens the buy fill received
        self.peak_vq = vq                   # peak price kept as a reserves pair
        self.peak_vt = vt
        self.sell_pending = False


coins = {}          # mint -> Coin, in first-seen order
positions = {}      # mint -> Position
open_orders = {}    # client_id -> (mint, side)
buy_pending = set()  # mints with a buy in flight


def _price_ge(vq_a, vt_a, vq_b, vt_b, num, den):
    """Is price A >= (num/den) * price B? Prices are virtual quote/token pairs."""
    return vq_a * vt_b * den >= num * vq_b * vt_a


def _note_trade(coin, e, slot):
    """Fold one recorded TradeEvent into the coin's memory."""
    rq = int(e["real_quote_reserves"])
    coin.rq_now = rq
    if rq > coin.peak_rq:
        coin.peak_rq = rq
    coin.trades += 1
    coin.last_trade_slot = slot
    if slot - coin.created_slot <= EARLY_SLOTS and rq > coin.early_rq_peak:
        coin.early_rq_peak = rq
    user = e["user"]
    quote = int(e["quote_amount"])
    if user == coin.creator and not e["is_buy"]:
        coin.creator_sold = True
    if not coin.tracked:
        if rq >= TRACK_RQ:
            coin.tracked = True
        else:
            return
    if e["is_buy"]:
        coin.buyers.add(user)
        coin.flows[user] = coin.flows.get(user, 0) + quote
        coin.total_buy_quote += quote
        if quote > coin.biggest_buy:
            coin.biggest_buy = quote
        coin.win_buy += quote
    else:
        coin.flows[user] = coin.flows.get(user, 0) - quote
    coin.win_total += quote
    coin.window.append((slot, bool(e["is_buy"]), quote))
    coin.samples.append((slot, int(e["virtual_quote_reserves"]), int(e["virtual_token_reserves"])))
    # Keep the sample book small: drop all but the newest sample older than
    # the stability horizon, so a reference ~STABLE_LAG back always survives.
    while len(coin.samples) >= 2 and coin.samples[1][0] <= slot - SAMPLE_KEEP:
        coin.samples.popleft()


def _window_buy_share(coin, slot):
    """Prune the flow window to the last WINDOW_SLOTS; return (buy, total, n)."""
    win = coin.window
    cutoff = slot - WINDOW_SLOTS
    while win and win[0][0] < cutoff:
        _s, was_buy, quote = win.popleft()
        coin.win_total -= quote
        if was_buy:
            coin.win_buy -= quote
    return coin.win_buy, coin.win_total, len(win)


def _reference_price(coin, slot):
    """The newest price sample at least STABLE_LAG slots old, or None."""
    ref = None
    for s, vq, vt in coin.samples:
        if s > slot - STABLE_LAG:
            break
        ref = (vq, vt)
    return ref


def _spiking(coin, curve, slot):
    """True when the price is in a vertical move: more than +4% above the
    newest sample at least SPIKE_LAG slots old. Buying into that move is
    how the latency window turns an entry into a knife-catch; the coin is
    simply re-read on later batches, so no candidacy is burned."""
    ref = None
    for s, vq, vt in coin.samples:
        if s > slot - SPIKE_LAG:
            break
        ref = (vq, vt)
    if ref is None:
        return False
    vq_now = curve.virtual_quote_reserves
    vt_now = curve.virtual_token_reserves
    return vq_now * ref[1] * SPIKE_DEN > SPIKE_NUM * ref[0] * vt_now


def _entry_ok(coin, curve, slot):
    """Every entry filter, cheapest first. curve is the shim's live curve."""
    if coin.bought_ever or coin.complete or coin.creator_sold:
        return False
    if not coin.tracked:
        return False
    rq = coin.rq_now
    if rq < MIN_RQ or rq > MAX_RQ:
        return False
    if rq * PEAK_KEEP_DEN < coin.peak_rq * PEAK_KEEP_NUM:
        return False
    if coin.trades < MIN_TRADES or len(coin.buyers) < MIN_BUYERS:
        return False
    if coin.early_rq_peak > EARLY_RQ_LIMIT:
        return False
    win_buy, win_total, win_n = _window_buy_share(coin, slot)
    if win_n < MIN_WINDOW_TRADES or win_total <= 0:
        return False
    if win_buy * BUY_SHARE_DEN < win_total * BUY_SHARE_NUM:
        return False
    if coin.total_buy_quote <= 0:
        return False
    if coin.biggest_buy * BIGGEST_BUY_DEN > coin.total_buy_quote * BIGGEST_BUY_NUM:
        return False
    positives = sorted((v for v in coin.flows.values() if v > 0), reverse=True)
    inflow = sum(positives)
    if inflow <= 0:
        return False
    if sum(positives[:5]) * TOP5_LIMIT_DEN >= inflow * TOP5_LIMIT_NUM:
        return False
    ref = _reference_price(coin, slot)
    if ref is None:
        return False
    if not _price_ge(curve.virtual_quote_reserves, curve.virtual_token_reserves,
                     ref[0], ref[1], STABLE_NUM, STABLE_DEN):
        return False
    if _spiking(coin, curve, slot):
        return False
    return True


def _exit_reason(pos, curve, coin, slot):
    """Why this position should close now, or None to keep holding."""
    vq = curve.virtual_quote_reserves
    vt = curve.virtual_token_reserves
    if vt <= 0:
        return None
    # Track the peak price as an exact reserves pair.
    if vq * pos.peak_vt > pos.peak_vq * vt:
        pos.peak_vq, pos.peak_vt = vq, vt
    eq, et = pos.entry_quote, pos.entry_tokens
    if _price_ge(vq, vt, eq, et, TP_NUM, TP_DEN):
        return "take_profit"
    if vq * et * SL_DEN <= SL_NUM * eq * vt:
        return "stop_loss"
    if _price_ge(pos.peak_vq, pos.peak_vt, eq, et, TRAIL_ARM_NUM, TRAIL_ARM_DEN):
        if vq * pos.peak_vt * TRAIL_DROP_DEN <= TRAIL_DROP_NUM * pos.peak_vq * vt:
            return "trailing_stop"
    if slot - coin.last_trade_slot >= STALE_SLOTS:
        return "stale"
    if slot - pos.entry_slot >= MAX_HOLD_SLOTS:
        return "max_hold"
    return None


def on_batch(state, batch):
    slot = batch["slot"]
    orders = []

    # 1. Fills first: they settle orders sent two slots ago.
    for f in batch["fills"]:
        cid = f["client_id"]
        if cid not in open_orders:
            continue
        mint, side = open_orders.pop(cid)
        if side == "buy":
            buy_pending.discard(mint)
            coin = coins.get(mint)
            if f["status"] == "filled":
                if coin is not None:
                    coin.bought_ever = True
                curve = state.curves.get(mint)
                vq = curve.virtual_quote_reserves if curve else 0
                vt = curve.virtual_token_reserves if curve else 1
                positions[mint] = Position(
                    mint, int(f["slot"]), int(f["quote_lamports"]),
                    int(f["token_amount"]), vq, vt)
            elif f["status"] == "failed" and coin is not None:
                # The transaction burned its fee and never filled: do not
                # pay that toll again on the same coin.
                coin.bought_ever = True
            elif f["status"] == "rejected" and f.get("reason") == "max_slippage" and coin is not None:
                # The price ran more than the slippage band inside the
                # latency window: that coin is in a blow-off this bot
                # does not chase, ever.
                coin.bought_ever = True
            # Any other rejection cost nothing: the coin may be retried
            # while its attempt budget lasts.
        else:
            pos = positions.get(mint)
            if pos is not None:
                pos.sell_pending = False
                if f["status"] == "filled":
                    del positions[mint]

    # A position the harness closed itself (curve completed, final flatten)
    # disappears from the account: forget it here too.
    for mint in list(positions):
        if mint not in state.positions:
            del positions[mint]

    # 2. Fold this batch's market events into the coin memories.
    traded = []
    for e in batch["events"]:
        kind = e["event"]
        mint = e["mint"]
        if kind == "CreateEvent":
            if mint not in coins:
                coins[mint] = Coin(mint, e["creator"], int(e["slot"]))
            continue
        coin = coins.get(mint)
        if coin is None:
            coin = coins[mint] = Coin(mint, e.get("creator", ""), int(e["slot"]))
        if kind == "TradeEvent":
            _note_trade(coin, e, int(e["slot"]))
            traded.append(mint)
        elif kind == "CompleteEvent":
            coin.complete = True

    caps = state.hello["caps"]

    # 3. Exits, before entries, so a closing coin frees its slot at once
    #    (the fill lands two slots later, but the intent is recorded now).
    for mint, pos in list(positions.items()):
        if pos.sell_pending:
            continue
        curve = state.curves.get(mint)
        coin = coins.get(mint)
        if curve is None or coin is None:
            continue
        if _exit_reason(pos, curve, coin, slot) is None:
            continue
        held = state.positions.get(mint)
        if held is None or held.token_amount <= 0:
            continue
        cid = state.client_id()
        orders.append({"side": "sell", "client_id": cid, "mint": mint,
                       "token_amount": held.token_amount,
                       "max_slippage_bps": SELL_SLIPPAGE_BPS})
        open_orders[cid] = (mint, "sell")
        pos.sell_pending = True

    # 4. Entries on coins that traded in this batch.
    committed = state.committed_position_count()
    free = state.free_cash_lamports()
    tx = state.tx_fee_lamports()
    seen = set()
    for mint in traded:
        if mint in seen:
            continue
        seen.add(mint)
        if len(orders) >= caps["max_orders_per_batch"]:
            break
        if committed >= MAX_POSITIONS:
            break
        if mint in positions or mint in buy_pending:
            continue
        coin = coins.get(mint)
        curve = state.curves.get(mint)
        if coin is None or curve is None:
            continue
        if free < STAKE_LAMPORTS + tx:
            break
        if state.max_buy_lamports(mint) < STAKE_LAMPORTS:
            continue
        if not _entry_ok(coin, curve, slot):
            continue
        if coin.attempts >= MAX_BUY_ATTEMPTS:
            continue
        cid = state.client_id()
        orders.append({"side": "buy", "client_id": cid, "mint": mint,
                       "quote_lamports": STAKE_LAMPORTS,
                       "max_slippage_bps": BUY_SLIPPAGE_BPS})
        open_orders[cid] = (mint, "buy")
        buy_pending.add(mint)
        coin.attempts += 1
        committed += 1
        free -= STAKE_LAMPORTS + tx

    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
