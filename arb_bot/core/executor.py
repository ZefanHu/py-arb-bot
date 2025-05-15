# arb_bot/core/executor.py
import asyncio
import logging
from decimal import Decimal, ROUND_DOWN
import time
import uuid
import json

class OrderExecutor:
    def __init__(self, okx_connector, market_data_handler, config, logger=None):
        self.connector = okx_connector
        self.market_data = market_data_handler
        self.config = config # 全局配置对象
        self.logger = logger or logging.getLogger(__name__)
        if not self.logger.handlers:
            # 如果logger没有handler，添加一个默认的控制台handler
            # logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            pass # 假设logger已由外部（如okx_conn.py）配置
        self.trade_api = self.connector.trade_api

        other_config = self.config.get('other', {})
        self.order_timeout_seconds = other_config.get('order_timeout_seconds', 10)
        self.order_poll_interval = other_config.get('order_poll_interval', 0.5)
        self.opportunity_cooldown_seconds = other_config.get('opportunity_cooldown_seconds', 5)

        self.executed_opportunity_cache = {} # 用于机会冷却
        self.is_trading_active = False # 套利执行状态锁
        self.active_arbitrage_tasks = set() # 跟踪活动的套利任务

        arbitrage_config = self.config.get('arbitrage', {})
        self.core_currencies = arbitrage_config.get('core_currencies', ["USDT", "ETH", "USDC"]) # 提供默认值
        if not self.core_currencies or len(self.core_currencies) != 3:
            self.logger.critical(
                f"Configuration error: 'core_currencies' must contain exactly 3 currencies for executor. Found: {self.core_currencies}"
            )
            # 为了程序在配置错误时仍能尝试运行（尽管可能不正确），提供安全的回退值
            self.quote_stable_asset = "USDT"
            self.primary_arb_asset = self.core_currencies[1] if len(self.core_currencies) > 1 else "ETH"
            self.secondary_stable_asset = self.core_currencies[2] if len(self.core_currencies) > 2 else "USDC"
            self.logger.warning("Using fallback core currencies due to configuration error.")
        else:
            self.quote_stable_asset = self.core_currencies[0]
            self.primary_arb_asset = self.core_currencies[1]
            self.secondary_stable_asset = self.core_currencies[2]

        self.trading_pairs_config = arbitrage_config.get('trading_pairs', [])


    async def _generate_cl_ord_id(self, prefix="arb"):
        """生成符合OKX要求的客户端订单ID。"""
        ts_part = str(int(time.time() * 1000000))[-7:] # 时间戳部分，取后7位增加变化
        uuid_part = uuid.uuid4().hex[:12] # UUID增加唯一性
        base_id = f"{prefix}{ts_part}{uuid_part}"
        cl_ord_id = "".join(filter(str.isalnum, base_id)) # 只保留字母和数字
        if not cl_ord_id or not cl_ord_id[0].isalpha(): # 确保以字母开头
            cl_ord_id = "a" + cl_ord_id
        return cl_ord_id[:32] # 截断到32位

    async def _place_and_monitor_order(self, trade_params, leg_attempt_id_suffix):
        """下单并监控其状态，直到成交、取消或超时。"""
        cl_ord_id_to_send = await self._generate_cl_ord_id(prefix=f"L{leg_attempt_id_suffix}_")
        self.logger.info(f"Attempting to place order: clOrdId={cl_ord_id_to_send}, "
                         f"InstId={trade_params['instId']}, Side={trade_params['side']}, "
                         f"Type={trade_params['ordType']}, Px={trade_params.get('px', 'N/A')}, Sz={trade_params['sz']}")
        try:
            order_params_sdk = {
                'instId': trade_params['instId'], 'tdMode': 'cash', # 现货交易模式
                'side': trade_params['side'], 'ordType': trade_params['ordType'],
                'sz': trade_params['sz'], 'clOrdId': cl_ord_id_to_send
            }
            if trade_params['ordType'] == 'limit' and trade_params.get('px'):
                order_params_sdk['px'] = trade_params['px']

            order_result_sdk = await asyncio.to_thread(self.trade_api.place_order, **order_params_sdk)

            if order_result_sdk.get('code') != '0' or not order_result_sdk.get('data'):
                msg = order_result_sdk.get('msg', 'Unknown error during placement')
                s_msg_detail = msg
                s_code_detail = order_result_sdk.get('code', 'N/A')
                data_list = order_result_sdk.get('data', [])
                if data_list and isinstance(data_list, list) and len(data_list) > 0 and data_list[0].get('sCode') != '0':
                    s_msg_detail = data_list[0].get('sMsg', msg)
                    s_code_detail = data_list[0].get('sCode', s_code_detail)
                self.logger.error(f"Order placement failed for clOrdId {cl_ord_id_to_send}: {s_msg_detail} (Code: {s_code_detail}) Full API: {order_result_sdk}")
                return {"status": "failed_placement", "clOrdId": cl_ord_id_to_send, "error_message": s_msg_detail, "sCode": s_code_detail}

            order_data = order_result_sdk['data'][0]
            ord_id = order_data.get('ordId')
            returned_cl_ord_id = order_data.get('clOrdId', cl_ord_id_to_send)
            if order_data.get('sCode') != '0':
                self.logger.error(f"Order rejected by exchange for clOrdId {returned_cl_ord_id} (ordId: {ord_id}): {order_data.get('sMsg')} (sCode: {order_data.get('sCode')})")
                return {"status": "failed_placement_sCode", "clOrdId": returned_cl_ord_id, "ordId": ord_id, "error_message": order_data.get('sMsg'), "sCode": order_data.get('sCode')}

            self.logger.info(f"Order placed: clOrdId={returned_cl_ord_id}, ordId={ord_id}. Monitoring...")
            start_time = time.time()
            while time.time() - start_time < self.order_timeout_seconds:
                await asyncio.sleep(self.order_poll_interval)
                status_res = await asyncio.to_thread(self.trade_api.get_orders, instId=trade_params['instId'], ordId=ord_id)
                if status_res.get('code') == '0' and status_res.get('data'):
                    status_data = status_res['data'][0]
                    state = status_data.get('state')
                    fill_sz_str = status_data.get('accFillSz', '0')
                    avg_px_str = status_data.get('avgPx', '0')
                    fill_sz = Decimal(fill_sz_str)
                    avg_px = Decimal(avg_px_str if avg_px_str and avg_px_str.strip() else '0') # avgPx 可能为空字符串
                    fee = Decimal(status_data.get('fee', '0'))
                    fee_ccy = status_data.get('feeCcy', '')
                    self.logger.debug(f"Order {ord_id}: State={state}, FilledSz={fill_sz}, AvgPx={avg_px}, Fee={fee}{fee_ccy}")
                    if state == 'filled':
                        self.logger.info(f"Order {ord_id} ({returned_cl_ord_id}) FILLED. Sz:{fill_sz}, Px:{avg_px}, Fee:{fee}{fee_ccy}")
                        return {"status": "filled", "ordId": ord_id, "clOrdId": returned_cl_ord_id, "fillSz": fill_sz, "avgPx": avg_px,
                                "executed_base_qty": fill_sz, "executed_quote_qty": fill_sz * avg_px, "fee_paid": fee, "fee_ccy": fee_ccy}
                    if state in ['canceled', 'mmp_canceled']:
                        self.logger.warning(f"Order {ord_id} ({returned_cl_ord_id}) CANCELED. Filled:{fill_sz}")
                        return {"status": "canceled", "ordId": ord_id, "clOrdId": returned_cl_ord_id, "fillSz": fill_sz, "avgPx": avg_px,
                                "executed_base_qty": fill_sz, "executed_quote_qty": fill_sz * avg_px, "fee_paid": fee, "fee_ccy": fee_ccy}
                else:
                    self.logger.warning(f"Failed to get status for {ord_id}: {status_res.get('msg')}")

            self.logger.warning(f"Order {ord_id} ({returned_cl_ord_id}) timed out. Attempting cancel.")
            cancel_res = await asyncio.to_thread(self.trade_api.cancel_order, instId=trade_params['instId'], ordId=ord_id)
            final_fill_sz, final_avg_px, final_fee, final_fee_ccy = Decimal('0'), Decimal('0'), Decimal('0'), ''
            if cancel_res.get('code') == '0' and cancel_res.get('data'):
                cancel_data_item = cancel_res['data'][0]
                self.logger.info(f"Cancel for {ord_id} sent. sCode:{cancel_data_item.get('sCode')}, sMsg:{cancel_data_item.get('sMsg')}")
                if cancel_data_item.get('sCode') == '0':
                    await asyncio.sleep(self.order_poll_interval * 2)
                    final_status = await asyncio.to_thread(self.trade_api.get_orders, instId=trade_params['instId'], ordId=ord_id)
                    if final_status.get('code') == '0' and final_status.get('data'):
                        s_data = final_status['data'][0]
                        final_fill_sz_str = s_data.get('accFillSz','0')
                        avg_px_str_final = s_data.get('avgPx','0')
                        final_fill_sz = Decimal(final_fill_sz_str)
                        final_avg_px = Decimal(avg_px_str_final if avg_px_str_final and avg_px_str_final.strip() else '0')
                        final_fee, final_fee_ccy = Decimal(s_data.get('fee','0')), s_data.get('feeCcy','')
                        self.logger.info(f"Order {ord_id} final state after cancel: {s_data.get('state')}, Filled:{final_fill_sz}")
            else:
                self.logger.error(f"Failed to cancel {ord_id}: {cancel_res.get('msg')}")
            return {"status": "canceled_after_timeout", "ordId": ord_id, "clOrdId": returned_cl_ord_id, "fillSz": final_fill_sz, "avgPx": final_avg_px,
                    "executed_base_qty": final_fill_sz, "executed_quote_qty": final_fill_sz * final_avg_px, "fee_paid": final_fee, "fee_ccy": final_fee_ccy}
        except Exception as e:
            self.logger.error(f"Exception in order processing for {cl_ord_id_to_send}: {e}", exc_info=True)
            return {"status": "exception", "clOrdId": cl_ord_id_to_send, "error_message": str(e)}

    async def execute_arbitrage_path(self, path_id_prefix, trades_sequence, opportunity_key):
        if self.is_trading_active:
            self.logger.info(f"Trading active. Skipping {path_id_prefix} for key {opportunity_key}")
            return False
        self.is_trading_active = True

        initial_quote_amount_str = str(self.config.get('arbitrage', {}).get('trade_amount_usdt', '100.0'))
        initial_amount_for_cycle = Decimal(initial_quote_amount_str)

        # assets_in_cycle: 追踪当前这个套利循环中，理论上我们持有的各种币的数量
        assets_in_cycle = {ccy: Decimal('0') for ccy in self.core_currencies}
        assets_in_cycle[self.quote_stable_asset] = initial_amount_for_cycle

        attempt_id = f"{path_id_prefix}_{uuid.uuid4().hex[:6]}"
        executed_legs_details = []

        try:
            current_time = time.time()
            if current_time - self.executed_opportunity_cache.get(opportunity_key, 0) < self.opportunity_cooldown_seconds:
                self.logger.debug(f"Opportunity {opportunity_key} in cooldown. Skipping.")
                self.is_trading_active = False; return False
            self.executed_opportunity_cache[opportunity_key] = current_time

            self.logger.info(f"Executing path {attempt_id}: {len(trades_sequence)} legs. Initial {self.quote_stable_asset}: {initial_amount_for_cycle}")

            for i, leg_template in enumerate(trades_sequence):
                leg_num = i + 1
                self.logger.info(f"Path {attempt_id} - Leg {leg_num}: {leg_template['descr']}")
                current_leg_params = leg_template.copy()

                asset_to_spend_for_this_leg = ""
                if current_leg_params['side'] == 'buy':
                    asset_to_spend_for_this_leg = current_leg_params['quoteCcy']
                elif current_leg_params['side'] == 'sell':
                    asset_to_spend_for_this_leg = current_leg_params['baseCcy']

                amount_to_spend_for_this_leg = assets_in_cycle.get(asset_to_spend_for_this_leg, Decimal(0))

                if amount_to_spend_for_this_leg <= Decimal('1e-9'): # 允许极小的正值，但主要防止0或负
                    self.logger.error(f"Path {attempt_id} - Leg {leg_num}: Insufficient or zero amount ({amount_to_spend_for_this_leg:.8f} {asset_to_spend_for_this_leg}) to spend. Aborting.")
                    await self._initiate_recovery(attempt_id, assets_in_cycle, executed_legs_details)
                    self.is_trading_active = False; return False

                instr_details_list = await asyncio.to_thread(self.connector.get_instrument_details, instType='SPOT', instId=current_leg_params['instId'])
                if not instr_details_list or not instr_details_list[0]:
                    self.logger.error(f"Path {attempt_id} - Leg {leg_num}: Failed to get instrument details for {current_leg_params['instId']}. Aborting.")
                    await self._initiate_recovery(attempt_id, assets_in_cycle, executed_legs_details)
                    self.is_trading_active = False; return False

                instr_details = instr_details_list[0]
                lot_sz_decimal = Decimal(instr_details.get('lotSz', '0.00000001'))
                min_sz_decimal = Decimal(instr_details.get('minSz', '0.00000001'))

                calculated_sz_for_leg = Decimal(0)
                if current_leg_params['side'] == 'buy':
                    price = Decimal(current_leg_params['px'])
                    if price <= Decimal(0):
                        self.logger.error(f"Path {attempt_id} - Leg {leg_num}: Price is zero or negative for buy. Aborting.");
                        self.is_trading_active = False; return False
                    calculated_sz_for_leg = (amount_to_spend_for_this_leg / price).quantize(lot_sz_decimal, rounding=ROUND_DOWN)
                else: # side == 'sell'
                    calculated_sz_for_leg = amount_to_spend_for_this_leg.quantize(lot_sz_decimal, rounding=ROUND_DOWN)

                current_leg_params['sz'] = str(calculated_sz_for_leg)
                self.logger.info(f"Path {attempt_id} - Leg {leg_num}: Calculated sz: {calculated_sz_for_leg} {current_leg_params['baseCcy']} (spending ~{amount_to_spend_for_this_leg:.8f} {asset_to_spend_for_this_leg})")

                if calculated_sz_for_leg < min_sz_decimal:
                    self.logger.error(f"Path {attempt_id} - Leg {leg_num}: Calculated sz {calculated_sz_for_leg} < minSz {min_sz_decimal} for {current_leg_params['instId']}. Aborting.")
                    await self._initiate_recovery(attempt_id, assets_in_cycle, executed_legs_details)
                    self.is_trading_active = False; return False

                outcome = await self._place_and_monitor_order(current_leg_params, f"{attempt_id}_L{leg_num}")
                executed_legs_details.append({"leg_params": current_leg_params, "outcome": outcome, "success": (outcome and outcome.get("status") == "filled")})

                # --- 修正 `assets_in_cycle` 更新逻辑 ---
                base_qty_filled = outcome.get('executed_base_qty', Decimal(0))
                quote_qty_involved = outcome.get('executed_quote_qty', Decimal(0))
                fee_paid = outcome.get('fee_paid', Decimal(0))
                fee_ccy = outcome.get('fee_ccy', '')
                base_ccy_leg = current_leg_params['baseCcy']
                quote_ccy_leg = current_leg_params['quoteCcy']

                if not outcome or outcome.get("status") != "filled":
                    self.logger.error(f"Path {attempt_id} - Leg {leg_num} FAILED or not fully filled. Status: {outcome.get('status') if outcome else 'Unknown'}.")
                    # 即使失败，如果部分成交，也需要更新 assets_in_cycle 以便正确恢复
                    if outcome and base_qty_filled > Decimal(0): # 检查是否有实际成交量
                        self.logger.info(f"Path {attempt_id} - Leg {leg_num} had partial fill: {base_qty_filled} {base_ccy_leg}. Updating cycle assets before recovery.")
                        if current_leg_params['side'] == 'buy':
                            assets_in_cycle[base_ccy_leg] += base_qty_filled
                            assets_in_cycle[quote_ccy_leg] -= quote_qty_involved
                            if fee_ccy == base_ccy_leg: assets_in_cycle[base_ccy_leg] -= fee_paid
                            elif fee_ccy == quote_ccy_leg: assets_in_cycle[quote_ccy_leg] -= fee_paid
                        elif current_leg_params['side'] == 'sell':
                            assets_in_cycle[base_ccy_leg] -= base_qty_filled
                            assets_in_cycle[quote_ccy_leg] += quote_qty_involved
                            if fee_ccy == quote_ccy_leg: assets_in_cycle[quote_ccy_leg] -= fee_paid
                            elif fee_ccy == base_ccy_leg: assets_in_cycle[base_ccy_leg] -= fee_paid
                    await self._initiate_recovery(attempt_id, assets_in_cycle, executed_legs_details)
                    self.is_trading_active = False; return False

                self.logger.info(f"Path {attempt_id} - Leg {leg_num} successfully filled.")

                # 直接修改 assets_in_cycle 中的现有值
                if current_leg_params['side'] == 'buy':
                    assets_in_cycle[quote_ccy_leg] -= quote_qty_involved  # 减少花费的 quote_ccy
                    assets_in_cycle[base_ccy_leg] += base_qty_filled     # 增加获得的 base_ccy
                    # 手续费调整
                    if fee_ccy == base_ccy_leg:
                        assets_in_cycle[base_ccy_leg] -= fee_paid
                    elif fee_ccy == quote_ccy_leg:
                        assets_in_cycle[quote_ccy_leg] -= fee_paid
                elif current_leg_params['side'] == 'sell':
                    assets_in_cycle[base_ccy_leg] -= base_qty_filled      # 减少卖出的 base_ccy
                    assets_in_cycle[quote_ccy_leg] += quote_qty_involved  # 增加获得的 quote_ccy
                    # 手续费调整
                    if fee_ccy == quote_ccy_leg:
                        assets_in_cycle[quote_ccy_leg] -= fee_paid
                    elif fee_ccy == base_ccy_leg:
                        assets_in_cycle[base_ccy_leg] -= fee_paid

                # 清理极小值，避免浮点累积误差，并使用 abs()
                for ccy_key_cleanup in list(assets_in_cycle.keys()):
                    if abs(assets_in_cycle[ccy_key_cleanup]) < Decimal('1e-9'):
                        assets_in_cycle[ccy_key_cleanup] = Decimal('0')

                # 使用 abs() 进行日志记录的条件判断
                log_assets = {k: v.quantize(Decimal('1e-8')) for k,v in assets_in_cycle.items() if abs(v) > Decimal('1e-9')}
                self.logger.info(f"Path {attempt_id} - Assets in cycle after Leg {leg_num}: {log_assets}")

                for ccy_check, bal_check in assets_in_cycle.items():
                    # 允许起始稳定币在中间步骤暂时为负（因预扣或计算顺序），但在最后应该回正
                    # 对于非起始稳定币，不应出现显著负值
                    if bal_check < Decimal('-1e-8'): # 允许非常小的负数容差
                        if ccy_check == self.quote_stable_asset and leg_num < len(trades_sequence):
                             self.logger.debug(f"Path {attempt_id} - Leg {leg_num}: {self.quote_stable_asset} temporarily {bal_check}. This is acceptable mid-path.")
                             continue # 中间步骤起始稳定币为负可以接受

                        self.logger.critical(f"Path {attempt_id} - CRITICAL: Unexpected negative balance for {ccy_check}: {bal_check}. Aborting. Cycle assets: {log_assets}")
                        await self._initiate_recovery(attempt_id, assets_in_cycle, executed_legs_details)
                        self.is_trading_active = False; return False

            final_val = assets_in_cycle.get(self.quote_stable_asset, Decimal('0'))
            profit = final_val - initial_amount_for_cycle
            self.logger.info(f"Path {attempt_id} completed. Initial {self.quote_stable_asset}: {initial_amount_for_cycle:.8f}, "
                             f"Final theoretical {self.quote_stable_asset} in cycle: {final_val:.8f}, Theoretical profit: {profit:.8f}")
            self.is_trading_active = False
            return True

        except Exception as e:
            self.logger.error(f"Error in execute_arbitrage_path for {attempt_id if 'attempt_id' in locals() else path_id_prefix}: {e}", exc_info=True)
            assets_for_rec = assets_in_cycle if 'assets_in_cycle' in locals() else {}
            legs_info = executed_legs_details if 'executed_legs_details' in locals() else []
            if not assets_for_rec and 'initial_amount_for_cycle' in locals():
                assets_for_rec[self.quote_stable_asset] = initial_amount_for_cycle
            await self._initiate_recovery(attempt_id if 'attempt_id' in locals() else path_id_prefix, assets_for_rec, legs_info)
            self.is_trading_active = False
            return False
        finally:
            self.is_trading_active = False

    def _find_recovery_pair(self, asset_to_sell, target_asset):
        """辅助函数，查找用于恢复的交易对。"""
        asset_to_sell_upper = asset_to_sell.upper()
        target_asset_upper = target_asset.upper()
        for pair_name in self.trading_pairs_config:
            parts = pair_name.upper().split('-')
            # 我们要卖出 asset_to_sell (作为base) 来换取 target_asset (作为quote)
            if (parts[0] == asset_to_sell_upper and parts[1] == target_asset_upper):
                return pair_name
        self.logger.warning(f"Could not find direct pair to sell {asset_to_sell} for {target_asset} where {asset_to_sell} is base.")
        return None


    async def _initiate_recovery(self, attempt_id, assets_held_from_failed_cycle, executed_legs_details):
        """更精确的恢复逻辑：只尝试卖出在失败的套利循环中实际获得的、非目标的资产。"""
        self.logger.warning(f"Path {attempt_id} - Initiating TARGETED recovery...")
        # 使用 abs() 进行日志记录的条件判断
        log_assets_to_recover = {k: str(v.quantize(Decimal('1e-8'))) for k, v in assets_held_from_failed_cycle.items() if abs(v) > Decimal('1e-9')}
        self.logger.info(f"    Assets held from this specific failed cycle (non-zero balances): {log_assets_to_recover}")
        if executed_legs_details:
            self.logger.info(f"    Last leg details: {json.dumps(executed_legs_details[-1:], default=str, cls=DecimalEncoder)}") # 使用自定义Encoder处理Decimal

        try:
            for asset_ccy, amount_held_in_cycle_theoretical in assets_held_from_failed_cycle.items():
                # 只处理正向持有的、非目标稳定币的资产
                if asset_ccy == self.quote_stable_asset or amount_held_in_cycle_theoretical <= Decimal('1e-9'):
                    continue

                is_primary_arb = (asset_ccy == self.primary_arb_asset)
                is_secondary_non_target_stable = (asset_ccy == self.secondary_stable_asset and asset_ccy != self.quote_stable_asset)

                if not (is_primary_arb or is_secondary_non_target_stable):
                    self.logger.info(f"Path {attempt_id} - Recovery: Skipping {asset_ccy} as it's not a relevant asset from this cycle needing recovery to {self.quote_stable_asset}.")
                    continue

                recovery_pair_name = self._find_recovery_pair(asset_ccy, self.quote_stable_asset)
                if not recovery_pair_name:
                    self.logger.error(f"Path {attempt_id} - Recovery: No direct pair found to sell {asset_ccy} for {self.quote_stable_asset}. Manual intervention for {amount_held_in_cycle_theoretical:.8f} {asset_ccy}.")
                    continue

                # 获取最新的账户可用余额
                balance_data = await asyncio.to_thread(self.connector.account_api.get_account, ccy=asset_ccy)
                actual_available_balance = Decimal('0')
                if balance_data and balance_data.get('code') == '0' and balance_data.get('data') and balance_data['data'][0].get('details'):
                    for detail in balance_data['data'][0]['details']:
                        if detail.get('ccy') == asset_ccy:
                            actual_available_balance = Decimal(detail.get('availBal', '0'))
                            break
                else:
                    self.logger.warning(f"Path {attempt_id} - Recovery: Could not fetch current available balance for {asset_ccy}. Will use theoretical amount from cycle ({amount_held_in_cycle_theoretical:.8f}) if positive and available.")

                # 确定实际能卖出的数量：取“本次循环理论持有的数量”和“账户实际可用数量”中的较小者
                amount_to_attempt_sell = min(amount_held_in_cycle_theoretical, actual_available_balance)

                if amount_to_attempt_sell <= Decimal('1e-9'):
                    self.logger.info(f"Path {attempt_id} - Recovery: No significant or available amount of {asset_ccy} (to sell: {amount_to_attempt_sell:.8f}, theoretical: {amount_held_in_cycle_theoretical:.8f}, available: {actual_available_balance:.8f}) to recover. Skipping.")
                    continue

                instr_details_list = await asyncio.to_thread(self.connector.get_instrument_details, instType='SPOT', instId=recovery_pair_name)
                if not instr_details_list or not instr_details_list[0]:
                    self.logger.error(f"Path {attempt_id} - Recovery: Failed to get instrument details for {recovery_pair_name}. Skipping {asset_ccy}.")
                    continue

                instr_details = instr_details_list[0]
                min_sz_recovery = Decimal(instr_details.get('minSz', '0.00000001'))
                lot_sz_recovery = Decimal(instr_details.get('lotSz', '0.00000001'))

                if instr_details.get('baseCcy') != asset_ccy: # 确保我们要卖的币是交易对的基础币
                    self.logger.error(f"Path {attempt_id} - Recovery: {asset_ccy} is not baseCcy of {recovery_pair_name}. Cannot directly sell for recovery. Skipping.")
                    continue

                quantized_amount_to_sell = amount_to_attempt_sell.quantize(lot_sz_recovery, rounding=ROUND_DOWN)

                if quantized_amount_to_sell < min_sz_recovery:
                    self.logger.warning(f"Path {attempt_id} - Recovery: Quantized amount {quantized_amount_to_sell} {asset_ccy} for {recovery_pair_name} is below minSz {min_sz_recovery}. Skipping.")
                    continue

                self.logger.info(f"Path {attempt_id} - TARGETED RECOVERY: Selling {quantized_amount_to_sell} {asset_ccy} for {self.quote_stable_asset} via market order on {recovery_pair_name}.")
                recovery_params = {
                    "instId": recovery_pair_name, "side": "sell", "ordType": "market",
                    "sz": str(quantized_amount_to_sell),
                    "baseCcy": asset_ccy, "quoteCcy": self.quote_stable_asset,
                    "descr": f"Targeted Recovery: Sell {asset_ccy} from cycle {attempt_id}"
                }
                outcome = await self._place_and_monitor_order(recovery_params, f"REC_TGT_{asset_ccy}")
                self.logger.info(f"Path {attempt_id} - {asset_ccy} targeted recovery outcome: {outcome}")

            self.logger.info(f"Path {attempt_id} - Targeted recovery process finished.")
        except Exception as e:
            self.logger.error(f"Error during targeted recovery for {attempt_id}: {e}", exc_info=True)

# 自定义JSON Encoder来处理Decimal对象，用于日志打印
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj) # 将Decimal转换为字符串
        return json.JSONEncoder.default(self, obj)


