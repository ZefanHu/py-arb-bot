# arb_bot/connectors/okx_conn.py
import yaml
import json
import asyncio
import websockets
from pathlib import Path
import logging
from decimal import Decimal # <--- 修复：为 sample_private_callback 导入 Decimal
import time

# --- 主要导入部分 ---
try:
    from ..okx_sdk import Account_api as Account
    from ..okx_sdk import Public_api as Public
    from ..okx_sdk import Market_api as Market
    from ..okx_sdk import Trade_api as Trade
    from ..okx_sdk.utils import get_timestamp as sdk_get_timestamp
    from ..okx_sdk.exceptions import OkexAPIException
    from ..utils.logger import setup_logger
    from ..core.market_data import MarketDataHandler
    from ..core.strategy import ArbitrageStrategy
    from ..core.executor import OrderExecutor
except ImportError as e:
    # --- Fallback 导入部分 ---
    print(f"相对导入失败: {e}。尝试使用备用路径进行导入。")
    import sys
    current_path = Path(__file__).resolve()
    project_root_dir_arb_bot = current_path.parent.parent
    py_arb_bot_dir = project_root_dir_arb_bot.parent
    if str(py_arb_bot_dir) not in sys.path:
        sys.path.insert(0, str(py_arb_bot_dir))
        print(f"已将备用路径添加到 sys.path: {py_arb_bot_dir}")
    from arb_bot.okx_sdk import Account_api as Account
    from arb_bot.okx_sdk import Public_api as Public
    from arb_bot.okx_sdk import Market_api as Market
    from arb_bot.okx_sdk import Trade_api as Trade
    from arb_bot.okx_sdk.utils import get_timestamp as sdk_get_timestamp
    from arb_bot.okx_sdk.exceptions import OkexAPIException
    from arb_bot.utils.logger import setup_logger
    from arb_bot.core.market_data import MarketDataHandler
    from arb_bot.core.strategy import ArbitrageStrategy
    from arb_bot.core.executor import OrderExecutor
    print("备用路径导入成功。")


class OKXConnector:
    # ... (OKXConnector 类的代码与上一版本相同，此处省略以保持简洁) ...
    def __init__(self, config_path_base="config"):
        self.logger = setup_logger(__name__, "INFO", log_to_file=True)
        base_dir = Path(__file__).resolve().parent.parent
        self.settings_path = base_dir / config_path_base / "settings.yaml"
        self.secrets_path = base_dir / config_path_base / "secrets.yaml"
        self._load_config()
        self._initialize_clients()

    def _load_config(self):
        try:
            with open(self.settings_path, 'r') as f:
                self.settings = yaml.safe_load(f)
            self.logger.info(f"配置文件 settings.yaml 加载成功，路径: {self.settings_path}")
        except FileNotFoundError:
            self.logger.error(f"配置文件 settings.yaml 未找到，路径: {self.settings_path}")
            raise
        except yaml.YAMLError as e:
            self.logger.error(f"解析 settings.yaml 文件失败: {e}")
            raise

        try:
            with open(self.secrets_path, 'r') as f:
                self.secrets = yaml.safe_load(f)
            self.logger.info(f"密钥文件 secrets.yaml 加载成功，路径: {self.secrets_path}")
        except FileNotFoundError:
            self.logger.error(f"密钥文件 secrets.yaml 未找到，路径: {self.secrets_path}")
            raise
        except yaml.YAMLError as e:
            self.logger.error(f"解析 secrets.yaml 文件失败: {e}")
            raise

        self.api_key = self.secrets.get('okx', {}).get('apikey')
        self.secret_key = self.secrets.get('okx', {}).get('secretkey')
        self.passphrase = self.secrets.get('okx', {}).get('passphrase')
        self.simulated = self.secrets.get('okx', {}).get('simulated', True)
        self.flag = '1' if self.simulated else '0'

        api_settings = self.settings.get('api', {})
        self.rest_endpoint = api_settings.get('rest_endpoint', "https://www.okx.com")
        if self.simulated:
            self.ws_public_url = api_settings.get('ws_public', "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999")
            self.ws_private_url = api_settings.get('ws_private', "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999")
        else:
            self.ws_public_url = api_settings.get('ws_public_real', "wss://ws.okx.com:8443/ws/v5/public")
            self.ws_private_url = api_settings.get('ws_private_real', "wss://ws.okx.com:8443/ws/v5/private")

        self.logger.info(f"配置加载完成。模式: {'模拟盘' if self.simulated else '实盘交易'}")
        self.logger.info(f"公共 WebSocket URL: {self.ws_public_url}")
        self.logger.info(f"私有 WebSocket URL: {self.ws_private_url}")


    def _initialize_clients(self):
        if not all([self.api_key, self.secret_key, self.passphrase]):
            self.logger.error("API key, secret key, 或 passphrase 未在 secrets.yaml 中配置。私有API功能将受限。")

        self.account_api = Account.AccountAPI(self.api_key, self.secret_key, self.passphrase, use_server_time=False, flag=self.flag)
        self.public_api = Public.PublicAPI(self.api_key, self.secret_key, self.passphrase, use_server_time=False, flag=self.flag)
        self.market_api = Market.MarketAPI(self.api_key, self.secret_key, self.passphrase, use_server_time=False, flag=self.flag)
        self.trade_api = Trade.TradeAPI(self.api_key, self.secret_key, self.passphrase, use_server_time=False, flag=self.flag)

        self.logger.info(f"OKX API 客户端初始化完成。")

    def get_server_time_sdk(self):
        try:
            result = self.public_api.get_system_time()
            if result.get('code') == '0' and result.get('data'):
                server_timestamp_str = result['data'][0]['ts']
                self.logger.debug(f"OKX 服务器时间 (ms): {server_timestamp_str}")
                return server_timestamp_str
            else:
                self.logger.error(f"获取服务器时间失败: {result.get('msg')} (Code: {result.get('code')})")
                return None
        except OkexAPIException as e:
            self.logger.error(f"获取服务器时间时发生 OkexAPIException: {e}")
        except Exception as e:
            self.logger.error(f"获取服务器时间时发生通用异常: {e}")
        return None

    def get_account_balance(self, ccy=None):
        try:
            result = self.account_api.get_account(ccy=ccy) if ccy else self.account_api.get_account()
            if result.get('code') == '0':
                self.logger.info(f"账户余额获取成功。")
                return result.get('data')
            else:
                self.logger.error(f"获取账户余额失败: {result.get('msg')} (Code: {result.get('code')}) - 详情: {result}")
                return result
        except Exception as e:
            self.logger.error(f"获取账户余额时发生通用异常: {e}", exc_info=True)
        return None

    def get_instrument_details(self, instType, uly=None, instId=None):
        try:
            result = self.public_api.get_instruments(instType=instType, uly=uly, instId=instId)
            if result.get('code') == '0':
                self.logger.info(f"产品详情 {instType} {instId or uly or ''} 获取成功: {len(result.get('data', []))} 个产品。")
                return result.get('data')
            else:
                self.logger.error(f"获取产品详情失败: {result.get('msg')} (Code: {result.get('code')})")
                return result
        except Exception as e:
            self.logger.error(f"获取产品详情时发生通用异常: {e}", exc_info=True)
        return None

    def get_fee_rates(self, instType, instId=None, uly=None, category=None):
        try:
            result = self.account_api.get_fee_rates(instType=instType, instId=instId, uly=uly, category=category)
            if result.get('code') == '0':
                self.logger.info(f"{instType} 的费率获取成功。")
                return result.get('data')
            else:
                self.logger.error(f"获取费率失败: {result.get('msg')} (Code: {result.get('code')})")
                return result
        except Exception as e:
            self.logger.error(f"获取费率时发生通用异常: {e}", exc_info=True)
        return None

    async def ws_login(self, ws):
        if not self.api_key: self.logger.warning("API key missing for WS login."); return False

        server_time_ms_str = self.get_server_time_sdk()
        timestamp_sec = str(int(int(server_time_ms_str) / 1000)) if server_time_ms_str else str(int(time.time()))
        if not server_time_ms_str: self.logger.warning("获取服务器时间失败，使用本地时间进行WebSocket登录 (可靠性较低)。")

        message_to_sign = timestamp_sec + 'GET' + '/users/self/verify'

        import hmac, base64
        signature = base64.b64encode(hmac.new(bytes(self.secret_key, 'utf8'), bytes(message_to_sign, 'utf-8'), 'sha256').digest()).decode()

        login_payload = {
            "op": "login",
            "args": [{"apiKey": self.api_key, "passphrase": self.passphrase, "timestamp": timestamp_sec, "sign": signature}]
        }
        await ws.send(json.dumps(login_payload))
        self.logger.info(f"登录请求已发送至WebSocket (时间戳: {timestamp_sec})")
        try:
            response_raw = await asyncio.wait_for(ws.recv(), timeout=10)
            response_data = json.loads(response_raw)
            self.logger.info(f"WebSocket登录响应: {response_raw}")
            if response_data.get("event") == "login" and response_data.get("code") == "0":
                self.logger.info("WebSocket登录成功。")
                return True
            self.logger.error(f"WebSocket登录失败: {response_data.get('msg')} (Code: {response_data.get('code')})")
            return False
        except asyncio.TimeoutError:
            self.logger.error("等待WebSocket登录响应超时。")
            return False
        except Exception as e:
            self.logger.error(f"WebSocket登录响应接收/解析异常: {e}", exc_info=True)
            return False

    async def subscribe_to_channels(self, ws, channels):
        if not channels:
            self.logger.warning("没有提供需要订阅的频道。")
            return False

        subscription_payload = {"op": "subscribe", "args": channels}
        await ws.send(json.dumps(subscription_payload))
        self.logger.info(f"频道订阅请求已发送: {json.dumps(channels)}")

        try:
            response_raw = await asyncio.wait_for(ws.recv(), timeout=10)
            response_data = json.loads(response_raw)
            self.logger.info(f"频道订阅响应: {response_raw}")

            if response_data.get("event") == "subscribe":
                if "arg" in response_data and response_data["arg"].get("channel") in [ch.get("channel") for ch in channels]:
                     self.logger.info(f"成功订阅频道 (至少部分成功，首个确认频道: {response_data['arg']})")
                     return True
                else:
                    self.logger.info(f"订阅事件已接收，假设所有请求的频道订阅成功: {channels}")
                    return True
            elif response_data.get("event") == "error":
                self.logger.error(f"订阅频道失败: {response_data.get('msg')} (Code: {response_data.get('code')}) Arg: {response_data.get('arg')}")
                return False

            self.logger.warning(f"收到未预期的订阅响应格式: {response_raw[:200]}。假设数据流将开始。")
            return True

        except asyncio.TimeoutError:
            self.logger.error("等待WebSocket订阅响应超时。")
            return False
        except Exception as e:
            self.logger.error(f"WebSocket订阅响应接收/解析异常: {e}", exc_info=True)
            return False

    async def _websocket_handler_loop(self, ws, callback, ws_type="Public"):
        heartbeat_interval = self.settings.get('other', {}).get('heartbeat_interval', 25)
        while True:
            try:
                message_raw = await asyncio.wait_for(ws.recv(), timeout=heartbeat_interval + 5)
                if message_raw == 'pong':
                    self.logger.debug(f"{ws_type} WebSocket pong 消息已接收。")
                    continue

                try:
                    message_data = json.loads(message_raw)
                except json.JSONDecodeError:
                    self.logger.warning(f"在 {ws_type} WebSocket 上收到非JSON消息: {message_raw}")
                    continue

                await callback(message_data)
            except asyncio.TimeoutError:
                self.logger.debug(f"{ws_type} WebSocket 接收超时 (>{heartbeat_interval+5}s)，发送 ping 以保持连接。")
                try:
                    await ws.send('ping')
                except websockets.exceptions.ConnectionClosed:
                    self.logger.warning(f"{ws_type} WebSocket 连接在发送 ping 时已关闭。")
                    break
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.warning(f"{ws_type} WebSocket 连接已关闭: Code={e.code}, Reason='{e.reason}'")
                break
            except Exception as e:
                self.logger.error(f"{ws_type} WebSocket 处理循环中发生错误: {e}", exc_info=True)
                break

    async def _websocket_manager(self, url, channels, callback, ws_type="Public", requires_login=False):
        retry_delay = self.settings.get('other',{}).get('api_retry_delay', 5)
        ping_interval_setting = self.settings.get('other',{}).get('heartbeat_interval', 25)

        while True:
            try:
                async with websockets.connect(url, ping_interval=ping_interval_setting, ping_timeout=max(5, ping_interval_setting - 5)) as ws: # ping_timeout 不能为0或负
                    self.logger.info(f"已连接到 {ws_type} WebSocket: {url}")

                    subscribed_successfully = False
                    if requires_login:
                        if await self.ws_login(ws):
                            subscribed_successfully = await self.subscribe_to_channels(ws, channels)
                        else:
                            self.logger.error(f"登录到 {ws_type} WebSocket 失败。稍后重试...")
                    else:
                        subscribed_successfully = await self.subscribe_to_channels(ws, channels)

                    if subscribed_successfully:
                        await self._websocket_handler_loop(ws, callback, ws_type)
                    else:
                        self.logger.error(f"订阅 {ws_type} 频道失败。稍后重试连接...")

            except (ConnectionRefusedError, websockets.exceptions.InvalidURI,
                    websockets.exceptions.InvalidHandshake, OSError,
                    websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosedOK) as e:
                self.logger.error(f"{ws_type} WebSocket 连接/握手错误或正常关闭: {type(e).__name__} - {e}。将在 {retry_delay}秒 后重试...")
            except Exception as e:
                self.logger.error(f"{ws_type} WebSocket 管理器发生意外异常: {e}", exc_info=True)

            self.logger.info(f"{ws_type} WebSocket 已断开。将在 {retry_delay}秒 后尝试重新连接...")
            await asyncio.sleep(retry_delay)

    async def public_websocket_handler(self, channels, callback):
        await self._websocket_manager(self.ws_public_url, channels, callback, "Public", requires_login=False)

    async def private_websocket_handler(self, channels, callback):
        await self._websocket_manager(self.ws_private_url, channels, callback, "Private", requires_login=True)

# --- 全局变量，用于在回调中访问处理器实例 ---
market_data_handler_global = None
arbitrage_strategy_global = None
order_executor_global = None

# --- WebSocket 回调函数 ---
async def market_data_websocket_callback(message_data):
    global market_data_handler_global, arbitrage_strategy_global, order_executor_global
    current_logger = logging.getLogger(__name__)

    if market_data_handler_global:
        try:
            market_data_handler_global.process_websocket_message(message_data)
        except Exception as e:
            current_logger.error(f"回调函数中处理市场数据时出错: {e}", exc_info=True)
            return

        if arbitrage_strategy_global and order_executor_global:
            if order_executor_global.is_trading_active:
                return

            try:
                opportunities = arbitrage_strategy_global.check_opportunities()
            except Exception as e:
                current_logger.error(f"回调函数中检查套利机会时出错: {e}", exc_info=True)
                return

            if opportunities:
                for opportunity in opportunities:
                    price_key_parts = []
                    for leg in opportunity['trades']:
                        price_key_parts.append(f"{leg['instId']}:{leg['px']}")
                    opportunity_prices_key = frozenset(price_key_parts)

                    current_logger.info(f"尝试执行套利机会: {opportunity['path_id']} (机会键: {opportunity_prices_key}) "
                                        f"预期利润率: {opportunity['profit_ratio']:.6f}")

                    # 创建异步任务来执行套利路径
                    # 将任务添加到 OrderExecutor 内部的集合中进行跟踪
                    if order_executor_global: # 再次检查以防万一
                        task = asyncio.create_task(
                            order_executor_global.execute_arbitrage_path(
                                path_id_prefix=opportunity['path_id'],
                                trades_sequence=opportunity['trades'],
                                opportunity_key=opportunity_prices_key
                            )
                        )
                        order_executor_global.active_arbitrage_tasks.add(task)
                        task.add_done_callback(order_executor_global.active_arbitrage_tasks.discard)
                    break
    else:
        current_logger.warning(f"MarketDataHandler (market_data_handler_global) 未初始化。原始公共数据: {json.dumps(message_data)[:200]}")

async def sample_private_callback(message_data):
    logger = logging.getLogger(__name__)
    if "arg" in message_data and "channel" in message_data["arg"]:
        channel = message_data["arg"]["channel"]
        if channel == "account":
            for detail_entry in message_data.get('data', []):
                for balance_detail in detail_entry.get('details', []):
                    ccy = balance_detail.get('ccy')
                    avail_bal_str = balance_detail.get('availBal', '0')
                    if ccy and avail_bal_str:
                        try:
                            avail_bal = Decimal(avail_bal_str)
                            if avail_bal > Decimal(0) or avail_bal_str == '0':
                                 logger.info(f"私有账户更新: {ccy} 可用余额: {avail_bal}")
                        except Exception as e:
                            logger.warning(f"无法将余额 '{avail_bal_str}' 转换为 Decimal (币种: {ccy}): {e}")
        elif channel == "orders":
            for order_data in message_data.get('data', []):
                logger.info(f"私有订单更新: instId={order_data.get('instId')}, ordId={order_data.get('ordId')}, "
                            f"clOrdId={order_data.get('clOrdId')}, state={order_data.get('state')}, "
                            f"fillSz={order_data.get('accFillSz')}, avgPx={order_data.get('avgPx')}")
    elif "event" in message_data:
        logger.info(f"私有频道事件: {message_data['event']} - Msg: {message_data.get('msg', '')} Code: {message_data.get('code')}")


if __name__ == '__main__':
    # 实例化 OKX 连接器
    connector = OKXConnector()
    main_logger = connector.logger

    # --- 1. 初始化 MarketDataHandler ---
    trading_pairs_from_settings = connector.settings.get('arbitrage', {}).get('trading_pairs', [])
    if not trading_pairs_from_settings:
        main_logger.critical("关键错误: 未在 settings.yaml 的 arbitrage.trading_pairs 中找到交易对配置。程序退出。")
        exit(1)

    market_data_handler_global = MarketDataHandler(
        trading_pairs=trading_pairs_from_settings,
        logger=main_logger
    )
    main_logger.info(f"MarketDataHandler 已为交易对全局初始化: {trading_pairs_from_settings}")

    # --- 2. 初始化 ArbitrageStrategy ---
    strategy_config = connector.settings
    arbitrage_strategy_global = ArbitrageStrategy(
        market_data_handler=market_data_handler_global,
        config=strategy_config,
        logger=main_logger
    )
    main_logger.info("ArbitrageStrategy 已全局初始化。")

    # --- 3. 初始化 OrderExecutor ---
    order_executor_global = OrderExecutor(
        okx_connector=connector,
        market_data_handler=market_data_handler_global,
        config=connector.settings,
        logger=main_logger
    )
    main_logger.info("OrderExecutor 已全局初始化。")

    main_logger.info("--- 开始 REST API 测试 (可以注释掉以专注于WebSocket) ---")
    balance_data = connector.get_account_balance()
    if balance_data and isinstance(balance_data, list) and balance_data: # 检查 balance_data 是否是列表且非空
        balance_details = balance_data[0].get('details', [])
        if balance_details: # 检查 details 是否存在且非空
            for b_info in balance_details:
                 if Decimal(b_info.get('availBal', 0)) > 0 or Decimal(b_info.get('frozenBal',0)) > 0 :
                      main_logger.info(f"余额 {b_info['ccy']}: 可用: {b_info['availBal']}, 冻结: {b_info['frozenBal']}")
        else:
            main_logger.warning("账户余额中未找到 'details' 或 'details' 为空。")
    elif balance_data and 'code' in balance_data and balance_data['code'] != '0':
        main_logger.error(f"获取账户余额失败。错误: {balance_data.get('msg')}")
    else:
        main_logger.warning("未返回余额数据或余额为空。")

    if trading_pairs_from_settings:
        first_pair = trading_pairs_from_settings[0]
        main_logger.info(f"\n获取 SPOT {first_pair} 产品详情:")
        spot_instruments = connector.get_instrument_details(instType='SPOT', instId=first_pair)
        if spot_instruments and isinstance(spot_instruments, list) and spot_instruments:
            main_logger.info(json.dumps(spot_instruments[0], indent=2))
        else:
            main_logger.error(f"获取 SPOT {first_pair} 产品详情失败或无数据。")

        main_logger.info(f"\n获取 SPOT {first_pair} 费率:")
        fee_spot = connector.get_fee_rates(instType='SPOT', instId=first_pair)
        if fee_spot and isinstance(fee_spot, list) and fee_spot:
            main_logger.info(json.dumps(fee_spot[0], indent=2))
        else:
            main_logger.error(f"获取 SPOT {first_pair} 费率失败或无数据。")

    main_logger.info("\n--- 开始 WebSocket 及套利执行测试 ---")

    async def run_websockets_test():
        if not market_data_handler_global or not arbitrage_strategy_global or not order_executor_global:
            main_logger.error("核心组件未初始化。无法运行WebSocket测试。")
            return

        tasks = []

        public_ws_channel_configs = connector.settings.get('websocket_channels', {}).get('public', [])
        all_public_subscriptions = []
        if public_ws_channel_configs:
            for pair_id in trading_pairs_from_settings:
                for channel_config_template in public_ws_channel_configs:
                    subscription_arg = channel_config_template.copy()
                    subscription_arg['instId'] = pair_id
                    all_public_subscriptions.append(subscription_arg)

            if all_public_subscriptions:
                main_logger.info(f"准备公共 WebSocket 订阅: {json.dumps(all_public_subscriptions)}")
                tasks.append(asyncio.create_task(
                    connector.public_websocket_handler(all_public_subscriptions, market_data_websocket_callback)
                ))
        else:
            main_logger.warning("在配置文件中未找到公共 WebSocket 频道配置。")

        if connector.simulated:
            private_ws_channel_configs = connector.settings.get('websocket_channels', {}).get('private', [])
            if private_ws_channel_configs:
                main_logger.info(f"准备私有 WebSocket 订阅: {json.dumps(private_ws_channel_configs)}")
                tasks.append(asyncio.create_task(
                    connector.private_websocket_handler(private_ws_channel_configs, sample_private_callback)
                ))

        if not tasks:
            main_logger.warning("未创建任何 WebSocket 任务。请检查配置。")
            return

        test_duration = connector.settings.get('other', {}).get('websocket_test_duration', 10)
        main_logger.info(f"运行 WebSocket 测试 {test_duration} 秒... 如果发现机会，OrderExecutor 将尝试交易。")

        start_time = time.time()
        try:
            while time.time() - start_time < test_duration:
                # 检查活动的套利任务
                # 使用副本进行迭代，因为集合可能在回调中被修改
                current_arb_tasks = list(order_executor_global.active_arbitrage_tasks)
                for task in current_arb_tasks:
                    if task.done():
                        try:
                            task.result() # 获取结果以暴露任何未处理的异常
                        except asyncio.CancelledError:
                            main_logger.info(f"套利任务 {getattr(task, 'get_name', lambda: id(task))()} 已被取消。")
                        except Exception as e_task:
                            main_logger.error(f"套利任务 {getattr(task, 'get_name', lambda: id(task))()} 执行出错: {e_task}", exc_info=True)
                        # 无论如何都从活动集合中移除已完成的任务
                        order_executor_global.active_arbitrage_tasks.discard(task)

                # 检查主WebSocket任务是否仍在运行
                all_main_ws_tasks_done = all(t.done() for t in tasks)
                if all_main_ws_tasks_done and not order_executor_global.active_arbitrage_tasks:
                    main_logger.info("所有主WebSocket任务和套利任务已完成，提前结束测试循环。")
                    break

                await asyncio.sleep(0.1)
            main_logger.info(f"WebSocket 测试时长 {test_duration} 秒已到。")

        except KeyboardInterrupt:
            main_logger.info("WebSocket 测试被用户中断。")
        except asyncio.CancelledError:
             main_logger.info("主 WebSocket 测试循环被取消。")
        finally:
            main_logger.info("正在取消所有活动的 WebSocket 和套利任务...")
            # 合并所有需要取消的任务
            all_tasks_to_cancel = tasks + list(order_executor_global.active_arbitrage_tasks)
            for task in all_tasks_to_cancel:
                if task and not task.done(): # 确保任务存在且未完成
                    task.cancel()

            if all_tasks_to_cancel:
                try:
                    # 等待所有任务完成取消或正常结束，设置超时
                    results = await asyncio.wait_for(asyncio.gather(*all_tasks_to_cancel, return_exceptions=True), timeout=10.0)
                    for i, result in enumerate(results):
                        task_ref = all_tasks_to_cancel[i]
                        task_name = getattr(task_ref, 'get_name', lambda: f"Task-{id(task_ref)}")()
                        if isinstance(result, asyncio.CancelledError):
                            main_logger.info(f"任务 {task_name} 已成功取消。")
                        elif isinstance(result, Exception):
                            main_logger.error(f"任务 {task_name} 收集时发现异常: {result}", exc_info=True)
                except asyncio.TimeoutError:
                    main_logger.warning("等待任务收集超时。部分任务可能仍在后台运行或未完全取消。")
                except Exception as e: # 捕获 gather 本身可能抛出的其他异常
                    main_logger.error(f"任务收集期间发生异常: {e}", exc_info=True)

            main_logger.info("WebSocket 任务处理完成。")

    # 从配置决定是否在启动时自动运行WebSocket测试
    if connector.settings.get('other', {}).get('run_websocket_tests_on_start', True):
        try:
            asyncio.run(run_websockets_test())
        except KeyboardInterrupt: # 捕获在 asyncio.run 级别的 Ctrl+C
            main_logger.info("程序被用户通过 Ctrl+C 终止。")
        except Exception as e: # 捕获 asyncio.run 中未处理的顶层异常
            main_logger.critical(f"asyncio.run 中发生未处理的致命错误: {e}", exc_info=True)
    else:
        main_logger.info("WebSocket 测试未配置为自动启动。 "
                         "如需启动，请在 settings.yaml 的 'other' 部分设置 'run_websocket_tests_on_start: true'。")

    main_logger.info("\n--- OKX Connector 测试脚本执行完毕 ---")


