# arb_bot/connectors/okx_conn.py
import yaml
import json
import asyncio
import websockets
from pathlib import Path

# --- Primary Import Section (for package execution) ---
try:
    from ..okx_sdk import Account_api as Account
    from ..okx_sdk import Public_api as Public
    from ..okx_sdk import Market_api as Market
    from ..okx_sdk import Trade_api as Trade
    # Assuming get_timestamp is also needed from okx_sdk.utils if not defined elsewhere
    from ..okx_sdk.utils import get_timestamp as sdk_get_timestamp
    from ..okx_sdk.exceptions import OkexAPIException
    from ..utils.logger import setup_logger
except ImportError as e:
    # --- Fallback Import Section (for potential standalone testing/troubleshooting) ---
    # This block is entered if the relative imports above fail.
    # This might happen if the script is run in a way that Python doesn't recognize its package context.
    print(f"Relative import failed: {e}. Attempting fallback imports for standalone execution.")
    import sys
    current_path = Path(__file__).resolve()
    # project_root is 'arb_bot'. Its parent is 'py-arb-bot'.
    project_root_dir_arb_bot = current_path.parent.parent
    # Add 'py-arb-bot' to sys.path so 'arb_bot.module' can be imported.
    py_arb_bot_dir = project_root_dir_arb_bot.parent
    if str(py_arb_bot_dir) not in sys.path:
        sys.path.insert(0, str(py_arb_bot_dir))
        print(f"Added to sys.path for fallback: {py_arb_bot_dir}")

    try:
        from arb_bot.okx_sdk import Account_api as Account
        from arb_bot.okx_sdk import Public_api as Public
        from arb_bot.okx_sdk import Market_api as Market
        from arb_bot.okx_sdk import Trade_api as Trade
        from arb_bot.okx_sdk.utils import get_timestamp as sdk_get_timestamp
        from arb_bot.okx_sdk.exceptions import OkexAPIException
        from arb_bot.utils.logger import setup_logger
        print("Fallback imports successful.")
    except ModuleNotFoundError as mnfe:
        print(f"Fallback ModuleNotFoundError: {mnfe}. Ensure 'py-arb-bot' is in PYTHONPATH or script is run as a module.")
        # Simplified logger for critical failure if setup_logger itself can't be imported
        import logging
        logging.basicConfig(level=logging.INFO)
        logger = logging.getLogger(__name__)
        logger.error(f"CRITICAL: Could not import necessary modules even with fallback: {mnfe}")
        # Re-raise or exit if these are critical
        raise mnfe
    except ImportError as ie_fallback:
        import logging
        logging.basicConfig(level=logging.INFO)
        logger = logging.getLogger(__name__)
        logger.error(f"CRITICAL: Fallback ImportError for setup_logger or SDK: {ie_fallback}")
        raise ie_fallback


class OKXConnector:
    def __init__(self, config_path_base="config"):
        # Initialize logger first
        # The setup_logger should now be available from one of the import blocks
        self.logger = setup_logger(__name__, "INFO", log_to_file=True)

        base_dir = Path(__file__).resolve().parent.parent # This is arb_bot directory

        self.settings_path = base_dir / config_path_base / "settings.yaml"
        self.secrets_path = base_dir / config_path_base / "secrets.yaml"

        self._load_config()
        self._initialize_clients()

    def _load_config(self):
        try:
            with open(self.settings_path, 'r') as f:
                self.settings = yaml.safe_load(f)
            self.logger.info(f"Settings loaded successfully from {self.settings_path}")
        except FileNotFoundError:
            self.logger.error(f"Settings file not found at {self.settings_path}")
            raise
        except yaml.YAMLError as e:
            self.logger.error(f"Error parsing settings YAML from {self.settings_path}: {e}")
            raise

        try:
            with open(self.secrets_path, 'r') as f:
                self.secrets = yaml.safe_load(f)
            self.logger.info(f"Secrets loaded successfully from {self.secrets_path}")
        except FileNotFoundError:
            self.logger.error(f"Secrets file not found at {self.secrets_path}")
            raise
        except yaml.YAMLError as e:
            self.logger.error(f"Error parsing secrets YAML from {self.secrets_path}: {e}")
            raise

        self.api_key = self.secrets['okx']['apikey']
        self.secret_key = self.secrets['okx']['secretkey']
        self.passphrase = self.secrets['okx']['passphrase']
        self.simulated = self.secrets['okx'].get('simulated', True)

        self.flag = '1' if self.simulated else '0'

        self.rest_endpoint = self.settings['api']['rest_endpoint']
        self.ws_public_url = self.settings['api']['ws_public']
        self.ws_private_url = self.settings['api']['ws_private']
        self.logger.info(f"Configuration loaded. Mode: {'Simulated' if self.simulated else 'Real Trading'}")


    def _initialize_clients(self):
        self.account_api = Account.AccountAPI(self.api_key, self.secret_key, self.passphrase, use_server_time=False, flag=self.flag)
        self.public_api = Public.PublicAPI(self.api_key, self.secret_key, self.passphrase, use_server_time=False, flag=self.flag)
        self.market_api = Market.MarketAPI(self.api_key, self.secret_key, self.passphrase, use_server_time=False, flag=self.flag)
        self.trade_api = Trade.TradeAPI(self.api_key, self.secret_key, self.passphrase, use_server_time=False, flag=self.flag)

        self.logger.info(f"OKX API Clients initialized.")

    def get_server_time_sdk(self):
        """Gets server time using the SDK's public API client."""
        try:
            # The PublicAPI client itself doesn't have a direct get_server_time method.
            # The SDK's client.py has _get_timestamp which calls /api/v5/public/time
            # We can use the public_api to call the same endpoint if needed, or rely on its internal use.
            # For an explicit call:
            result = self.public_api.get_system_time() # This uses the consts.SYSTEM_TIME
            if result.get('code') == '0' and result.get('data'):
                server_timestamp_str = result['data'][0]['ts']
                self.logger.info(f"OKX Server time (ms): {server_timestamp_str}")
                return server_timestamp_str
            else:
                self.logger.error(f"Error fetching server time: {result.get('msg')} (Code: {result.get('code')})")
                return None
        except OkexAPIException as e:
            self.logger.error(f"OkexAPIException fetching server time: {e}")
        except Exception as e:
            self.logger.error(f"Generic exception fetching server time: {e}")
        return None


    # --- REST API Methods ---
    def get_account_balance(self, ccy=None):
        try:
            if ccy:
                result = self.account_api.get_account(ccy=ccy)
            else:
                result = self.account_api.get_account()

            if result.get('code') == '0':
                self.logger.info(f"Account balance fetched successfully.")
                # self.logger.debug(f"Balance data: {json.dumps(result.get('data'))}")
                return result.get('data')
            else:
                self.logger.error(f"Error fetching account balance: {result.get('msg')} (Code: {result.get('code')}) - Details: {result}")
                return result
        except OkexAPIException as e:
            self.logger.error(f"OkexAPIException fetching account balance: {e}")
        except Exception as e:
            self.logger.error(f"Generic exception fetching account balance: {e}", exc_info=True)
        return None

    def get_instrument_details(self, instType, uly=None, instId=None):
        try:
            result = self.public_api.get_instruments(instType=instType, uly=uly, instId=instId)
            if result.get('code') == '0':
                self.logger.info(f"Instrument details for {instType} {instId or uly or ''} fetched: {len(result.get('data', []))} instruments.")
                return result.get('data')
            else:
                self.logger.error(f"Error fetching instrument details: {result.get('msg')} (Code: {result.get('code')})")
                return result
        except OkexAPIException as e:
            self.logger.error(f"OkexAPIException fetching instrument details: {e}")
        except Exception as e:
            self.logger.error(f"Generic exception fetching instrument details: {e}", exc_info=True)
        return None

    def get_fee_rates(self, instType, instId=None, uly=None, category=None):
        try:
            result = self.account_api.get_fee_rates(instType=instType, instId=instId, uly=uly, category=category)
            if result.get('code') == '0':
                self.logger.info(f"Fee rates for {instType} fetched successfully.")
                return result.get('data')
            else:
                self.logger.error(f"Error fetching fee rates: {result.get('msg')} (Code: {result.get('code')})")
                return result
        except OkexAPIException as e:
            self.logger.error(f"OkexAPIException fetching fee rates: {e}")
        except Exception as e:
            self.logger.error(f"Generic exception fetching fee rates: {e}", exc_info=True)
        return None

    # --- WebSocket Methods ---
    async def ws_login(self, ws):
        if not self.api_key or not self.secret_key or not self.passphrase:
            self.logger.warning("API credentials not found, cannot log in to private WebSocket.")
            return False

        server_time_ms_str = self.get_server_time_sdk()
        if not server_time_ms_str:
            self.logger.error("Failed to get server time for WebSocket login. Using local time as fallback (less reliable).")
            # Fallback to local time if server time fetch fails, though less ideal
            # sdk_get_timestamp() returns ISO format, we need epoch seconds for ws login
            import time
            timestamp_sec = str(int(time.time()))
        else:
            timestamp_sec = str(int(int(server_time_ms_str) / 1000))


        message = timestamp_sec + 'GET' + '/users/self/verify'

        import hmac
        import base64
        mac = hmac.new(bytes(self.secret_key, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
        sign = base64.b64encode(mac.digest()).decode("utf-8")

        login_param = {
            "op": "login",
            "args": [{"apiKey": self.api_key, "passphrase": self.passphrase, "timestamp": timestamp_sec, "sign": sign}]
        }
        await ws.send(json.dumps(login_param))
        self.logger.info(f"Login request sent to WebSocket with timestamp: {timestamp_sec}")

        try:
            response_raw = await asyncio.wait_for(ws.recv(), timeout=10)
            self.logger.info(f"WebSocket login response: {response_raw}")
            response_data = json.loads(response_raw)
            if response_data.get("event") == "login" and response_data.get("code") == "0": # success code is "0"
                self.logger.info("WebSocket login successful.")
                return True
            else:
                self.logger.error(f"WebSocket login failed: {response_data.get('msg')} (Code: {response_data.get('code')})")
                return False
        except asyncio.TimeoutError:
            self.logger.error("Timeout waiting for WebSocket login response.")
            return False
        except Exception as e:
            self.logger.error(f"Exception during WebSocket login recv: {e}", exc_info=True)
            return False


    async def subscribe_to_channels(self, ws, channels):
        sub_param = {"op": "subscribe", "args": channels}
        await ws.send(json.dumps(sub_param))
        self.logger.info(f"Subscription request sent for channels: {channels}")

        try:
            response_raw = await asyncio.wait_for(ws.recv(), timeout=10) # Wait for subscription ack
            self.logger.info(f"Subscription response: {response_raw}")
            response_data = json.loads(response_raw)
            if response_data.get("event") == "subscribe" and response_data.get("arg", {}).get("channel") == channels[0].get("channel"): # Basic check
                self.logger.info(f"Successfully subscribed to {channels[0].get('channel')}")
                return True
            elif response_data.get("event") == "error":
                 self.logger.error(f"Error subscribing to channels: {response_data.get('msg')} (Code: {response_data.get('code')})")
                 return False
            else: # Could be data already, or unexpected ack
                self.logger.warning(f"Unexpected subscription response or first data message: {response_raw[:200]}")
                # Assume success if no explicit error, but log it. The first message might be data.
                return True # Or pass the message to callback if it's data
        except asyncio.TimeoutError:
            self.logger.error("Timeout waiting for WebSocket subscription response.")
            return False
        except Exception as e:
            self.logger.error(f"Exception during WebSocket subscription recv: {e}", exc_info=True)
            return False


    async def _websocket_handler_loop(self, ws, callback, ws_type="Public"):
        while True:
            try:
                message_raw = await asyncio.wait_for(ws.recv(), timeout=30)
                if message_raw == 'pong':
                    self.logger.debug(f"{ws_type} WebSocket pong received.")
                    continue

                # Attempt to parse JSON, if it fails, it might be an unhandled simple string like 'pong' from other exchanges
                try:
                    message_data = json.loads(message_raw)
                except json.JSONDecodeError:
                    self.logger.warning(f"Received non-JSON message on {ws_type} WebSocket: {message_raw}")
                    continue # or handle as needed

                # Business logic callback
                await callback(message_data)

            except asyncio.TimeoutError:
                self.logger.debug(f"{ws_type} WebSocket recv timeout, sending ping.")
                try:
                    await ws.send('ping')
                except websockets.exceptions.ConnectionClosed:
                    self.logger.warning(f"{ws_type} WebSocket connection closed while sending ping.")
                    break
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.warning(f"{ws_type} WebSocket connection closed: {e.code} {e.reason}")
                break
            except Exception as e:
                self.logger.error(f"Error in {ws_type} WebSocket handler loop: {e}", exc_info=True)
                await asyncio.sleep(5) # Avoid rapid reconnection loops on persistent errors
                break # Or implement more sophisticated reconnection logic

    async def public_websocket_handler(self, channels, callback):
        while True: # Outer loop for reconnection
            try:
                async with websockets.connect(self.ws_public_url) as ws:
                    self.logger.info(f"Connected to public WebSocket: {self.ws_public_url}")
                    if await self.subscribe_to_channels(ws, channels):
                        await self._websocket_handler_loop(ws, callback, "Public")
                    else:
                        self.logger.error("Failed to subscribe to public channels. Retrying connection...")
            except ConnectionRefusedError:
                 self.logger.error(f"Public WebSocket connection refused. Retrying in 10s...")
            except Exception as e:
                self.logger.error(f"Exception in public_websocket_handler connect/subscribe: {e}", exc_info=True)

            self.logger.info("Public WebSocket disconnected. Attempting to reconnect in 10 seconds...")
            await asyncio.sleep(10)


    async def private_websocket_handler(self, channels, callback):
         while True: # Outer loop for reconnection
            try:
                async with websockets.connect(self.ws_private_url) as ws:
                    self.logger.info(f"Connected to private WebSocket: {self.ws_private_url}")
                    if await self.ws_login(ws):
                        if await self.subscribe_to_channels(ws, channels):
                            await self._websocket_handler_loop(ws, callback, "Private")
                        else:
                            self.logger.error("Failed to subscribe to private channels after login. Retrying connection...")
                    else:
                        self.logger.error("Failed to login to private WebSocket. Retrying connection...")
            except ConnectionRefusedError:
                 self.logger.error(f"Private WebSocket connection refused. Retrying in 10s...")
            except Exception as e:
                self.logger.error(f"Exception in private_websocket_handler connect/login/subscribe: {e}", exc_info=True)

            self.logger.info("Private WebSocket disconnected. Attempting to reconnect in 10 seconds...")
            await asyncio.sleep(10)


async def sample_public_callback(message):
    # This is a global logger instance for the module, if not using class logger
    # logger = logging.getLogger(__name__) # Or pass logger instance around
    # For simplicity, assume logger is accessible or use print
    # print(f"Public CB: {json.dumps(message)[:200]}")
    if "arg" in message and "channel" in message["arg"]:
        channel = message["arg"]["channel"]
        instId = message["arg"].get("instId", "N/A")
        action = message.get("action") # For books channel (snapshot/update)

        if channel == "tickers":
            # OKXConnector.logger.info(f"Ticker [{instId}]: {message['data'][0]['last']}")
            print(f"Ticker [{instId}]: {message['data'][0]['last']}")
        elif channel == "books":
            if action == "snapshot":
                # OKXConnector.logger.info(f"OrderBook Snapshot [{instId}]: {len(message['data'][0]['bids'])} bids, {len(message['data'][0]['asks'])} asks")
                print(f"OrderBook Snapshot [{instId}]: {len(message['data'][0]['bids'])} bids, {len(message['data'][0]['asks'])} asks")
            elif action == "update":
                # OKXConnector.logger.info(f"OrderBook Update [{instId}]")
                print(f"OrderBook Update [{instId}]")
        else:
            # OKXConnector.logger.info(f"Public Msg [{channel}]: {json.dumps(message)[:150]}")
            print(f"Public Msg [{channel}]: {json.dumps(message)[:150]}")
    elif "event" in message:
        # OKXConnector.logger.info(f"Event: {message['event']} - Msg: {message.get('msg', '')}")
        print(f"Event: {message['event']} - Msg: {message.get('msg', '')}")


async def sample_private_callback(message):
    # logger = logging.getLogger(__name__)
    # print(f"Private CB: {json.dumps(message)[:200]}")
    if "arg" in message and "channel" in message["arg"]:
        channel = message["arg"]["channel"]
        if channel == "account":
            # OKXConnector.logger.info(f"Account Update: {message['data'][0]['details'][0]['ccy']} Bal: {message['data'][0]['details'][0]['availBal']}")
            print(f"Account Update: {message['data'][0]['details'][0]['ccy']} Bal: {message['data'][0]['details'][0]['availBal']}")
        elif channel == "orders":
            # OKXConnector.logger.info(f"Order Update: {message['data'][0]['instId']} - {message['data'][0]['state']}")
            print(f"Order Update: {message['data'][0]['instId']} - {message['data'][0]['state']}")
        else:
            # OKXConnector.logger.info(f"Private Msg [{channel}]: {json.dumps(message)[:150]}")
            print(f"Private Msg [{channel}]: {json.dumps(message)[:150]}")
    elif "event" in message:
        # OKXConnector.logger.info(f"Event: {message['event']} - Msg: {message.get('msg', '')}")
        print(f"Event: {message['event']} - Msg: {message.get('msg', '')}")


if __name__ == '__main__':
    connector = OKXConnector()
    main_logger = connector.logger # Use the logger from the connector instance

    main_logger.info("--- Testing REST APIs ---")
    balance_data = connector.get_account_balance()
    if balance_data and isinstance(balance_data, list) and balance_data: # Check if list and not empty
        for b_info in balance_data[0].get('details', []):
             if float(b_info.get('availBal', 0)) > 0 or float(b_info.get('frozenBal',0)) > 0 :
                  main_logger.info(f"Balance for {b_info['ccy']}: Avail: {b_info['availBal']}, Frozen: {b_info['frozenBal']}")
    elif balance_data and 'code' in balance_data and balance_data['code'] != '0':
        main_logger.error(f"Failed to fetch account balance. Error: {balance_data.get('msg')}")
    else:
        main_logger.warning("No balance data returned or balance is empty.")

    main_logger.info("\nFetching SPOT BTC-USDT instrument details:")
    spot_instruments = connector.get_instrument_details(instType='SPOT', instId='BTC-USDT')
    if spot_instruments and isinstance(spot_instruments, list) and spot_instruments:
        main_logger.info(json.dumps(spot_instruments[0], indent=2))
    else:
        main_logger.error("Failed to fetch SPOT BTC-USDT instrument details or no data.")

    main_logger.info("\nFetching Fee Rates for SPOT BTC-USDT:")
    fee_spot = connector.get_fee_rates(instType='SPOT', instId='BTC-USDT')
    if fee_spot: # Assuming fee_spot is the data part or a dict with data
        main_logger.info(json.dumps(fee_spot, indent=2))
    else:
        main_logger.error("Failed to fetch SPOT fee rates or no data.")

    main_logger.info("\n--- Testing WebSocket (will run for a short period if uncommented) ---")

    async def run_websockets_test():
        tasks = []

        # Public WebSocket Test (Tickers)
        public_channels_ticker = [{"channel": "tickers", "instId": "BTC-USDT"}]
        tasks.append(asyncio.create_task(
            connector.public_websocket_handler(public_channels_ticker, sample_public_callback)
        ))

        # Public WebSocket Test (Order Book)
        public_channels_books = [{"channel": "books", "instId": "BTC-USDT-SWAP"}]
        tasks.append(asyncio.create_task(
             connector.public_websocket_handler(public_channels_books, sample_public_callback)
        ))

        # Private WebSocket Test (Account Balance)
        if connector.simulated: # Only run private if simulated, or if explicitly allowed for real
            private_channels_account = [{"channel": "account"}]
            tasks.append(asyncio.create_task(
                connector.private_websocket_handler(private_channels_account, sample_private_callback)
            ))

            private_channels_orders = [{"channel": "orders", "instType": "SPOT"}]
            tasks.append(asyncio.create_task(
                connector.private_websocket_handler(private_channels_orders, sample_private_callback)
            ))
        else:
            main_logger.info("Skipping private WebSocket tests as not in simulated mode.")


        try:
            await asyncio.sleep(30) # Run for 30 seconds
        except KeyboardInterrupt:
            main_logger.info("WebSocket test interrupted by user.")
        finally:
            main_logger.info("Cancelling WebSocket tasks...")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            main_logger.info("WebSocket tasks cancelled.")

    # To run WebSocket tests:
    asyncio.run(run_websockets_test())

    main_logger.info("\n--- OKX Connector Test Script Finished ---")
    main_logger.info("Uncomment 'asyncio.run(run_websockets_test())' at the end of the script to test WebSocket connections.")


