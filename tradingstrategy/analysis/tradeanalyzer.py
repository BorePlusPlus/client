"""Analyze spot trades: figure out how we won or lost trades.

A simplified trade analysis that only understands spots buys and sells, not margined trading or short positions.
Unlike Backtrader, this one is good for multiasset portfolio analysis.
"""
import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Iterable, Optional, Tuple, Callable

import numpy as np
import pandas as pd
from pandas.io.formats.style import Styler

from tradingstrategy.analysis.tradehint import TradeHint, TradeHintType
from tradingstrategy.exchange import ExchangeUniverse
from tradingstrategy.pair import PairUniverse, PandasPairUniverse
from tradingstrategy.types import PrimaryKey, USDollarAmount
from tradingstrategy.utils.format import format_value, format_percent, format_price, format_duration_days_hours_mins, \
    format_percent_2_decimals
from tradingstrategy.utils.summarydataframe import as_dollar, as_integer, create_summary_table, as_percent


@dataclass
class SpotTrade:
    """Track spot trades to construct position performance.

    For sells, quantity is negative.
    """

    #: Internal running counter to uniquely label all trades in trade analysis
    trade_id: PrimaryKey

    #: Trading pair for this trade
    pair_id: PrimaryKey

    #: When this trade was made, the backtes simulation thick
    timestamp: pd.Timestamp

    #: Asset price at buy in
    price: USDollarAmount

    #: How much we bought the asset. Negative value for sells.
    quantity: float

    #: How much fees we paid to the exchange
    commission: USDollarAmount

    #: How much we lost against the midprice due to the slippage
    slippage: USDollarAmount

    #: Any hints applied for this trade why it was performed
    hint: Optional[TradeHint] = None

    def is_buy(self):
        return self.quantity > 0

    def is_sell(self):
        return self.quantity < 0

    @property
    def value(self) -> USDollarAmount:
        return  abs(self.price * self.quantity)


@dataclass
class TradePosition:
    """How a particular asset traded.

    Each asset can have multiple entries (buys) and exits (sells)

    For a simple strategies there can be only one or two trades per position.

    * Enter (buy)

    * Exit (sell optionally)
    """

    #: List of all trades done for this position
    trades: List[SpotTrade] = field(default_factory=list)

    #: Closing the position could be deducted from the trades themselves,
    #: but we cache it by hand to speed up processing
    opened_at: Optional[pd.Timestamp] = None

    #: Closing the position could be deducted from the trades themselves,
    #: but we cache it by hand to speed up processing
    closed_at: Optional[pd.Timestamp] = None

    def __eq__(self, other: "TradePosition"):
        """Trade positions are unique by opening timestamp and pair id.]

        We assume there cannot be a position opened for the same asset at the same time twice.
        """
        return self.position_id == other.position_id

    def __hash__(self):
        """Allows easily create index (hash map) of all positions"""
        return hash((self.position_id))

    @property
    def position_id(self) -> PrimaryKey:
        """Position id is the same as the opening trade id."""
        return self.trades[0].trade_id

    @property
    def pair_id(self) -> PrimaryKey:
        """Position id is the same as the opening trade id."""
        return self.trades[0].pair_id

    @property
    def duration(self) -> Optional[datetime.timedelta]:
        """How long this position was held.

        :return: None if the position is still open
        """
        if not self.is_closed():
            return None
        return self.closed_at - self.opened_at

    def is_open(self):
        return self.closed_at is None

    def is_closed(self):
        return not self.is_open()

    @property
    def open_quantity(self) -> float:
        return sum([t.quantity for t in self.trades])

    @property
    def open_value(self) -> float:
        """The current value of this open position, with the price at the time of opening."""
        assert self.is_open()
        return sum([t.value for t in self.trades])

    @property
    def open_price(self) -> float:
        """At what price we opened this position.

        Supports only simple enter/exit positions.
        """
        buys = list(self.buys)
        assert len(buys) == 1
        return buys[0].price

    @property
    def close_price(self) -> float:
        """At what price we opened this position.

        Supports only simple enter/exit positions.
        """
        sells = list(self.sells)
        assert len(sells) == 1
        return sells[0].price

    @property
    def buys(self) -> Iterable[SpotTrade]:
        return [t for t in self.trades if t.is_buy()]

    @property
    def sells(self) -> Iterable[SpotTrade]:
        return [t for t in self.trades if t.is_sell()]

    @property
    def buy_value(self) -> USDollarAmount:
        return sum([t.value for t in self.trades if t.is_buy()])

    @property
    def sell_value(self) -> USDollarAmount:
        return sum([t.value for t in self.trades if t.is_sell()])

    @property
    def realised_profit(self) -> USDollarAmount:
        """Calculated life-time profit over this position."""
        assert not self.is_open()
        return -sum([t.quantity * t.price for t in self.trades])

    @property
    def realised_profit_percent(self) -> float:
        """Calculated life-time profit over this position."""
        assert not self.is_open()
        buy_value = self.buy_value
        sell_value = self.sell_value
        return sell_value / buy_value - 1

    def is_win(self):
        """Did we win this trade."""
        assert not self.is_open()
        return self.realised_profit > 0

    def is_lose(self):
        assert not self.is_open()
        return self.realised_profit < 0

    def is_stop_loss(self) -> bool:
        """Was stop loss triggered for this position"""
        for t in self.trades:
            if t.hint:
                if t.hint.type == TradeHintType.stop_loss_triggered:
                    return True
        return False

    def add_trade(self, t: SpotTrade):
        if self.trades:
            last_trade = self.trades[-1]
            assert t.timestamp > last_trade.timestamp, f"Tried to do trades in wrong order. Last: {last_trade}, got {t}"
        self.trades.append(t)

    def can_trade_close_position(self, t: SpotTrade):
        assert self.is_open()
        if not t.is_sell():
            return False
        open_quantity = self.open_quantity
        closing_quantity = -t.quantity
        assert closing_quantity <= open_quantity, "Cannot sell more than we have in balance sheet"
        return closing_quantity == open_quantity


@dataclass
class AssetTradeHistory:
    """How a particular asset traded.

    Each position can have increments or decrements.
    When position is decreased to zero, it is considered closed, and a new buy open a new position.
    """
    positions: List[TradePosition] = field(default_factory=list)

    def get_first_opened_at(self) -> Optional[pd.Timestamp]:
        if self.positions:
            return self.positions[0].opened_at
        return None

    def get_last_closed_at(self) -> Optional[pd.Timestamp]:
        for position in reversed(self.positions):
            if not position.is_open():
                return position.closed_at

        return None

    def add_trade(self, t: SpotTrade):
        """Adds a new trade to the asset history.

        If there is an open position the trade is added against this,
        otherwise a new position is opened for tracking.
        """
        current_position = None
        if self.positions:
            if self.positions[-1].is_open():
                current_position = self.positions[-1]

        if current_position:
            if current_position.can_trade_close_position(t):
                # Close the existing position
                current_position.closed_at = t.timestamp
                current_position.add_trade(t)
                assert current_position.open_quantity == 0
            else:
                # Add to the existing position
                current_position.add_trade(t)
        else:
            # Open new position
            new_position = TradePosition(opened_at=t.timestamp)
            new_position.add_trade(t)
            self.positions.append(new_position)


@dataclass
class TradeSummary:
    """Some generic statistics over all the trades"""
    won: int
    lost: int
    zero_loss: int
    stop_losses: int
    undecided: int
    realised_profit: USDollarAmount
    open_value: USDollarAmount
    uninvested_cash: USDollarAmount
    initial_cash: USDollarAmount

    def to_dataframe(self) -> pd.DataFrame:
        """Creates a human-readable Pandas dataframe table from the object."""
        total_trades = self.won + self.lost
        human_data = {
            "Return %": as_percent(self.realised_profit / self.initial_cash),
            "Cash at start": as_dollar(self.initial_cash),
            "Value at end": as_dollar(self.open_value + self.uninvested_cash),
            "Trade win percent": as_percent(self.won / total_trades),
            "Total trades done": as_integer(self.won + self.lost + self.zero_loss),
            "Won trades": as_integer(self.won),
            "Lost trades": as_integer(self.lost),
            "Stop losses triggered": as_integer(self.stop_losses),
            "Stop loss % of all": as_percent(self.stop_losses / total_trades),
            "Stop loss % of lost": as_percent(self.stop_losses / self.lost) if self.lost else "-",
            "Zero profit trades": as_integer(self.zero_loss),
            "Positions open at the end": as_integer(self.undecided),
            "Realised profit and loss": as_dollar(self.realised_profit),
            "Portfolio unrealised value": as_dollar(self.open_value),
            "Cash left at the end": as_dollar(self.uninvested_cash),
        }
        return create_summary_table(human_data)


@dataclass
class TradeAnalyzer:
    """Analysis of trades in a portfolio."""

    #: How a particular asset traded. Asset id -> Asset history mapping
    asset_histories: Dict[object, AssetTradeHistory] = field(default_factory=dict)

    def get_first_opened_at(self) -> Optional[pd.Timestamp]:
        def all_opens():
            for history in self.asset_histories.values():
                yield history.get_first_opened_at()

        return min(all_opens())

    def get_last_closed_at(self) -> Optional[pd.Timestamp]:
        def all_closes():
            for history in self.asset_histories.values():
                closed = history.get_last_closed_at()
                if closed:
                    yield closed
        return max(all_closes())

    def get_all_positions(self) -> Iterable[Tuple[PrimaryKey, TradePosition]]:
        """Return open and closed positions over all traded assets."""
        for pair_id, history in self.asset_histories.items():
            for position in history.positions:
                yield pair_id, position

    def get_open_positions(self) -> Iterable[Tuple[PrimaryKey, TradePosition]]:
        """Return open and closed positions over all traded assets."""
        for pair_id, history in self.asset_histories.items():
            for position in history.positions:
                if position.is_open():
                    yield pair_id, position

    def calculate_summary_statistics(self, initial_cash, uninvested_cash) -> TradeSummary:
        """Calculate some statistics how our trades went."""
        won = lost = zero_loss = stop_losses = undecided = 0
        open_value: USDollarAmount = 0
        profit: USDollarAmount = 0
        for pair_id, position in self.get_all_positions():
            if position.is_open():
                open_value += position.open_value
                undecided += 1
                continue

            if position.is_stop_loss():
                stop_losses += 1

            if position.is_win():
                won += 1
            elif position.is_lose():
                lost += 1
            else:
                # Any profit exactly balances out loss in slippage and commission
                zero_loss += 1

            profit += position.realised_profit

        return TradeSummary(
            won=won,
            lost=lost,
            zero_loss=zero_loss,
            stop_losses=stop_losses,
            undecided=undecided,
            realised_profit=profit,
            open_value=open_value,
            uninvested_cash=uninvested_cash,
            initial_cash=initial_cash,
        )

    def create_timeline(self) -> pd.DataFrame:
        """Create a timeline feed how we traded over a course of time.

        Note: We assume each position has only one enter and exit event, not position increases over the lifetime.

        :return: DataFrame with timestamp and timeline_event columns
        """

        def gen_events():
            for pair_id, position in self.get_all_positions():
                yield (position.position_id, position)

        df = pd.DataFrame(gen_events(), columns=["position_id", "position"])
        return df


def expand_timeline(
        exchange_universe: ExchangeUniverse,
        pair_universe: PandasPairUniverse,
        timeline: pd.DataFrame,
        vmin=-0.3,
        vmax=0.2,
        hidden_columns = ["Id", "PnL % raw"]) -> Tuple[pd.DataFrame, Callable]:
    """Expand trade history timeline to human readable table.

    This will the outputting much easier in Python Notebooks.

    Currently does not incrementing/decreasing positions gradually.

    Instaqd of applying styles or returning a styled dataframe, we return a callable that applies the styles.
    This is because of Pandas issue https://github.com/pandas-dev/pandas/issues/40675 - hidden indexes, columns,
    etc. are not exported.

    :param vmax: Trade success % to have the extreme green color.

    :param vmin: The % of lost capital on the trade to have the extreme red color. Default -0.3

    :param hidden_columns: Hide columns in the output table

    :return: DataFrame with human readable position win/loss information, having DF indexed by timestamps and a styler function
    """

    # https://stackoverflow.com/a/52363890/315168
    def expander(row):
        position: TradePosition = row["position"]
        # timestamp = row.name  # ???
        pair_id = position.pair_id
        pair_info = pair_universe.get_pair_by_id(pair_id)
        exchange = exchange_universe.get_by_id(pair_info.exchange_id)

        remarks = "SL" if position.is_stop_loss() else ""

        r = {
            # "timestamp": timestamp,
            "Id": position.position_id,
            "Remarks": remarks,
            "Opened at": position.opened_at,
            "Duration": format_duration_days_hours_mins(position.duration) if position.duration else np.nan,
            "Exchange": exchange.name,
            "Base asset": pair_info.base_token_symbol,
            "Quote asset": pair_info.quote_token_symbol,
            "PnL USD": format_value(position.realised_profit) if position.is_closed() else np.nan,
            "PnL %": format_percent_2_decimals(position.realised_profit_percent) if position.is_closed() else np.nan,
            "PnL % raw": position.realised_profit_percent if position.is_closed() else 0,
            "Open price USD": format_price(position.open_price),
            "Close price USD": format_price(position.close_price) if position.is_closed() else np.nan,
        }
        return r

    applied_df = timeline.apply(expander, axis='columns', result_type='expand')

    # https://stackoverflow.com/a/52720936/315168
    applied_df\
        .sort_values(by=['Id'], ascending=[True], inplace=True)

    # Get rid of NaN labels
    # https://stackoverflow.com/a/28390992/315168
    applied_df.fillna('', inplace=True)

    def apply_styles(df: pd.DataFrame):
        # Create a Pandas Styler with multiple styling options applied
        # Dynamically color the background of trade outcome coluns # https://pandas.pydata.org/docs/reference/api/pandas.io.formats.style.Styler.background_gradient.html
        return df.style\
            .hide_index()\
            .hide_columns(hidden_columns)\
            .background_gradient(
                axis=0,
                gmap=applied_df['PnL % raw'],
                cmap='RdYlGn',
                vmin=vmin,  # We can only lose 100% of our money on position
                vmax=vmax)  # 50% profit is 21.5 position. Assume this is the max success color we can hit over

    return applied_df, apply_styles
