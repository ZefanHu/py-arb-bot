# arb_bot/core/strategy.py
import logging
from decimal import Decimal, ROUND_DOWN # 确保 ROUND_UP 也导入如果需要，但当前计算主要用 ROUND_DOWN
import json # 用于在测试中打印机会

class ArbitrageStrategy:
    def __init__(self, market_data_handler, config, logger=None):
        self.market_data_handler = market_data_handler
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

        arbitrage_config = self.config.get('arbitrage', {})
        self.fee_rate = Decimal(str(arbitrage_config.get('fee_rate', '0.001')))
        self.min_profit_threshold = Decimal(str(arbitrage_config.get('min_profit_threshold', '0.002')))
        self.initial_usdt_amount = Decimal(str(arbitrage_config.get('trade_amount_usdt', '100.0')))

        # 从配置中动态获取核心币种和交易对
        self.core_currencies = arbitrage_config.get('core_currencies', [])
        self.trading_pairs_config = arbitrage_config.get('trading_pairs', [])

        if len(self.core_currencies) != 3:
            self.logger.critical(
                f"Configuration error: 'core_currencies' must contain exactly 3 currencies. Found: {self.core_currencies}"
            )
            raise ValueError("core_currencies must contain 3 currencies for triangular arbitrage.")

        # 假设 core_currencies 的顺序是 [QUOTE_STABLE, PRIMARY_ARBITRAGE_ASSET, SECONDARY_STABLE]
        # 例如: ["USDT", "ETH", "USDC"]
        self.quote_stable_asset = self.core_currencies[0] # 例如 "USDT"
        self.primary_arb_asset = self.core_currencies[1]  # 例如 "ETH"
        self.secondary_stable_asset = self.core_currencies[2] # 例如 "USDC"

        # 动态查找交易对
        self.pair_primary_quote = self._find_pair(self.primary_arb_asset, self.quote_stable_asset)
        self.pair_primary_secondary = self._find_pair(self.primary_arb_asset, self.secondary_stable_asset)
        self.pair_secondary_quote = self._find_pair(self.secondary_stable_asset, self.quote_stable_asset)

        if not all([self.pair_primary_quote, self.pair_primary_secondary, self.pair_secondary_quote]):
            self.logger.critical(
                f"Could not find all required trading pairs for core currencies: {self.core_currencies} "
                f"among configured trading_pairs: {self.trading_pairs_config}. "
                f"Found: PQ={self.pair_primary_quote}, PS={self.pair_primary_secondary}, SQ={self.pair_secondary_quote}"
            )
            raise ValueError("One or more required trading pairs not found in configuration.")

        # 动态定义套利路径中涉及的交易对和币种信息
        # 路径1: QUOTE_STABLE -> SECONDARY_STABLE -> PRIMARY_ARBITRAGE_ASSET -> QUOTE_STABLE
        # 例如: USDT -> USDC -> ETH -> USDT
        self.path1_details = {
            "step1": {"pair": self.pair_secondary_quote,   "base_ccy": self.secondary_stable_asset, "quote_ccy": self.quote_stable_asset, "side": "buy"},
            "step2": {"pair": self.pair_primary_secondary, "base_ccy": self.primary_arb_asset,      "quote_ccy": self.secondary_stable_asset, "side": "buy"},
            "step3": {"pair": self.pair_primary_quote,     "base_ccy": self.primary_arb_asset,      "quote_ccy": self.quote_stable_asset, "side": "sell"}
        }
        # 路径2: QUOTE_STABLE -> PRIMARY_ARBITRAGE_ASSET -> SECONDARY_STABLE -> QUOTE_STABLE
        # 例如: USDT -> ETH -> USDC -> USDT
        self.path2_details = {
            "step1": {"pair": self.pair_primary_quote,     "base_ccy": self.primary_arb_asset,      "quote_ccy": self.quote_stable_asset, "side": "buy"},
            "step2": {"pair": self.pair_primary_secondary, "base_ccy": self.primary_arb_asset,      "quote_ccy": self.secondary_stable_asset, "side": "sell"},
            "step3": {"pair": self.pair_secondary_quote,   "base_ccy": self.secondary_stable_asset, "quote_ccy": self.quote_stable_asset, "side": "sell"}
        }

        self.logger.info(f"ArbitrageStrategy initialized for {self.primary_arb_asset} arbitrage.")
        self.logger.info(f"  Quote Stable: {self.quote_stable_asset}, Primary Arb: {self.primary_arb_asset}, Secondary Stable: {self.secondary_stable_asset}")
        self.logger.info(f"  Pair {self.primary_arb_asset}/{self.quote_stable_asset}: {self.pair_primary_quote}")
        self.logger.info(f"  Pair {self.primary_arb_asset}/{self.secondary_stable_asset}: {self.pair_primary_secondary}")
        self.logger.info(f"  Pair {self.secondary_stable_asset}/{self.quote_stable_asset}: {self.pair_secondary_quote}")
        self.logger.info(f"  Fee rate: {self.fee_rate}, Min profit threshold: {self.min_profit_threshold}, Initial {self.quote_stable_asset} amount: {self.initial_usdt_amount}")

    def _find_pair(self, ccy1, ccy2):
        """辅助函数，根据两个币种名称在配置的交易对列表中查找对应的交易对名称。"""
        # 确保币种名称是标准格式，例如大写
        ccy1_upper = ccy1.upper()
        ccy2_upper = ccy2.upper()
        for pair_name in self.trading_pairs_config:
            parts = pair_name.upper().split('-')
            if (parts[0] == ccy1_upper and parts[1] == ccy2_upper) or \
               (parts[0] == ccy2_upper and parts[1] == ccy1_upper):
                return pair_name # 返回配置文件中原始大小写的交易对名称
        return None

    def check_opportunities(self):
        self.logger.debug(f"Checking for {self.primary_arb_asset} arbitrage opportunities...")
        opportunities = []

        # --- 检查路径1: QUOTE_STABLE -> SECONDARY_STABLE -> PRIMARY_ARBITRAGE_ASSET -> QUOTE_STABLE ---
        # 例如: USDT -> USDC -> ETH -> USDT
        # Step 1 (Buy Secondary Stable with Quote Stable): Best Ask for SecondaryStable/QuoteStable pair
        ask1_sq_str, _ = self.market_data_handler.get_best_ask_with_qty(self.path1_details["step1"]["pair"])
        # Step 2 (Buy Primary Arb with Secondary Stable): Best Ask for PrimaryArb/SecondaryStable pair
        ask2_ps_str, _ = self.market_data_handler.get_best_ask_with_qty(self.path1_details["step2"]["pair"])
        # Step 3 (Sell Primary Arb for Quote Stable): Best Bid for PrimaryArb/QuoteStable pair
        bid3_pq_str, _ = self.market_data_handler.get_best_bid_with_qty(self.path1_details["step3"]["pair"])

        if all([ask1_sq_str, ask2_ps_str, bid3_pq_str]):
            try:
                p_sq_ask = Decimal(ask1_sq_str) # Price to buy Secondary Stable (e.g., USDC price in USDT)
                p_ps_ask = Decimal(ask2_ps_str) # Price to buy Primary Arb (e.g., ETH price in USDC)
                p_pq_bid = Decimal(bid3_pq_str) # Price to sell Primary Arb (e.g., ETH price in USDT)

                if p_sq_ask == Decimal(0) or p_ps_ask == Decimal(0):
                    self.logger.warning(f"Path 1 ({self.quote_stable_asset}->{self.secondary_stable_asset}->{self.primary_arb_asset}->{self.quote_stable_asset}): Zero price encountered in asks.")
                else:
                    # 1 QUOTE_STABLE buys (1/p_sq_ask) SECONDARY_STABLE
                    # (1/p_sq_ask) SECONDARY_STABLE buys (1/p_sq_ask) * (1/p_ps_ask) PRIMARY_ARB_ASSET
                    # This amount of PRIMARY_ARB_ASSET sells for (1/p_sq_ask) * (1/p_ps_ask) * p_pq_bid QUOTE_STABLE
                    profit_ratio_p1 = (Decimal('1') / p_sq_ask) * (Decimal('1') / p_ps_ask) * p_pq_bid
                    profit_ratio_p1_after_fees = profit_ratio_p1 * ((Decimal('1') - self.fee_rate)**3)

                    if profit_ratio_p1_after_fees > (Decimal('1') + self.min_profit_threshold):
                        path_id_p1 = f"P1_{self.quote_stable_asset}_{self.secondary_stable_asset}_{self.primary_arb_asset}_{self.quote_stable_asset}"
                        self.logger.info(f"!!! Arbitrage Opportunity Found ({path_id_p1}) !!!")
                        self.logger.info(f"    Prices: {self.path1_details['step1']['pair']}(A):{p_sq_ask}, {self.path1_details['step2']['pair']}(A):{p_ps_ask}, {self.path1_details['step3']['pair']}(B):{p_pq_bid}")
                        self.logger.info(f"    Expected profit ratio (after fees): {profit_ratio_p1_after_fees:.6f}")
                        trades = self._calculate_trades_for_path(self.path1_details, p_sq_ask, p_ps_ask, p_pq_bid)
                        if trades:
                            opportunities.append({"path_id": path_id_p1, "trades": trades, "profit_ratio": profit_ratio_p1_after_fees})
            except Exception as e:
                self.logger.error(f"Error calculating Path 1 opportunity: {e}", exc_info=True)
        else:
            self.logger.debug(f"Path 1 ({self.quote_stable_asset}->{self.secondary_stable_asset}->{self.primary_arb_asset}->{self.quote_stable_asset}): Missing market data for one or more pairs.")


        # --- 检查路径2: QUOTE_STABLE -> PRIMARY_ARBITRAGE_ASSET -> SECONDARY_STABLE -> QUOTE_STABLE ---
        # 例如: USDT -> ETH -> USDC -> USDT
        # Step 1 (Buy Primary Arb with Quote Stable): Best Ask for PrimaryArb/QuoteStable pair
        ask1_pq_str, _ = self.market_data_handler.get_best_ask_with_qty(self.path2_details["step1"]["pair"])
        # Step 2 (Sell Primary Arb for Secondary Stable): Best Bid for PrimaryArb/SecondaryStable pair
        bid2_ps_str, _ = self.market_data_handler.get_best_bid_with_qty(self.path2_details["step2"]["pair"])
        # Step 3 (Sell Secondary Stable for Quote Stable): Best Bid for SecondaryStable/QuoteStable pair
        bid3_sq_str, _ = self.market_data_handler.get_best_bid_with_qty(self.path2_details["step3"]["pair"])

        if all([ask1_pq_str, bid2_ps_str, bid3_sq_str]):
            try:
                p_pq_ask = Decimal(ask1_pq_str) # Price to buy Primary Arb (e.g., ETH price in USDT)
                p_ps_bid = Decimal(bid2_ps_str) # Price to sell Primary Arb (e.g., ETH price in USDC)
                p_sq_bid = Decimal(bid3_sq_str) # Price to sell Secondary Stable (e.g., USDC price in USDT)

                if p_pq_ask == Decimal(0):
                    self.logger.warning(f"Path 2 ({self.quote_stable_asset}->{self.primary_arb_asset}->{self.secondary_stable_asset}->{self.quote_stable_asset}): Zero price encountered in asks.")
                else:
                    # 1 QUOTE_STABLE buys (1/p_pq_ask) PRIMARY_ARB_ASSET
                    # (1/p_pq_ask) PRIMARY_ARB_ASSET sells for (1/p_pq_ask) * p_ps_bid SECONDARY_STABLE
                    # This amount of SECONDARY_STABLE sells for (1/p_pq_ask) * p_ps_bid * p_sq_bid QUOTE_STABLE
                    profit_ratio_p2 = (Decimal('1') / p_pq_ask) * p_ps_bid * p_sq_bid
                    profit_ratio_p2_after_fees = profit_ratio_p2 * ((Decimal('1') - self.fee_rate)**3)

                    if profit_ratio_p2_after_fees > (Decimal('1') + self.min_profit_threshold):
                        path_id_p2 = f"P2_{self.quote_stable_asset}_{self.primary_arb_asset}_{self.secondary_stable_asset}_{self.quote_stable_asset}"
                        self.logger.info(f"!!! Arbitrage Opportunity Found ({path_id_p2}) !!!")
                        self.logger.info(f"    Prices: {self.path2_details['step1']['pair']}(A):{p_pq_ask}, {self.path2_details['step2']['pair']}(B):{p_ps_bid}, {self.path2_details['step3']['pair']}(B):{p_sq_bid}")
                        self.logger.info(f"    Expected profit ratio (after fees): {profit_ratio_p2_after_fees:.6f}")
                        trades = self._calculate_trades_for_path(self.path2_details, p_pq_ask, p_ps_bid, p_sq_bid)
                        if trades:
                            opportunities.append({"path_id": path_id_p2, "trades": trades, "profit_ratio": profit_ratio_p2_after_fees})
            except Exception as e:
                self.logger.error(f"Error calculating Path 2 opportunity: {e}", exc_info=True)
        else:
             self.logger.debug(f"Path 2 ({self.quote_stable_asset}->{self.primary_arb_asset}->{self.secondary_stable_asset}->{self.quote_stable_asset}): Missing market data for one or more pairs.")

        return opportunities

    def _calculate_trades_for_path(self, path_details, p1, p2, p3):
        """
        通用函数，为给定路径计算交易序列。
        p1, p2, p3 是对应步骤的执行价格。
        path_details 结构:
        {
            "step1": {"pair": "PAIR_NAME", "base_ccy": "BASE1", "quote_ccy": "QUOTE1", "side": "buy/sell"},
            "step2": {"pair": "PAIR_NAME", "base_ccy": "BASE2", "quote_ccy": "QUOTE2", "side": "buy/sell"},
            "step3": {"pair": "PAIR_NAME", "base_ccy": "BASE3", "quote_ccy": "QUOTE3", "side": "buy/sell"}
        }
        """
        trades = []
        current_amount = self.initial_usdt_amount # 初始总是以 quote_stable_asset (如USDT) 开始
        current_asset = self.quote_stable_asset

        # --- Leg 1 ---
        leg1 = path_details["step1"]
        price_leg1 = p1
        qty_leg1_base = Decimal(0)

        if price_leg1 <= Decimal(0):
            self.logger.error(f"Path Calc Error: Price for leg 1 ({leg1['pair']}) is zero or negative.")
            return None

        if leg1["side"] == "buy": # 买入 base_ccy (leg1["base_ccy"])，花费 quote_ccy (current_asset)
            if current_asset != leg1["quote_ccy"]:
                self.logger.error(f"Asset mismatch for leg 1 buy: Expected to spend {leg1['quote_ccy']}, have {current_asset}")
                return None
            qty_leg1_base = (current_amount / price_leg1).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
            amount_spent_quote = qty_leg1_base * price_leg1 # 实际花费的 quote
            trades.append({
                "instId": leg1["pair"], "side": "buy", "ordType": "limit",
                "px": str(price_leg1), "sz": str(qty_leg1_base),
                "baseCcy": leg1["base_ccy"], "quoteCcy": leg1["quote_ccy"],
                "descr": f"Leg 1: Buy {qty_leg1_base:.8f} {leg1['base_ccy']} with ~{amount_spent_quote:.8f} {leg1['quote_ccy']}"
            })
            current_amount = qty_leg1_base * (Decimal('1') - self.fee_rate) # 获得的 base_ccy 扣除手续费
            current_asset = leg1["base_ccy"]
        else: # leg1["side"] == "sell" 卖出 base_ccy (current_asset)，获得 quote_ccy
            if current_asset != leg1["base_ccy"]:
                self.logger.error(f"Asset mismatch for leg 1 sell: Expected to sell {leg1['base_ccy']}, have {current_asset}")
                return None
            qty_leg1_base = current_amount.quantize(Decimal('1e-8'), rounding=ROUND_DOWN) # current_amount 是 base_ccy 的数量
            amount_received_quote = (qty_leg1_base * price_leg1)
            trades.append({
                "instId": leg1["pair"], "side": "sell", "ordType": "limit",
                "px": str(price_leg1), "sz": str(qty_leg1_base),
                "baseCcy": leg1["base_ccy"], "quoteCcy": leg1["quote_ccy"],
                "descr": f"Leg 1: Sell {qty_leg1_base:.8f} {leg1['base_ccy']} for ~{amount_received_quote:.8f} {leg1['quote_ccy']}"
            })
            current_amount = amount_received_quote * (Decimal('1') - self.fee_rate) # 获得的 quote_ccy 扣除手续费
            current_asset = leg1["quote_ccy"]

        if current_amount <= Decimal(0): self.logger.warning("Path Calc: Amount became zero after leg 1."); return None

        # --- Leg 2 ---
        leg2 = path_details["step2"]
        price_leg2 = p2
        qty_leg2_base = Decimal(0)

        if price_leg2 <= Decimal(0):
            self.logger.error(f"Path Calc Error: Price for leg 2 ({leg2['pair']}) is zero or negative.")
            return None

        if leg2["side"] == "buy":
            if current_asset != leg2["quote_ccy"]:
                self.logger.error(f"Asset mismatch for leg 2 buy: Expected to spend {leg2['quote_ccy']}, have {current_asset}")
                return None
            qty_leg2_base = (current_amount / price_leg2).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
            amount_spent_quote_l2 = qty_leg2_base * price_leg2
            trades.append({
                "instId": leg2["pair"], "side": "buy", "ordType": "limit",
                "px": str(price_leg2), "sz": str(qty_leg2_base),
                "baseCcy": leg2["base_ccy"], "quoteCcy": leg2["quote_ccy"],
                "descr": f"Leg 2: Buy {qty_leg2_base:.8f} {leg2['base_ccy']} with ~{amount_spent_quote_l2:.8f} {leg2['quote_ccy']}"
            })
            current_amount = qty_leg2_base * (Decimal('1') - self.fee_rate)
            current_asset = leg2["base_ccy"]
        else: # leg2["side"] == "sell"
            if current_asset != leg2["base_ccy"]:
                self.logger.error(f"Asset mismatch for leg 2 sell: Expected to sell {leg2['base_ccy']}, have {current_asset}")
                return None
            qty_leg2_base = current_amount.quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
            amount_received_quote_l2 = (qty_leg2_base * price_leg2)
            trades.append({
                "instId": leg2["pair"], "side": "sell", "ordType": "limit",
                "px": str(price_leg2), "sz": str(qty_leg2_base),
                "baseCcy": leg2["base_ccy"], "quoteCcy": leg2["quote_ccy"],
                "descr": f"Leg 2: Sell {qty_leg2_base:.8f} {leg2['base_ccy']} for ~{amount_received_quote_l2:.8f} {leg2['quote_ccy']}"
            })
            current_amount = amount_received_quote_l2 * (Decimal('1') - self.fee_rate)
            current_asset = leg2["quote_ccy"]

        if current_amount <= Decimal(0): self.logger.warning("Path Calc: Amount became zero after leg 2."); return None

        # --- Leg 3 ---
        leg3 = path_details["step3"]
        price_leg3 = p3
        qty_leg3_base = Decimal(0)
        final_quote_expected = Decimal(0)

        if price_leg3 <= Decimal(0) and leg3["side"] == "buy": # Only problematic if buying and price is bad
             self.logger.error(f"Path Calc Error: Price for leg 3 BUY ({leg3['pair']}) is zero or negative.")
             return None
        # If selling, a zero price for p3 (bid price) might be valid if market is very thin, though unlikely profitable.

        if leg3["side"] == "buy": # This case should not happen if the final asset is quote_stable_asset
            self.logger.error(f"Path Calc Logic Error: Leg 3 trying to buy, but should be selling to {self.quote_stable_asset}")
            return None # Should always end by selling to the starting quote stable asset
        else: # leg3["side"] == "sell" (selling current_asset for quote_stable_asset)
            if current_asset != leg3["base_ccy"]:
                self.logger.error(f"Asset mismatch for leg 3 sell: Expected to sell {leg3['base_ccy']}, have {current_asset}")
                return None
            qty_leg3_base = current_amount.quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
            final_quote_expected = (qty_leg3_base * price_leg3) * (Decimal('1') - self.fee_rate)
            trades.append({
                "instId": leg3["pair"], "side": "sell", "ordType": "limit",
                "px": str(price_leg3), "sz": str(qty_leg3_base),
                "baseCcy": leg3["base_ccy"], "quoteCcy": leg3["quote_ccy"],
                "descr": f"Leg 3: Sell {qty_leg3_base:.8f} {leg3['base_ccy']} for {self.quote_stable_asset}. Expected: {final_quote_expected:.8f} {self.quote_stable_asset}"
            })

        self.logger.info(f"Path Calc: Initial {self.quote_stable_asset}: {self.initial_usdt_amount:.8f}, Final {self.quote_stable_asset} (est.): {final_quote_expected:.8f}, "
                         f"Profit (est.): {(final_quote_expected - self.initial_usdt_amount):.8f} {self.quote_stable_asset}")
        return trades


if __name__ == '__main__':
    class MockMarketDataHandler:
        def __init__(self, test_data): self.test_data = test_data
        def get_best_ask_with_qty(self, inst_id): return self.test_data.get(inst_id, {}).get("ask")
        def get_best_bid_with_qty(self, inst_id): return self.test_data.get(inst_id, {}).get("bid")

    logger = logging.getLogger("StrategyTestDynamic")
    logger.setLevel(logging.DEBUG) # 设置为 DEBUG 以查看更多日志
    if not logger.handlers:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(ch)

    # --- 测试 ETH 套利 ---
    mock_config_eth = {
        'arbitrage': {
            'core_currencies': ["USDT", "ETH", "USDC"],
            'trading_pairs': ["ETH-USDT", "USDC-USDT", "ETH-USDC"], # 确保顺序或查找逻辑能正确匹配
            'fee_rate': '0.001',
            'min_profit_threshold': '0.0005', # 降低阈值以便测试
            'trade_amount_usdt': '100'
        }
    }
    # 模拟市场数据 (ETH 价格较高)
    # Path 1: USDT -> USDC -> ETH -> USDT
    # Buy USDC with USDT: 1 USDC = 0.999 USDT (ask) => 1 USDT = 1/0.999 = 1.001001 USDC
    # Buy ETH with USDC: 1 ETH = 1990 USDC (ask) => 1 USDC = 1/1990 = 0.000502512 ETH
    # Sell ETH for USDT: 1 ETH = 2000 USDT (bid)
    # 1 USDT -> 1.001001 USDC -> 1.001001 * 0.000502512 ETH -> 1.001001 * 0.000502512 * 2000 USDT
    # = 0.000503015 * 2000 = 1.00603 USDT (未计费)
    # 计费后: 1.00603 * (1-0.001)^3 = 1.00603 * 0.997002999 = 1.003011... > 1 + 0.0005
    mock_data_eth_p1_profit = {
        "USDC-USDT": {"ask": ("0.9990", "50000"), "bid": ("0.9985", "50000")}, # 买USDC的价格
        "ETH-USDC":  {"ask": ("1990", "50"),    "bid": ("1988", "50")},       # 买ETH的价格
        "ETH-USDT":  {"ask": ("2002", "50"),    "bid": ("2000", "50")}        # 卖ETH的价格
    }
    logger.info("\n--- Testing ETH Path 1 with Profit ---")
    md_handler_eth_p1 = MockMarketDataHandler(mock_data_eth_p1_profit)
    strategy_eth_p1 = ArbitrageStrategy(md_handler_eth_p1, mock_config_eth, logger)
    opportunities_eth_p1 = strategy_eth_p1.check_opportunities()
    if opportunities_eth_p1:
        logger.info(f"ETH Path 1 Opportunities: {json.dumps(opportunities_eth_p1, indent=2, default=str)}")

    # Path 2: USDT -> ETH -> USDC -> USDT
    # Buy ETH with USDT: 1 ETH = 2000 USDT (ask) -> 1 USDT = 1/2000 = 0.0005 ETH
    # Sell ETH for USDC: 1 ETH = 2005 USDC (bid)
    # Sell USDC for USDT: 1 USDC = 1.0000 USDT (bid)
    # 1 USDT -> 0.0005 ETH -> 0.0005 * 2005 USDC -> 0.0005 * 2005 * 1.0000 USDT
    # = 1.0025 USDC -> 1.0025 * 1.0000 USDT = 1.0025 USDT (未计费)
    # 计费后: 1.0025 * (0.999^3) = 1.0025 * 0.997002999 = 0.9995... (不满足0.0005阈值)
    # 如果 p_sq_bid (USDC-USDT bid) 稍微高一点，比如 1.0010
    # 1.0025 * 1.0010 = 1.0035025 * 0.997002999 = 1.00048... (仍然不满足)
    # 需要更优的价格或更低的费用
    mock_data_eth_p2_profit = {
        "ETH-USDT":  {"ask": ("2000", "50"),    "bid": ("1998", "50")},        # 买ETH的价格
        "ETH-USDC":  {"ask": ("2008", "50"),    "bid": ("2005", "50")},        # 卖ETH的价格
        "USDC-USDT": {"ask": ("1.0005", "50000"), "bid": ("1.0000", "50000")}  # 卖USDC的价格
    }
    logger.info("\n--- Testing ETH Path 2 (likely no profit with these numbers) ---")
    md_handler_eth_p2 = MockMarketDataHandler(mock_data_eth_p2_profit)
    strategy_eth_p2 = ArbitrageStrategy(md_handler_eth_p2, mock_config_eth, logger)
    opportunities_eth_p2 = strategy_eth_p2.check_opportunities()
    if opportunities_eth_p2:
        logger.info(f"ETH Path 2 Opportunities: {json.dumps(opportunities_eth_p2, indent=2, default=str)}")
    else:
        logger.info("No ETH Path 2 opportunities found with the given mock data and threshold.")


