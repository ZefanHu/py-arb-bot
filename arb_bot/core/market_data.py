# arb_bot/core/market_data.py
import json
import logging
from collections import OrderedDict # 用于维护有序的订单簿层级

class MarketDataHandler:
    """
    处理和管理来自交易所的实时市场数据，主要是订单簿和最新成交。
    此类设计为可处理在初始化时指定的任何交易对集合。
    """
    def __init__(self, trading_pairs, logger=None):
        """
        初始化 MarketDataHandler。

        Args:
            trading_pairs (list): 需要处理市场数据的交易对列表 (例如 ["ETH-USDT", "USDC-USDT", "ETH-USDC"])。
                                  这个列表通常从 settings.yaml 配置文件中读取并传入。
            logger (logging.Logger, optional): 日志记录器实例。如果为 None，则使用默认的 print。
        """
        self.trading_pairs = trading_pairs
        if logger:
            self.logger = logger
        else:
            self.logger = logging.getLogger(__name__) # 创建一个基本的logger
            if not self.logger.handlers: # 防止重复添加handler
                logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        # 初始化数据结构
        # self.order_books: 存储每个交易对的订单簿
        #   - bids: OrderedDict, 价格(str) -> 数量(str)，按价格降序排列
        #   - asks: OrderedDict, 价格(str) -> 数量(str)，按价格升序排列
        #   - ts: 时间戳 (str)
        #   - checksum: 校验和 (int,可选)
        self.order_books = {
            pair: {
                "bids": OrderedDict(),
                "asks": OrderedDict(),
                "ts": None,
                "checksum": None
            } for pair in self.trading_pairs
        }
        # self.last_trades: 存储每个交易对的最新成交信息
        self.last_trades = {pair: None for pair in self.trading_pairs}
        # self.best_prices: 存储每个交易对的最佳买卖价
        self.best_prices = {
            pair: {
                "best_bid_price": None, "best_bid_qty": None,
                "best_ask_price": None, "best_ask_qty": None,
                "timestamp": None
            } for pair in self.trading_pairs
        }
        self.logger.info(f"MarketDataHandler initialized for dynamically configured pairs: {', '.join(trading_pairs)}")

    def process_websocket_message(self, message_data):
        """
        处理从 WebSocket 接收到的原始消息。
        根据消息内容分发到相应的处理函数。
        只处理在 self.trading_pairs 中定义的交易对的消息。
        """
        try:
            if 'arg' not in message_data or 'channel' not in message_data['arg']:
                if message_data.get('event') == 'error':
                    self.logger.error(f"WebSocket API Error: {message_data.get('msg')} (Code: {message_data.get('code')})")
                elif message_data.get('event') == 'subscribe' or message_data.get('event') == 'login':
                    self.logger.info(f"Subscription/Login Event: {message_data}")
                else:
                    # self.logger.warning(f"Received message without 'arg' or 'channel': {message_data}") # 可能过于频繁
                    pass
                return

            channel = message_data['arg']['channel']
            inst_id = message_data['arg'].get('instId') # 交易对ID, 例如 "ETH-USDT"

            # 核心动态逻辑：只处理在初始化时配置的交易对
            if not inst_id or inst_id not in self.trading_pairs:
                # self.logger.debug(f"Received message for non-monitored instId '{inst_id}' or missing instId. Channel: {channel}")
                return

            if 'data' not in message_data or not message_data['data']:
                # self.logger.debug(f"No data in message for {inst_id} on channel {channel}: {message_data}")
                return

            data_list = message_data['data']

            if channel == 'books': # 订单簿频道
                action = message_data.get('action') # 'snapshot' 或 'update'
                if action == 'snapshot':
                    self._process_order_book_snapshot(inst_id, data_list[0])
                elif action == 'update':
                    self._process_order_book_update(inst_id, data_list[0])
                else:
                    self.logger.warning(f"Unknown action '{action}' for books channel on {inst_id}: {message_data}")

            elif channel == 'trades': # 最新成交频道
                self._process_trades_update(inst_id, data_list)

            elif channel == 'tickers': # 行情频道 (可选)
                self._process_tickers_update(inst_id, data_list[0])
            # 可以根据需要添加对其他频道的处理

        except Exception as e:
            self.logger.error(f"Error processing WebSocket message for {inst_id if 'inst_id' in locals() else 'unknown instId'}: {e}\nMessage: {message_data}", exc_info=True)

    def _process_order_book_snapshot(self, inst_id, snapshot_data):
        """处理订单簿的首次全量快照 (snapshot)。"""
        if inst_id not in self.order_books: # 双重检查，理论上 process_websocket_message 已过滤
            self.logger.warning(f"Received snapshot for untracked instId: {inst_id}")
            return

        self.order_books[inst_id]['bids'].clear()
        self.order_books[inst_id]['asks'].clear()

        for price, qty, _, _ in snapshot_data.get('bids', []):
            if float(qty) > 0:
                self.order_books[inst_id]['bids'][price] = qty
        for price, qty, _, _ in snapshot_data.get('asks', []):
            if float(qty) > 0:
                self.order_books[inst_id]['asks'][price] = qty

        self.order_books[inst_id]['ts'] = snapshot_data.get('ts')
        self.order_books[inst_id]['checksum'] = snapshot_data.get('checksum')
        self._update_best_prices(inst_id)
        self.logger.info(f"Order book snapshot for {inst_id} processed. "
                         f"Bids: {len(self.order_books[inst_id]['bids'])}, "
                         f"Asks: {len(self.order_books[inst_id]['asks'])}. "
                         f"Best Bid: {self.best_prices[inst_id]['best_bid_price']}, "
                         f"Best Ask: {self.best_prices[inst_id]['best_ask_price']}")

    def _process_order_book_update(self, inst_id, update_data):
        """处理订单簿的增量更新 (update)。"""
        if inst_id not in self.order_books:
            return

        book_side_updated = False
        for price, qty, _, _ in update_data.get('bids', []):
            if float(qty) == 0:
                if price in self.order_books[inst_id]['bids']:
                    del self.order_books[inst_id]['bids'][price]
                    book_side_updated = True
            else:
                self.order_books[inst_id]['bids'][price] = qty
                book_side_updated = True
        for price, qty, _, _ in update_data.get('asks', []):
            if float(qty) == 0:
                if price in self.order_books[inst_id]['asks']:
                    del self.order_books[inst_id]['asks'][price]
                    book_side_updated = True
            else:
                self.order_books[inst_id]['asks'][price] = qty
                book_side_updated = True

        if book_side_updated:
            # 确保 bids 按价格降序，asks 按价格升序
            sorted_bids = OrderedDict(sorted(self.order_books[inst_id]['bids'].items(), key=lambda item: float(item[0]), reverse=True))
            self.order_books[inst_id]['bids'] = sorted_bids
            sorted_asks = OrderedDict(sorted(self.order_books[inst_id]['asks'].items(), key=lambda item: float(item[0])))
            self.order_books[inst_id]['asks'] = sorted_asks

        self.order_books[inst_id]['ts'] = update_data.get('ts')
        new_checksum = update_data.get('checksum')
        if new_checksum:
             self.order_books[inst_id]['checksum'] = new_checksum
             # TODO: 实现 checksum 校验逻辑 (参考 OKX 文档)

        self._update_best_prices(inst_id)

    def _process_trades_update(self, inst_id, trades_data_list):
        """处理最新成交数据。"""
        if inst_id not in self.last_trades:
            return
        for trade_data in trades_data_list:
            self.last_trades[inst_id] = trade_data
            # self.logger.info(f"Trade update for {inst_id}: Side={trade_data.get('side')}, Price={trade_data.get('px')}, Qty={trade_data.get('sz')}") # 可能过于频繁

    def _process_tickers_update(self, inst_id, ticker_data):
        """处理行情 (tickers) 数据。"""
        # self.logger.info(f"Ticker update for {inst_id}: LastPx={ticker_data.get('last')}, BidPx={ticker_data.get('bidPx')}, AskPx={ticker_data.get('askPx')}") # 可能过于频繁
        # 主要依赖订单簿获取最佳买卖价，ticker数据可作补充或校验
        pass

    def _update_best_prices(self, inst_id):
        """根据当前订单簿更新并记录最佳买卖价格和数量。"""
        if inst_id not in self.order_books:
            return

        current_book = self.order_books[inst_id]
        old_best_bid = self.best_prices[inst_id]['best_bid_price']
        old_best_ask = self.best_prices[inst_id]['best_ask_price']

        new_best_bid_price, new_best_bid_qty = None, None
        if current_book['bids']:
            new_best_bid_price = next(iter(current_book['bids']))
            new_best_bid_qty = current_book['bids'][new_best_bid_price]

        new_best_ask_price, new_best_ask_qty = None, None
        if current_book['asks']:
            new_best_ask_price = next(iter(current_book['asks']))
            new_best_ask_qty = current_book['asks'][new_best_ask_price]

        timestamp = current_book.get('ts')

        if (new_best_bid_price != old_best_bid or new_best_ask_price != old_best_ask):
            self.best_prices[inst_id]['best_bid_price'] = new_best_bid_price
            self.best_prices[inst_id]['best_bid_qty'] = new_best_bid_qty
            self.best_prices[inst_id]['best_ask_price'] = new_best_ask_price
            self.best_prices[inst_id]['best_ask_qty'] = new_best_ask_qty
            self.best_prices[inst_id]['timestamp'] = timestamp

            self.logger.info(
                f"Best prices updated for {inst_id}: "
                f"Bid: {new_best_bid_price} (Qty: {new_best_bid_qty}), "
                f"Ask: {new_best_ask_price} (Qty: {new_best_ask_qty}), "
                f"TS: {timestamp}"
            )

    def get_best_bid(self, inst_id):
        return self.best_prices[inst_id].get('best_bid_price')

    def get_best_ask(self, inst_id):
        return self.best_prices[inst_id].get('best_ask_price')

    def get_best_bid_with_qty(self, inst_id):
        bp = self.best_prices[inst_id]
        return bp.get('best_bid_price'), bp.get('best_bid_qty')

    def get_best_ask_with_qty(self, inst_id):
        bp = self.best_prices[inst_id]
        return bp.get('best_ask_price'), bp.get('best_ask_qty')

    def get_order_book(self, inst_id):
        return self.order_books.get(inst_id)

    def get_last_trade_price(self, inst_id):
        trade = self.last_trades.get(inst_id)
        return trade.get('px') if trade else None

    def display_order_book(self, inst_id, depth=5):
        """清晰地展示指定交易对的订单簿深度。"""
        if inst_id not in self.order_books:
            self.logger.info(f"No order book data for {inst_id} to display.")
            return

        book = self.order_books[inst_id]
        ts = self.best_prices[inst_id].get('timestamp', book.get('ts', 'N/A'))
        display_str = f"\n--- Order Book: {inst_id} (TS: {ts}) ---\n"
        display_str += "Bids (Price | Quantity)          Asks (Price | Quantity)\n"
        display_str += "-------------------------          -------------------------\n"
        bids_items = list(book['bids'].items())
        asks_items = list(book['asks'].items())
        for i in range(depth):
            bid_info = f"{bids_items[i][0]:>10} | {bids_items[i][1]:>10}" if i < len(bids_items) else " " * 25
            ask_info = f"{asks_items[i][0]:>10} | {asks_items[i][1]:>10}" if i < len(asks_items) else ""
            display_str += f"{bid_info}          {ask_info}\n"
        best_bid, best_bid_qty = self.get_best_bid_with_qty(inst_id)
        best_ask, best_ask_qty = self.get_best_ask_with_qty(inst_id)
        spread = None
        if best_bid and best_ask:
            try: # 确保价格可以转换为浮点数进行计算
                spread = float(best_ask) - float(best_bid)
            except ValueError:
                spread = "Error" # 如果价格字符串无法转换
        display_str += "-------------------------------------------------------------\n"
        display_str += f"Best Bid: {best_bid or 'N/A'} (Qty: {best_bid_qty or 'N/A'})\n"
        display_str += f"Best Ask: {best_ask or 'N/A'} (Qty: {best_ask_qty or 'N/A'})\n"
        display_str += f"Spread: {spread if spread is not None else 'N/A'}\n"
        display_str += "--- End of Order Book ---\n"
        self.logger.info(display_str)


if __name__ == '__main__':
    # --- 用于独立测试 MarketDataHandler 的示例代码 ---
    # 注意：此处的交易对和消息仅为示例，实际机器人运行时会根据 settings.yaml 配置动态处理。
    example_pair_1 = "EXAMPLE-COIN-USDT" # 示例交易对1
    example_pair_2 = "ANOTHER-COIN-USDT" # 示例交易对2
    test_pairs_for_example = [example_pair_1, example_pair_2]

    logger = logging.getLogger("MarketDataTestStandalone")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(ch)

    # MarketDataHandler 实例会基于传入的 test_pairs_for_example 进行初始化
    market_handler = MarketDataHandler(trading_pairs=test_pairs_for_example, logger=logger)
    logger.info(f"Standalone test: MarketDataHandler initialized for example pairs: {test_pairs_for_example}")

    # 1. 模拟订单簿快照 (Snapshot) for example_pair_1
    #    这里的价格和数量是任意的，仅用于演示数据结构和处理流程。
    snapshot_msg_example_pair = {
        "arg": {"channel": "books", "instId": example_pair_1},
        "action": "snapshot",
        "data": [{
            "asks": [
                ["30001.0", "0.5", "0", "1"],
                ["30001.5", "1.2", "0", "3"]
            ],
            "bids": [
                ["30000.0", "1.0", "0", "2"],
                ["29999.5", "0.8", "0", "1"]
            ],
            "ts": "1626771920123", # 示例时间戳
            "checksum": 12345678 # 示例校验和
        }]
    }
    logger.info(f"\n--- Processing Snapshot for {example_pair_1} (Example Data) ---")
    market_handler.process_websocket_message(snapshot_msg_example_pair)
    market_handler.display_order_book(example_pair_1, depth=2)

    # 2. 模拟订单簿更新 (Update) for example_pair_1
    update_msg_example_pair = {
        "arg": {"channel": "books", "instId": example_pair_1},
        "action": "update",
        "data": [{
            "asks": [
                ["30001.2", "0.3", "0", "1"],
                ["30001.5", "0", "0", "0"]    # 数量为0，移除此档
            ],
            "bids": [
                ["30000.0", "1.2", "0", "3"]
            ],
            "ts": "1626771920500",
            "checksum": 12345690
        }]
    }
    logger.info(f"\n--- Processing Update for {example_pair_1} (Example Data) ---")
    market_handler.process_websocket_message(update_msg_example_pair)
    market_handler.display_order_book(example_pair_1, depth=3)

    # 3. 模拟最新成交 (Trades) for example_pair_1
    trades_msg_example_pair = {
        "arg": {"channel": "trades", "instId": example_pair_1},
        "data": [
            {"instId": example_pair_1, "tradeId": "1001", "px": "30000.5", "sz": "0.1", "side": "buy", "ts": "1626771921000"},
            {"instId": example_pair_1, "tradeId": "1002", "px": "30000.8", "sz": "0.05", "side": "sell", "ts": "1626771921500"}
        ]
    }
    logger.info(f"\n--- Processing Trades for {example_pair_1} (Example Data) ---")
    market_handler.process_websocket_message(trades_msg_example_pair)
    logger.info(f"Last trade price for {example_pair_1}: {market_handler.get_last_trade_price(example_pair_1)}")

    logger.info("\n--- Standalone MarketDataHandler Test Complete ---")
    logger.info(f"To run the actual bot, please use okx_conn.py or your main entry script, which will load pairs from settings.yaml.")


