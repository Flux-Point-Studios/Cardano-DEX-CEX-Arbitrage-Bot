"""
Cardano DEX-CEX Arbitrage Bot

This bot performs arbitrage between decentralized and centralized exchanges for Cardano native tokens.
It monitors price differences and executes trades when profitable opportunities are found.

Known Issues and Limitations:
- Rate limiting: Both DEX and CEX APIs have rate limits that may affect operation frequency
- Network congestion: High blockchain congestion can delay transaction confirmations
- Liquidity dependency: Requires sufficient liquidity on both exchanges
- Price impact: Large trades may cause significant price impact, affecting profitability
- API downtime: Dependent on multiple external APIs being operational
- Balance requirements: Requires maintaining balance on both DEX and CEX
- Transaction fees: Network fees and exchange fees affect minimum profitable trade size
- State management: Bot state may need manual cleanup if process is killed unexpectedly
- Recovery: Manual intervention may be needed if a trading cycle is interrupted

Configuration:
    Environment variables required - see .env.example
    Network configuration - Mainnet/Testnet
    Trading parameters - Quantity, thresholds
"""

# arbitrage_bot.py

import os
import sys
import time
import asyncio
import requests
import json
import logging
import pid
import signal
from typing import Optional
from dotenv import load_dotenv
from urllib.parse import urlsplit
from base64 import b64encode
from hashlib import sha256
from hmac import HMAC
from pycardano import (
    Network, BlockFrostChainContext, PaymentSigningKey, StakeSigningKey,
    PaymentVerificationKey, VerificationKeyWitness, PaymentKeyPair, Address,
    TransactionBuilder, TransactionWitnessSet, TransactionOutput, Transaction, Value, MultiAsset,
    AssetName
)

# Load environment variables
load_dotenv()

# Network configuration
NETWORK_ID = 1  # 1 for mainnet, 0 for testnet/preview/preprod
NETWORK = Network.MAINNET if NETWORK_ID == 1 else Network.TESTNET

# Configure logging
if not os.path.exists('logs'):
    os.makedirs('logs')

logging.basicConfig(
    level=logging.INFO,
    filename='logs/bot.log',
    filemode='a',
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Environment Variables
MAESTRO_API_KEY = os.getenv('MAESTRO_API_KEY')
GLEEC_API_KEY = os.getenv('GLEEC_API_KEY')
GLEEC_SECRET_KEY = os.getenv('GLEEC_SECRET_KEY')
CARDANO_ADDRESS = os.getenv('CARDANO_ADDRESS')
SIGNING_KEY_JSON = os.getenv('SIGNING_KEY_JSON')
VERIFICATION_KEY_JSON = os.getenv('VERIFICATION_KEY_JSON')
BLOCKFROST_PROJECT_ID = os.getenv('BLOCKFROST_PROJECT_ID')
STAKE_SIGNING_KEY_JSON = os.getenv('STAKE_SIGNING_KEY_JSON')
STAKE_VERIFICATION_KEY_JSON = os.getenv('STAKE_VERIFICATION_KEY_JSON')

if None in [SIGNING_KEY_JSON, VERIFICATION_KEY_JSON, STAKE_SIGNING_KEY_JSON, STAKE_VERIFICATION_KEY_JSON]:
    raise ValueError("Missing required signing key environment variables")

# Token IDs
ADA_TOKEN_ID = 'ADA'  # ADA token ID as per DexHunter API
TOKEN_POLICY_ID = 'ea153b5d4864af15a1079a94a0e2486d6376fa28aafad272d15b243a'
TOKEN_ASSET_NAME = '0014df10536861726473'  # Hex-encoded asset name
TOKEN_ID = f"{TOKEN_POLICY_ID}{TOKEN_ASSET_NAME}"  # Concatenate without dot

# Trade quantity (configurable via environment variable)
TRADE_QUANTITY = int(os.getenv('TRADE_QUANTITY', '500'))  # Default to 100 if not set

# State file for tracking bot state
STATE_FILE = 'bot_state.json'

def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def verify_environment():
    """Verify all required environment variables and keys are properly set."""
    try:
        # Test that keys can be loaded
        PaymentSigningKey.from_json(SIGNING_KEY_JSON)
        StakeSigningKey.from_json(STAKE_SIGNING_KEY_JSON)
        
        # Verify address has stake component
        address = Address.from_primitive(CARDANO_ADDRESS)
        if not hasattr(address, 'staking_part') or not address.staking_part:
            logging.warning("Address does not have stake component")
            
        return True
    except Exception as e:
        logging.error(f"Environment verification failed: {e}")
        return False

# DEXHunter API Base URL
DEXHUNTER_API_BASE_URL = os.getenv('DEXHUNTER_API_BASE_URL', 'https://api-us.dexhunterv3.app')

# Arbitrage Threshold (Percentage)
ARBITRAGE_THRESHOLD = float(os.getenv('ARBITRAGE_THRESHOLD', '1.0'))  # Default to 1.0%

class BotState:
    def __init__(self, state_file='bot_state.json'):
        self.state_file = state_file
        self.state = self.load_state()
        
    def load_state(self):
        try:
            with open(self.state_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {
                'last_trade_time': 0,
                'active_orders': {},
                'pending_transfers': {},
                'active_withdrawals': {},
                'completed_transactions': []
            }
            
    def save_state(self):
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)
            
    def update_order(self, order_id, status, details):
        self.state['active_orders'][order_id] = {
            'status': status,
            'details': details,
            'timestamp': time.time()
        }
        self.save_state()
        
    def update_transfer(self, tx_id, status, details):
        self.state['pending_transfers'][tx_id] = {
            'status': status,
            'details': details,
            'timestamp': time.time()
        }
        self.save_state()
        
    def update_withdrawal(self, withdrawal_id, status, details):
        self.state['active_withdrawals'][withdrawal_id] = {
            'status': status,
            'details': details,
            'timestamp': time.time()
        }
        self.save_state()
        
    def complete_transaction(self, tx_hash, tx_type='unknown'):
        """Complete a transaction with type tracking."""
        self.state['completed_transactions'].append({
            'tx_hash': tx_hash,
            'type': tx_type,
            'timestamp': time.time(),
            'dex_sell_completed': False
        })
        self.save_state()
        
    def get_pending_operations(self):
        return {
            'orders': {k: v for k, v in self.state['active_orders'].items() 
                      if v['status'] not in ['completed', 'failed']},
            'transfers': {k: v for k, v in self.state['pending_transfers'].items()
                         if v['status'] not in ['completed', 'failed']},
            'withdrawals': {k: v for k, v in self.state['active_withdrawals'].items()
                          if v['status'] not in ['completed', 'failed']}
        }

# Initialize state manager
state_manager = BotState()



class GleecAuth(requests.auth.AuthBase):
    def __init__(self, api_key, secret_key, window=10000):
        self.api_key = api_key
        self.secret_key = secret_key
        self.window = str(window)

    def __call__(self, request):
        try:
            url = urlsplit(request.url)
            message = [request.method.upper(), url.path]
            if url.query:
                message.append('?')
                message.append(url.query)
            if request.body:
                message.append(
                    request.body.decode() if isinstance(request.body, bytes) else request.body
                )

            timestamp = str(int(time.time() * 1000))
            message.append(timestamp)
            message.append(self.window)

            message_str = ''.join(message)
            signature = HMAC(
                key=self.secret_key.encode(),
                msg=message_str.encode(),
                digestmod=sha256
            ).hexdigest()
            auth_str = ':'.join([self.api_key, signature, timestamp, self.window])
            auth_header = 'HS256 ' + b64encode(auth_str.encode()).decode()
            request.headers['Authorization'] = auth_header
            return request
        except Exception as e:
            logging.error(f"Error in GleecAuth: {e}")
            raise

def get_average_price(token_in_id, token_out_id):
    """Fetch the average price from DEX Hunter API."""
    url = f"{DEXHUNTER_API_BASE_URL}/swap/averagePrice/{token_in_id}/{token_out_id}/"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        average_price = data.get('averagePrice')
        if average_price is None:
            logging.error(f"No averagePrice found in response: {data}")
            return None
        return float(average_price)
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"HTTP error in get_average_price: {http_err}")
        logging.error(f"Response content: {response.text}")
    except Exception as e:
        logging.error(f"Exception in get_average_price: {e}")
    return None

def get_cex_shards_price_usdt():
    """Fetch the SHARDS price in USDT from Gleec CEX."""
    url = "https://api.exchange.gleec.com/api/3/public/ticker/SHARDSUSDT"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        price_usdt = float(data['last'])
        return price_usdt
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"HTTP error in get_cex_shards_price_usdt: {http_err}")
    except Exception as e:
        logging.error(f"Exception in get_cex_shards_price_usdt: {e}")
    return None

def get_ada_price_usdt():
    """Fetch the ADA price in USDT from Gleec CEX."""
    url = "https://api.exchange.gleec.com/api/3/public/price/ticker/ADAUSDT"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        price_usdt = float(data['price'])
        return price_usdt
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"HTTP error in get_ada_price_usdt: {http_err}")
        logging.error(response.json())
    except Exception as e:
        logging.error(f"Exception in get_ada_price_usdt: {e}")
    return None

def calculate_shards_prices():
    """Calculate SHARDS price in ADA using CEX prices."""
    shards_price_usdt = get_cex_shards_price_usdt()
    ada_price_usdt = get_ada_price_usdt()
    if shards_price_usdt is not None and ada_price_usdt is not None:
        shards_price_ada = shards_price_usdt / ada_price_usdt
        return {
            'shards_price_ada': shards_price_ada,
            'shards_price_usdt': shards_price_usdt
        }
    else:
        logging.error("Could not retrieve necessary price data for SHARDS or ADA.")
        return None

async def check_cardano_balance():
    """Check if we have SHARDS on Cardano that need to be sold."""
    try:
        url = f"https://cardano-mainnet.blockfrost.io/api/v0/addresses/{CARDANO_ADDRESS}/assets"
        headers = {"project_id": BLOCKFROST_PROJECT_ID}
        
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            assets = response.json()
            for asset in assets:
                if asset.get('unit') == f"{TOKEN_POLICY_ID}{TOKEN_ASSET_NAME}":
                    quantity = int(asset.get('quantity', 0))
                    if quantity >= TRADE_QUANTITY:
                        logging.info(f"Found {quantity} SHARDS on Cardano address - executing DEX sell")
                        return True, quantity
            
        return False, 0
        
    except Exception as e:
        logging.error(f"Error checking Cardano balance: {e}")
        return False, 0

async def check_arbitrage_opportunity():
    """
    Monitor and detect arbitrage opportunities between DEX and CEX.
    
    Calculates price differences between exchanges and initiates trades when
    the difference exceeds the configured threshold.
    
    Returns:
        None
    
    Side Effects:
        - Initiates trades if opportunity is found
        - Updates bot state
        - Logs trading decisions and errors
    
    Known Issues:
        - Price may change between check and execution
        - Network latency can affect timing
        - Requires sufficient liquidity on both exchanges
    """
    try:
        # Fetch DEX price for swapping ADA to TOKEN
        dex_price_shards_per_ada = get_average_price(ADA_TOKEN_ID, TOKEN_ID)
        if dex_price_shards_per_ada is None or dex_price_shards_per_ada == 0:
            logging.error("Failed to get DEX price or price is zero.")
            return
        # Invert the dex price to get ADA per TOKEN
        dex_price_ada_per_shards = 1 / dex_price_shards_per_ada

        # Fetch CEX price data
        cex_prices = calculate_shards_prices()

        if dex_price_ada_per_shards is not None and cex_prices is not None:
            cex_price_ada = cex_prices['shards_price_ada']
            cex_price_usdt = cex_prices['shards_price_usdt']
            logging.info(f"DEX Price (ADA per SHARDS): {dex_price_ada_per_shards:.6f} ADA per SHARDS")
            logging.info(f"CEX Price (ADA per SHARDS): {cex_price_ada:.6f} ADA per SHARDS")

            price_difference = ((cex_price_ada - dex_price_ada_per_shards) / dex_price_ada_per_shards) * 100
            logging.info(f"Price Difference: {price_difference:.2f}%")

            if price_difference > ARBITRAGE_THRESHOLD:
                logging.info("Arbitrage Opportunity: Buy on DEX, Sell on CEX")
                await execute_trade('buy_dex_sell_cex', dex_price_ada_per_shards, cex_price_usdt)
            elif price_difference < -ARBITRAGE_THRESHOLD:
                logging.info("Arbitrage Opportunity: Buy on CEX, Sell on DEX")
                await execute_trade('buy_cex_sell_dex', dex_price_ada_per_shards, cex_price_usdt)
            else:
                logging.info("No significant arbitrage opportunity.")
        else:
            logging.error("Price data not available.")
    except Exception as e:
        logging.error(f"Exception in check_arbitrage_opportunity: {e}")
        

def get_order_book_depth(symbol):
    """Get order book depth for a trading pair."""
    url = f"https://api.exchange.gleec.com/api/3/public/orderbook/{symbol}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        return data
    except Exception as e:
        logging.error(f"Error getting order book: {e}")
        return None

def check_liquidity(symbol, side, quantity, price):
    """
    Check if there's sufficient liquidity for a trade.
    
    Args:
        symbol (str): Trading pair symbol
        side (str): 'buy' or 'sell'
        quantity (float): Amount to trade
        price (float): Target price
    
    Returns:
        bool: True if sufficient liquidity exists, False otherwise
    
    Known Issues:
        - Order book can change between check and execution
        - Does not account for hidden orders
        - May not detect synthetic liquidity
    """
    order_book = get_order_book_depth(symbol)
    if not order_book:
        return False
        
    side_key = 'ask' if side == 'buy' else 'bid'
    if side_key not in order_book:
        logging.error(f"Invalid order book data - missing {side_key}")
        return False
        
    orders = order_book[side_key]
    available_volume = 0
    
    try:
        for order in orders:
            order_price = float(order[0])
            order_quantity = float(order[1])
            
            # For buy orders, we need prices less than or equal to our target
            # For sell orders, we need prices greater than or equal to our target
            if (side == 'buy' and order_price <= price) or \
               (side == 'sell' and order_price >= price):
                available_volume += order_quantity
                if available_volume >= quantity:
                    logging.info(f"Found sufficient liquidity for {side} {quantity} {symbol} at price {price}")
                    return True
                    
        logging.warning(f"Insufficient liquidity for {side} {quantity} {symbol} at price {price}. Available: {available_volume}")
        return False
        
    except (IndexError, ValueError) as e:
        logging.error(f"Error parsing order book data: {e}")
        return False

def create_liquidity(symbol, quantity, price):
    """Create a limit order to provide liquidity."""
    url = 'https://api.exchange.gleec.com/api/3/spot/order'
    
    data = {
        'symbol': symbol,
        'side': 'sell',  # We provide liquidity on the sell side
        'quantity': str(quantity),
        'type': 'limit',
        'timeInForce': 'GTC',
        'price': str(price),
        'post_only': True  # Ensure we're providing not taking liquidity
    }

    session = requests.Session()
    session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
    
    try:
        response = session.post(url, json=data)
        response.raise_for_status()
        order_data = response.json()
        
        if 'error' in order_data:
            logging.error(f"Error creating liquidity: {order_data['error']}")
            return None
            
        logging.info(f"Created liquidity order: {order_data}")
        return order_data
        
    except Exception as e:
        logging.error(f"Error creating liquidity: {e}")
        if hasattr(e, 'response'):
            logging.error(f"Response: {e.response.text}")
        return None


async def execute_trade(direction, dex_price, cex_price_usdt):
    """
    Execute an arbitrage trade in the specified direction.
    
    Args:
        direction (str): Either 'buy_cex_sell_dex' or 'buy_dex_sell_cex'
        dex_price (float): Current price on DEX
        cex_price_usdt (float): Current price on CEX in USDT
    
    Returns:
        bool: True if trade cycle completed successfully, False otherwise
    
    Side Effects:
        - Places orders on exchanges
        - Transfers tokens between exchanges
        - Updates bot state
        - Logs trade execution details
    
    Known Issues:
        - Partial fills not handled
        - No automatic retry on failed transfers
        - Manual intervention needed if cycle interrupted
    """
    try:
        quantity = TRADE_QUANTITY
        
        await check_pending_operations()
        
        pending_ops = state_manager.get_pending_operations()
        if any(pending_ops.values()):
            logging.info("Found pending operations, completing those first...")
            return False
                
        if direction == 'buy_cex_sell_dex':
            # First part: Buy on CEX
            usdt_price = cex_price_usdt
            
            # Check liquidity before placing order
            if not check_liquidity('SHARDSUSDT', 'buy', quantity, usdt_price):
                logging.warning(f"Insufficient liquidity to buy {quantity} SHARDS at {usdt_price} USDT")
                if create_liquidity('SHARDSUSDT', quantity * 2, usdt_price * 0.99):
                    logging.info("Created liquidity order, waiting for fills...")
                    await asyncio.sleep(30)
                else:
                    return False
            
            # Execute CEX buy
            order = create_new_order('SHARDSUSDT', 'buy', quantity, usdt_price)
            if not order:
                logging.error("Failed to place CEX order.")
                return False

            order_id = order['id']
            state_manager.update_order(order_id, 'pending', order)
            
            # Wait for order fill
            order_filled = monitor_order_status(order_id, timeout=600)
            if not order_filled:
                logging.error("Failed to execute buy order on CEX.")
                return False

            # Check balance and withdraw
            balance = get_wallet_balance('SHARDS')
            if not balance or balance['available'] < quantity:
                logging.error("Insufficient SHARDS balance for withdrawal")
                return False

            # Initiate and wait for withdrawal
            withdrawal_success = withdraw_shards_to_cardano(quantity)
            if not withdrawal_success:
                logging.error("Withdrawal to Cardano failed")
                return False

            # Second part: Sell on DEX
            logging.info("Starting DEX sell portion of the trade...")
            dex_success = execute_trade_on_dex(quantity, sell=True)  # Note: sell=True here
            if dex_success:
                logging.info("Successfully completed full buy_cex_sell_dex cycle")
                return True
            else:
                logging.error("Failed to execute DEX sell")
                return False
                    
        elif direction == 'buy_dex_sell_cex':
            # Check liquidity on CEX for selling
            if not check_liquidity('SHARDSUSDT', 'sell', quantity, cex_price_usdt):
                logging.warning(f"Insufficient liquidity to sell {quantity} SHARDS at {cex_price_usdt} USDT")
                # Try to provide liquidity
                if create_liquidity('SHARDSUSDT', quantity * 2, cex_price_usdt * 1.01):  # Slightly lower price
                    logging.info("Created liquidity order, waiting for fills...")
                    time.sleep(30)  # Wait for potential fills
                else:
                    return False
                    
            # Execute DEX purchase first
            dex_success = execute_trade_on_dex(quantity, sell=False)
            if dex_success:
                # If DEX trade succeeds, transfer to CEX
                transfer_success = transfer_shards_to_gleec(quantity)
                if transfer_success:
                    # Execute sell on CEX
                    cex_success = execute_trade_on_cex('sell', quantity, cex_price_usdt)
                    if cex_success:
                        logging.info("Completed DEX buy and CEX sell")
                        return True
            return False

        return False
        
    except Exception as e:
        logging.error(f"Exception in execute_trade: {e}", exc_info=True)
        return False

def create_new_order(symbol, side, quantity, price=None, order_type='limit', time_in_force='GTC'):
    """Create new order with improved error handling."""
    url = 'https://api.exchange.gleec.com/api/3/spot/order'
    
    # Generate unique client_order_id
    client_order_id = f"bot_{int(time.time()*1000)}"
    
    data = {
        'symbol': symbol,
        'side': side,
        'quantity': str(quantity),
        'type': order_type,
        'timeInForce': time_in_force,
        'client_order_id': client_order_id
    }
    if price:
        data['price'] = str(price)

    session = requests.Session()
    session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
    
    try:
        # Check liquidity one final time before placing order
        if not check_liquidity(symbol, side, quantity, price):
            logging.error("Final liquidity check failed before order placement")
            return None
            
        response = session.post(url, json=data)
        response.raise_for_status()
        order_data = response.json()
        
        if 'id' not in order_data:
            logging.error(f"No order ID in response: {order_data}")
            return None
            
        # Store both IDs for tracking
        order_data['client_order_id'] = client_order_id
        logging.info(f"Order created: ID={order_data['id']}, ClientID={client_order_id}")
        return order_data
        
    except Exception as e:
        logging.error(f"Error creating order: {e}")
        if hasattr(e, 'response'):
            logging.error(f"Response: {e.response.text}")
        return None

def check_order_status(order_id):
    """Check current status of a CEX order."""
    # Get order details from state manager to access symbol
    order_details = state_manager.state['active_orders'].get(order_id, {}).get('details', {})
    symbol = order_details.get('symbol')
    
    if not symbol:
        logging.error(f"Cannot check order {order_id}: Missing symbol in order details")
        return 'error'

    # First try active orders
    url = f"https://api.exchange.gleec.com/api/3/spot/order/{order_id}"
    session = requests.Session()
    session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
    
    try:
        response = session.get(url)
        if response.status_code == 200:
            order_data = response.json()
            return order_data.get('status')
        
        # If not found in active orders, check history
        return check_order_history(order_id, symbol)  # Pass symbol to the function
            
    except Exception as e:
        logging.error(f"Error checking order status: {e}")
        return 'error'

def check_order_history(order_id, symbol):
    """Check order status in order history with symbol parameter."""
    url = "https://api.exchange.gleec.com/api/3/spot/history/order"
    params = {
        'symbol': symbol,
        'limit': 100
    }
    
    session = requests.Session()
    session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
    
    try:
        response = session.get(url, params=params)
        response.raise_for_status()
        orders = response.json()
        
        # Look for our order in the history
        for order in orders:
            if str(order['id']) == str(order_id):
                return order['status']
                
        return 'not_found'
        
    except Exception as e:
        logging.error(f"Error checking order history: {e}")
        return 'error'

def monitor_order_status(order_id, timeout=300):
    """Monitor order status with improved retry logic and timeout for 'new' status."""
    start_time = time.time()
    retry_delay = 2  # Initial delay in seconds
    max_retries = 30
    attempts = 0
    new_status_timeout = 60  # Timeout specifically for 'new' status (1 minute)
    new_status_start = time.time()
    
    # Get order details from state manager to access symbol
    order_details = state_manager.state['active_orders'].get(order_id, {}).get('details', {})
    symbol = order_details.get('symbol')
    
    if not symbol:
        logging.error(f"Cannot monitor order {order_id}: Missing symbol in order details")
        return False
        
    first_status_check = True

    while time.time() - start_time < timeout and attempts < max_retries:
        try:
            status = check_order_history(order_id, symbol)
            logging.info(f"Order {order_id} status: {status}")
            
            if status == 'filled':
                return True
            elif status in ['cancelled', 'expired', 'error']:
                return False
            elif status == 'new':
                # Only start the new status timer after first confirmation of new status
                if first_status_check:
                    new_status_start = time.time()
                    first_status_check = False
                # Check if we've been in 'new' status too long    
                elif time.time() - new_status_start > new_status_timeout:
                    logging.warning(f"Order {order_id} stuck in 'new' status for too long, cancelling...")
                    if cancel_order(order_id, symbol):
                        return False
            elif status == 'not_found' and not first_status_check:
                # Only consider not_found as an error after first successful status check
                logging.error(f"Order {order_id} not found after being created")
                return False
                
            # Exponential backoff with max delay of 10 seconds
            retry_delay = min(retry_delay * 1.5, 10)
            time.sleep(retry_delay)
            attempts += 1
            
        except Exception as e:
            logging.error(f"Error monitoring order {order_id}: {e}")
            time.sleep(retry_delay)
            attempts += 1

    logging.error(f"Order {order_id} monitoring timed out after {attempts} attempts")
    # Try to cancel the order before giving up
    cancel_order(order_id, symbol)
    return False

def cancel_order(order_id, symbol):
    """Cancel an active order."""
    url = f"https://api.exchange.gleec.com/api/3/spot/order/{order_id}/{symbol}"  # Include symbol in URL
    session = requests.Session()
    session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
    
    try:
        response = session.delete(url)
        response.raise_for_status()
        result = response.json()
        if result.get('status') == 'canceled':
            logging.info(f"Successfully cancelled order {order_id}")
            state_manager.update_order(order_id, 'cancelled', {
                'status': 'cancelled',
                'timestamp': time.time()
            })
            return True
        else:
            logging.error(f"Unexpected response when cancelling order: {result}")
            return False
    except Exception as e:
        logging.error(f"Failed to cancel order {order_id}: {e}")
        if hasattr(e, 'response'):
            logging.error(f"Response: {e.response.text}")
        return False

def handle_pending_order(order_id, order):
    """Handle a pending CEX order."""
    # Get symbol from order details
    symbol = order.get('details', {}).get('symbol')
    if not symbol:
        logging.error(f"Cannot handle order {order_id}: Missing symbol in order details")
        return False
        
    status = check_order_status(order_id)  # Symbol will be retrieved from state
    
    if status == 'not_found':
        # Order doesn't exist - mark as failed and clean up
        logging.warning(f"Order {order_id} not found - marking as failed")
        state_manager.update_order(order_id, 'failed', {
            'status': 'not_found',
            'timestamp': time.time()
        })
        return False
        
    elif status == 'filled':
        state_manager.update_order(order_id, 'completed', {
            'status': 'filled',
            'timestamp': time.time()
        })
        return True
        
    elif status in ['canceled', 'expired', 'error']:
        state_manager.update_order(order_id, 'failed', {
            'status': status,
            'timestamp': time.time()
        })
        return False
        
    return None

def handle_pending_transfer(tx_id, transfer):
    """Handle a pending transfer."""
    status = check_transfer_status(tx_id)
    if status == 'confirmed':
        state_manager.update_transfer(tx_id, 'completed', {'status': 'confirmed'})
    elif status == 'failed':
        state_manager.update_transfer(tx_id, 'failed', {'status': 'failed'})


def check_withdrawal_status(withdrawal_id):
    """Check the status of a withdrawal using the Gleec API."""
    try:
        url = f"https://api.exchange.gleec.com/api/3/wallet/transactions/{withdrawal_id}"
        session = requests.Session()
        session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
        response = session.get(url)
        response.raise_for_status()
        transaction = response.json()
        if transaction:
            return transaction['status']
        return 'UNKNOWN'
    except Exception as e:
        logging.error(f"Error checking withdrawal status: {e}")
        return 'ERROR'

def handle_pending_withdrawal(withdrawal_id):
    """Handle a pending withdrawal with improved state cleanup and tracking."""
    try:
        status = check_withdrawal_status(withdrawal_id)
        logging.info(f"Withdrawal {withdrawal_id} status: {status}")
        
        if status == 'SUCCESS':
            state_manager.update_withdrawal(withdrawal_id, 'completed', {'status': 'SUCCESS'})
            state_manager.complete_transaction(withdrawal_id, tx_type='withdrawal')
            if withdrawal_id in state_manager.state['active_withdrawals']:
                del state_manager.state['active_withdrawals'][withdrawal_id]
            state_manager.save_state()
            return True
        elif status in ['FAILED', 'ROLLED_BACK']:
            state_manager.update_withdrawal(withdrawal_id, 'failed', {'status': status})
            if withdrawal_id in state_manager.state['active_withdrawals']:
                del state_manager.state['active_withdrawals'][withdrawal_id]
            state_manager.save_state()
            return False
            
        return None
        
    except Exception as e:
        logging.error(f"Error handling withdrawal {withdrawal_id}: {e}")
        return False


def execute_trade_on_cex(side, quantity, price=None):
    """Execute a trade on the CEX."""
    try:
        symbol = 'SHARDSUSDT'
        if price is None:
            # Place a market order
            order = create_new_order(symbol, side, quantity, order_type='market')
        else:
            # Place a limit order
            order = create_new_order(symbol, side, quantity, price)
        if order:
            logging.info(f"CEX Order placed: {order}")
            return True
        else:
            logging.error("Failed to place CEX order.")
            return False
    except Exception as e:
        logging.error(f"Exception in execute_trade_on_cex: {e}", exc_info=True)
        return False

def create_new_order(symbol, side, quantity, price=None, order_type='limit', time_in_force='GTC'):
    """Create new order with client_order_id tracking."""
    url = 'https://api.exchange.gleec.com/api/3/spot/order'
    
    # Generate unique client_order_id
    client_order_id = f"bot_{int(time.time()*1000)}"
    
    data = {
        'symbol': symbol,
        'side': side,
        'quantity': str(quantity),
        'type': order_type,
        'timeInForce': time_in_force,
        'client_order_id': client_order_id
    }
    if price:
        data['price'] = str(price)

    session = requests.Session()
    session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
    
    try:
        response = session.post(url, json=data)
        response.raise_for_status()
        order_data = response.json()
        
        if 'id' not in order_data:
            logging.error(f"No order ID in response: {order_data}")
            return None
            
        # Store both IDs for tracking
        order_data['client_order_id'] = client_order_id
        logging.info(f"Order created: ID={order_data['id']}, ClientID={client_order_id}")
        return order_data
        
    except Exception as e:
        logging.error(f"Error creating order: {e}")
        if hasattr(e, 'response'):
            logging.error(f"Response: {e.response.text}")
        return None

def estimate_swap(amount_in, token_in_id, token_out_id):
    """Estimate the swap on the DEX using DEX Hunter API."""
    url = f"{DEXHUNTER_API_BASE_URL}/swap/estimate"
    payload = {
        "amount_in": amount_in,
        "token_in": token_in_id,
        "token_out": token_out_id,
        "slippage": 2,
        # "single_preferred_dex": "minswap",  # Optional
        # "blacklisted_dexes": ["string"],    # Optional
    }
    try:
        headers = {
            "Content-Type": "application/json",
            "accept": "application/json",
        }
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data  # estimation details
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"HTTP error in estimate_swap: {http_err}")
        logging.error(f"Response content: {response.text}")
    except Exception as e:
        logging.error(f"Exception in estimate_swap: {e}", exc_info=True)
    return None

def execute_trade_on_dex(quantity, sell=False):
    """Execute a trade on the DEX with improved error handling and logging."""
    try:
        amount_in = float(quantity)
        
        if (sell):
            token_in_id = TOKEN_ID
            token_out_id = ""  # Empty string for ADA
            logging.info(f"Selling {amount_in} SHARDS for ADA")
        else:
            token_in_id = ""  # Empty string for ADA
            token_out_id = TOKEN_ID
            logging.info(f"Buying SHARDS with {amount_in} ADA")

        # Estimate the swap
        estimate = estimate_swap(amount_in, token_in_id, token_out_id)
        if not estimate:
            logging.error("Swap estimation failed")
            return False

        logging.info(f"Swap estimate received: {json.dumps(estimate, indent=2)}")

        # Create the swap transaction with proper direction
        if sell:
            tx_cbor = create_swap_transaction(amount_in, CARDANO_ADDRESS, token_in=TOKEN_ID, token_out="")
        else:
            tx_cbor = create_swap_transaction(amount_in, CARDANO_ADDRESS, token_in="", token_out=TOKEN_ID)
            
        if not tx_cbor:
            logging.error("Failed to create swap transaction")
            return False

        # Sign the transaction using DexHunter's signing process
        try:
            signed_tx_cbor = sign_with_dexhunter(tx_cbor)
            if not signed_tx_cbor:
                logging.error("Failed to sign transaction")
                return False
                
            logging.info("Transaction signed successfully")
            
        except Exception as sign_error:
            logging.error(f"Error during transaction signing: {sign_error}", exc_info=True)
            return False

        # Submit the transaction
        try:
            tx_hash = submit_transaction(signed_tx_cbor)
            if tx_hash:
                logging.info(f"Trade executed successfully. Transaction hash: {tx_hash}")
                
                # Monitor transaction status
                status = monitor_transaction_status(tx_hash)
                if status == "confirmed":
                    logging.info(f"Transaction {tx_hash} confirmed")
                    # Update state after successful DEX trade
                    state_manager.complete_transaction(tx_hash)
                    return True
                else:
                    logging.error(f"Transaction {tx_hash} failed with status: {status}")
                    return False
                    
            else:
                logging.error("Transaction submission failed")
                return False
                
        except Exception as submit_error:
            logging.error(f"Error during transaction submission: {submit_error}", exc_info=True)
            return False

    except Exception as e:
        logging.error(f"Exception in execute_trade_on_dex: {e}", exc_info=True)
        return False

def monitor_transaction_status(tx_hash, timeout=300):
    """
    Monitor the status of a blockchain transaction.
    
    Args:
        tx_hash (str): Transaction hash to monitor
        timeout (int): Maximum time to wait in seconds
    
    Returns:
        str: Transaction status ('confirmed', 'timeout', or 'error')
    
    Known Issues:
        - Network congestion can cause timeouts
        - Reorgs not handled
        - Confirmation count may be insufficient for large amounts
    """
    url = f"https://cardano-mainnet.blockfrost.io/api/v0/txs/{tx_hash}"
    headers = {"project_id": BLOCKFROST_PROJECT_ID}
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                tx_data = response.json()
                if tx_data.get("block_height"):
                    return "confirmed"
                    
            elif response.status_code != 404: # 404 is expected for pending transactions
                logging.error(f"Error checking transaction status: {response.text}")
                return "error"
                
            time.sleep(5)  # Wait 5 seconds before checking again
            
        except Exception as e:
            logging.error(f"Error monitoring transaction: {e}", exc_info=True)
            return "error"
            
    return "timeout"

def create_swap_transaction(amount_in, buyer_address, token_in="", token_out=TOKEN_ID):
    """Create the swap transaction using DEX Hunter API."""
    url = f"{DEXHUNTER_API_BASE_URL}/swap/build"

    payload = {
        "buyer_address": buyer_address,
        "token_in": token_in,
        "token_out": token_out,
        "slippage": 2,
        "amount_in": amount_in
    }

    logging.debug(f"Request payload: {payload}")

    try:
        headers = {
            "Content-Type": "application/json",
            "accept": "application/json",
        }
        response = requests.post(url, json=payload, headers=headers)

        logging.debug(f"Response status code: {response.status_code}")
        logging.debug(f"Response content: {response.text}")

        response.raise_for_status()

        data = response.json()
        tx_cbor = data.get("cbor")
        if not tx_cbor:
            logging.error("No 'cbor' field in response.")
            return None
        return tx_cbor

    except requests.exceptions.HTTPError as http_err:
        logging.error(f"HTTP error in create_swap_transaction: {http_err}")
        logging.error(f"Status code: {response.status_code}")
        logging.error(f"Response content: {response.text}")
    except ValueError as json_err:
        logging.error(f"JSON decode error in create_swap_transaction: {json_err}")
        logging.error(f"Response content: {response.text}")
    except Exception as e:
        logging.error(f"Exception in create_swap_transaction: {e}", exc_info=True)
    return None

def sign_with_dexhunter(tx_cbor):
    """
    Sign a transaction using DexHunter's signing system.
    
    Args:
        tx_cbor (str): CBOR-encoded transaction to sign
    
    Returns:
        str: Signed transaction CBOR or None if signing fails
    
    Side Effects:
        - Creates witness files
        - Logs signing process details
    
    Known Issues:
        - Large transactions may hit size limits
        - Stake witness may be missing if not configured
        - May require manual cleanup of CBOR files
    """
    try:
        # Save initial CBOR
        save_cbor_file(tx_cbor, "initial_transaction.cbor")
        logging.info(f"Initial CBOR size: {len(tx_cbor)} chars")
        
        # Get both payment and stake keys
        payment_signing_key = PaymentSigningKey.from_json(SIGNING_KEY_JSON)
        payment_verification_key = payment_signing_key.to_verification_key()
        stake_signing_key = StakeSigningKey.from_json(STAKE_SIGNING_KEY_JSON)
        stake_verification_key = stake_signing_key.to_verification_key()
        
        # Verify we have both keys
        if not stake_signing_key or not stake_verification_key:
            logging.warning("Missing stake key(s) - will only sign with payment key")
        
        # Decode transaction
        tx = Transaction.from_cbor(bytes.fromhex(tx_cbor))
        tx_body_hash = tx.transaction_body.hash()
        
        # Create witness set
        witness_set = TransactionWitnessSet()
        witness_set.vkey_witnesses = []
        
        # Add payment key witness
        payment_witness = VerificationKeyWitness(
            payment_verification_key,
            payment_signing_key.sign(tx_body_hash)
        )
        witness_set.vkey_witnesses.append(payment_witness)
        logging.info(f"Added payment key witness: {str(payment_verification_key.hash())}")
        
        # Add stake key witness
        if stake_signing_key and stake_verification_key:
            stake_witness = VerificationKeyWitness(
                stake_verification_key,
                stake_signing_key.sign(tx_body_hash)
            )
            witness_set.vkey_witnesses.append(stake_witness)
            logging.info(f"Added stake key witness: {str(stake_verification_key.hash())}")
        
        # Convert to CBOR
        witness_cbor = witness_set.to_cbor()
        witness_hex = witness_cbor.hex()
        logging.info(f"Witness CBOR size: {len(witness_hex)} chars")
        save_cbor_file(witness_hex, "witness_transaction.cbor")
        
        # Create payload for DexHunter
        payload = {
            "Signatures": witness_hex,
            "txCbor": tx_cbor
        }
        
        # Send request to DexHunter
        headers = {
            "Content-Type": "application/json",
            "accept": "application/json"
        }
        
        logging.info("Sending request to DEX Hunter...")
        url = f"{DEXHUNTER_API_BASE_URL}/swap/sign"
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        # Log response details
        logging.info(f"DexHunter Response status: {response.status_code}")
        logging.debug(f"DexHunter Response headers: {dict(response.headers)}")
        logging.debug(f"DexHunter Response content: {response.text}")
        
        response.raise_for_status()
        data = response.json()
        
        # Get signed transaction
        signed_tx_cbor = data.get("cbor")
        if not signed_tx_cbor:
            raise ValueError("No 'cbor' field in response from DEX Hunter sign endpoint")
            
        # Save signed CBOR
        save_cbor_file(signed_tx_cbor, "signed_transaction.cbor")
        logging.info(f"Signed CBOR size: {len(signed_tx_cbor)} chars")
        
        # Verify signatures
        signed_tx = Transaction.from_cbor(bytes.fromhex(signed_tx_cbor))
        if signed_tx.transaction_witness_set and hasattr(signed_tx.transaction_witness_set, 'vkey_witnesses'):
            witness_hashes = [str(w.vkey.hash()) for w in signed_tx.transaction_witness_set.vkey_witnesses]
            required_signers = [str(s) for s in tx.transaction_body.required_signers] if hasattr(tx.transaction_body, 'required_signers') else []
            
            logging.info("\nVerifying final transaction signatures:")
            for signer in required_signers:
                if signer in witness_hashes:
                    logging.info(f"  ✓ Found signature for {signer}")
                else:
                    logging.warning(f"  ✗ Missing signature for {signer}")
        
        # Verify CBOR format before returning
        try:
            test_bytes = bytes.fromhex(signed_tx_cbor)
            logging.info(f"Verified final CBOR format, size: {len(test_bytes)} bytes")
        except ValueError as ve:
            logging.error(f"Invalid final CBOR hex string: {ve}")
            return None
            
        return signed_tx_cbor
        
    except Exception as e:
        logging.error(f"Exception in sign_with_dexhunter: {e}", exc_info=True)
        if 'payload' in locals():
            logging.error(f"Was trying to send payload: {json.dumps(payload, indent=2)}")
        raise

def save_cbor_file(cbor_data, filename, is_hex=True):
    """Save CBOR data to file for verification."""
    try:
        filepath = os.path.join(os.getcwd(), filename)
        if is_hex:
            # Convert hex string to bytes
            cbor_bytes = bytes.fromhex(cbor_data)
        else:
            cbor_bytes = cbor_data
            
        with open(filepath, 'wb') as f:
            f.write(cbor_bytes)
        logging.info(f"Saved CBOR to {filepath}")
        return True
    except Exception as e:
        logging.error(f"Error saving CBOR file: {e}")
        return False

def load_cbor_file(filename):
    """Load CBOR data from file."""
    try:
        filepath = os.path.join(os.getcwd(), filename)
        with open(filepath, 'rb') as f:
            cbor_bytes = f.read()
        logging.info(f"Loaded CBOR from {filepath}, size: {len(cbor_bytes)} bytes")
        return cbor_bytes
    except Exception as e:
        logging.error(f"Error loading CBOR file: {e}")
        return None

def submit_transaction(signed_tx_cbor: str) -> Optional[str]:
    """Submit transaction using Blockfrost."""
    url = "https://cardano-mainnet.blockfrost.io/api/v0/tx/submit"
    headers = {
        "project_id": BLOCKFROST_PROJECT_ID,
        "Content-Type": "application/cbor"
    }
    
    try:
        # Verify transaction
        tx = Transaction.from_cbor(bytes.fromhex(signed_tx_cbor))
        logging.info("Transaction verification passed")
        
        # Verify network parameters
        if hasattr(tx.transaction_body, 'network_id'):
            tx_network_id = tx.transaction_body.network_id
            logging.info(f"Transaction network ID: {tx_network_id}")
            if tx_network_id is not None and tx_network_id != NETWORK_ID:
                logging.error(f"Network ID mismatch: tx={tx_network_id}, env={NETWORK_ID}")
                raise ValueError("Transaction network ID does not match environment")
        
        if not tx.transaction_witness_set or not tx.transaction_witness_set.vkey_witnesses:
            raise ValueError("Transaction missing required witnesses")
        
        # Log witness details
        witness_hashes = [str(w.vkey.hash()) for w in tx.transaction_witness_set.vkey_witnesses]
        logging.info(f"Submitting with witnesses: {witness_hashes}")
        
        # Submit transaction
        raw_tx_bytes = bytes.fromhex(signed_tx_cbor)
        response = requests.post(
            url,
            headers=headers,
            data=raw_tx_bytes  # Send raw bytes directly
        )
        
        if response.status_code == 200:
            tx_hash = response.text.strip('"')
            logging.info(f"Transaction submitted. Hash: {tx_hash}")
            return tx_hash
            
        else:
            logging.error(f"Submission failed with status {response.status_code}")
            logging.error(f"Response content: {response.text}")
            
            try:
                error_data = response.json()
                if 'message' in error_data:
                    if isinstance(error_data['message'], str):
                        error_message = error_data['message']
                    else:
                        error_message = json.dumps(error_data['message'])
                    logging.error(f"Detailed error: {error_message}")
            except Exception as parse_error:
                logging.error(f"Failed to parse error response: {parse_error}")
            
            raise ValueError(f"Transaction submission failed: {response.text}")
            
    except Exception as e:
        logging.error(f"Exception in submit_transaction: {e}", exc_info=True)
        raise
    

def transfer_shards_to_gleec(quantity):
    """Transfer tokens to Gleec exchange."""
    try:
        gleec_shards_address = get_deposit_address('SHARDS')
        if gleec_shards_address:
            transaction_success = send_shards_to_address(gleec_shards_address, quantity)
            if transaction_success:
                deposit_confirmed = monitor_gleec_deposit('SHARDS', quantity)
                return deposit_confirmed
            else:
                logging.error("Failed to send SHARDS to Gleec deposit address.")
                return False
        else:
            logging.error("Failed to obtain Gleec SHARDS deposit address.")
            return False
    except Exception as e:
        logging.error(f"Exception in transfer_shards_to_gleec: {e}", exc_info=True)
        return False

def withdraw_shards_to_cardano(quantity):
    """Withdraw SHARDS tokens from Gleec exchange to Cardano wallet."""
    try:
        cardano_address = CARDANO_ADDRESS
        transaction_id = withdraw_crypto('SHARDS', quantity, cardano_address)
        if transaction_id:
            # Add a small delay before first status check
            time.sleep(2)
            withdrawal_confirmed = monitor_cardano_withdrawal(transaction_id)
            return withdrawal_confirmed
        else:
            logging.error("Failed to initiate SHARDS withdrawal from Gleec.")
            return False
    except Exception as e:
        logging.error(f"Exception in withdraw_shards_to_cardano: {e}", exc_info=True)
        return False

def send_shards_to_address(recipient_address, shards_quantity):
    """Send tokens to a specified Cardano address."""
    try:
        network = Network.MAINNET
        context = BlockFrostChainContext(BLOCKFROST_PROJECT_ID, network)

        # Load signing keys
        signing_key = PaymentSigningKey.from_json(SIGNING_KEY_JSON)
        verification_key = PaymentVerificationKey.from_json(VERIFICATION_KEY_JSON)
        key_pair = PaymentKeyPair(signing_key, verification_key)

        # Define TOKEN
        asset_name = AssetName(bytes.fromhex(TOKEN_ASSET_NAME))
        multi_asset = MultiAsset()
        multi_asset[TOKEN_POLICY_ID][asset_name] = int(shards_quantity)

        # Create transaction output
        min_ada = 1_500_000  # Adjust as needed
        value = Value(min_ada, multi_asset)
        recipient_addr = Address.from_primitive(recipient_address)
        tx_out = TransactionOutput(recipient_addr, value)

        # Build transaction
        builder = TransactionBuilder(context)
        builder.add_output(tx_out)
        my_address = Address.from_primitive(CARDANO_ADDRESS)
        builder.add_input_address(my_address)
        tx = builder.build_and_sign([signing_key], change_address=my_address)

        # Submit transaction
        tx_id = context.submit_tx(tx.to_cbor())
        logging.info(f"SHARDS sent to Gleec. Transaction ID: {tx_id}")
        return True
    except Exception as e:
        logging.error(f"Exception in send_shards_to_address: {e}", exc_info=True)
        return False

def get_deposit_address(currency):
    """Get the deposit address for a specified currency on Gleec exchange."""
    try:
        url = f"https://api.exchange.gleec.com/api/3/wallet/crypto/address?currency={currency}"
        session = requests.Session()
        session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
        response = session.get(url)
        response.raise_for_status()
        data = response.json()
        return data[0]['address']
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"HTTP error in get_deposit_address: {http_err}")
        logging.error(response.json())
    except Exception as e:
        logging.error(f"Exception in get_deposit_address: {e}")
    return None

def monitor_gleec_deposit(currency, expected_amount, timeout=3600):
    """Monitor the deposit of tokens on Gleec exchange."""
    try:
        start_time = time.time()
        while time.time() - start_time < timeout:
            balance = get_wallet_balance(currency)
            if balance:
                available = float(balance['available'])
                if available >= expected_amount:
                    logging.info(f"Deposit of {expected_amount} {currency} confirmed on Gleec.")
                    return True
            time.sleep(60)
        logging.error(f"Deposit of {expected_amount} {currency} not confirmed within timeout.")
        return False
    except Exception as e:
        logging.error(f"Exception in monitor_gleec_deposit: {e}", exc_info=True)
        return False

def get_wallet_balance(currency):
    """Get the wallet balance for a specified currency on Gleec exchange."""
    try:
        url = f"https://api.exchange.gleec.com/api/3/wallet/balance/{currency}"
        session = requests.Session()
        session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
        response = session.get(url)
        response.raise_for_status()
        data = response.json()
        return data
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"HTTP error in get_wallet_balance: {http_err}")
        logging.error(response.json())
    except Exception as e:
        logging.error(f"Exception in get_wallet_balance: {e}")
    return None

def withdraw_crypto(currency, amount, address, auto_commit=True):
    """Withdraw cryptocurrency from Gleec exchange to an external address."""
    url = "https://api.exchange.gleec.com/api/3/wallet/crypto/withdraw"
    data = {
        'currency': currency,
        'amount': str(amount),
        'address': address,
        'auto_commit': str(auto_commit).lower()  # Convert bool to "true"/"false"
    }
    session = requests.Session()
    session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
    
    try:
        response = session.post(url, data=data)
        response.raise_for_status()
        result = response.json()
        
        if 'id' in result:
            logging.info(f"Withdrawal initiated. Transaction ID: {result['id']}")
            return result['id']
        else:
            logging.error(f"Unexpected response format: {result}")
            return None
            
    except requests.exceptions.HTTPError as e:
        error_data = e.response.json().get('error', {})
        error_code = error_data.get('code')
        error_msg = error_data.get('message')
        error_desc = error_data.get('description', '')
        
        logging.error(f"Withdrawal failed: {error_code} - {error_msg}: {error_desc}")
        
        # Handle specific error cases
        if error_code == 20001:
            logging.error("Insufficient funds for withdrawal")
        elif error_code == 20003:
            logging.error("Withdrawal limit exceeded")
        elif error_code == 10001:
            logging.error(f"Validation error: {error_desc}")
            
        return None
    except Exception as e:
        logging.error(f"Exception in withdraw_crypto: {e}", exc_info=True)
        return None

def get_transaction_status(transaction_id):
    """Get detailed transaction status from Gleec."""
    url = f"https://api.exchange.gleec.com/api/3/wallet/transactions/{transaction_id}"
    session = requests.Session()
    session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
    
    try:
        response = session.get(url)
        response.raise_for_status()
        transaction = response.json()
        
        status = transaction.get('status')
        logging.info(f"Transaction {transaction_id} status: {status}")
        
        if status == 'SUCCESS':
            # Log blockchain details if available
            if 'native' in transaction:
                native = transaction['native']
                if 'hash' in native:
                    logging.info(f"Blockchain hash: {native['hash']}")
                if 'confirmations' in native:
                    logging.info(f"Confirmations: {native['confirmations']}")
                    
        return status, transaction.get('native', {})
        
    except requests.exceptions.HTTPError as e:
        logging.error(f"Error getting transaction status: {e}")
        if e.response.status_code == 404:
            return 'NOT_FOUND', {}
        return 'ERROR', {}
    except Exception as e:
        logging.error(f"Exception in get_transaction_status: {e}")
        return 'ERROR', {}

def deposit_to_cex(currency, amount):
    """Initiate a deposit of cryptocurrency to the Gleec exchange."""
    try:
        # Get deposit address from Gleec
        deposit_address = get_deposit_address(currency)
        if not deposit_address:
            logging.error("Failed to get deposit address from Gleec")
            return None

        # Send tokens to the deposit address
        tx_success = send_shards_to_address(deposit_address, amount)
        if not tx_success:
            logging.error("Failed to send tokens to deposit address")
            return None

        # Return a unique identifier for tracking the deposit
        deposit_id = f"deposit_{currency}_{int(time.time())}"
        return deposit_id

    except Exception as e:
        logging.error(f"Exception in deposit_to_cex: {e}", exc_info=True)
        return None

def monitor_cardano_withdrawal(transaction_id, timeout=3600, required_confirmations=2):
    """Monitor the withdrawal of tokens with enhanced status checking."""
    try:
        start_time = time.time()
        last_status = None
        check_count = 0
        
        while time.time() - start_time < timeout:
            check_count += 1
            status, details = get_transaction_status(transaction_id)
            
            if status != last_status:
                logging.info(f"Withdrawal {transaction_id} status changed to: {status}")
                last_status = status
            
            if status == 'SUCCESS':
                confirmations = details.get('confirmations', 0)
                if confirmations >= required_confirmations:
                    logging.info(f"Withdrawal {transaction_id} confirmed with {confirmations} confirmations")
                    return True
                else:
                    logging.info(f"Waiting for confirmations: {confirmations}/{required_confirmations}")
                    
            elif status == 'FAILED':
                logging.error(f"Withdrawal {transaction_id} failed")
                return False
                
            elif status == 'ROLLED_BACK':
                logging.error(f"Withdrawal {transaction_id} was rolled back")
                return False
                
            elif status == 'NOT_FOUND':
                if check_count > 3:  # Give it a few tries before giving up
                    logging.error(f"Withdrawal {transaction_id} not found after {check_count} attempts")
                    return False
                    
            # Exponential backoff for checking status
            sleep_time = min(60, 2 ** (check_count // 2))  # Max 60 seconds between checks
            time.sleep(sleep_time)
            
        logging.error(f"Withdrawal {transaction_id} monitoring timed out")
        return False
        
    except Exception as e:
        logging.error(f"Exception in monitor_cardano_withdrawal: {e}", exc_info=True)
        return False

def get_wallet_balance(currency):
    """Get the wallet balance for a specified currency."""
    url = f"https://api.exchange.gleec.com/api/3/wallet/balance/{currency}"
    session = requests.Session()
    session.auth = GleecAuth(GLEEC_API_KEY, GLEEC_SECRET_KEY)
    
    try:
        response = session.get(url)
        response.raise_for_status()
        balance = response.json()
        
        available = float(balance.get('available', 0))
        reserved = float(balance.get('reserved', 0))
        
        logging.info(f"Balance for {currency}: Available={available}, Reserved={reserved}")
        return {
            'available': available,
            'reserved': reserved,
            'total': available + reserved
        }
    except requests.exceptions.HTTPError as e:
        logging.error(f"Error getting wallet balance: {e}")
        logging.error(f"Response: {e.response.text}")
        return None
    except Exception as e:
        logging.error(f"Exception in get_wallet_balance: {e}")
        return None

def check_transfer_status(tx_id):
    """Check the status of a transfer on the Cardano blockchain."""
    url = f"https://cardano-mainnet.blockfrost.io/api/v0/txs/{tx_id}"
    headers = {
        "project_id": BLOCKFROST_PROJECT_ID
    }
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            tx_data = response.json()
            
            # Check number of confirmations
            if tx_data.get("block_height"):
                current_block = get_current_block()
                if current_block:
                    confirmations = current_block - tx_data["block_height"]
                    if confirmations >= 2:  # Requiring 2 confirmations
                        logging.info(f"Transaction {tx_id} confirmed with {confirmations} confirmations")
                        return 'confirmed'
                    else:
                        logging.info(f"Transaction {tx_id} has {confirmations} confirmations")
                        return 'pending'
                        
            return 'pending'
            
        elif response.status_code == 404:
            logging.warning(f"Transaction {tx_id} not found on blockchain")
            return 'not_found'
        else:
            logging.error(f"Error checking transaction status: {response.text}")
            return 'failed'
            
    except Exception as e:
        logging.error(f"Exception in check_transfer_status: {e}")
        return 'error'

def get_current_block():
    """Get the current block height from Blockfrost."""
    url = "https://cardano-mainnet.blockfrost.io/api/v0/blocks/latest"
    headers = {
        "project_id": BLOCKFROST_PROJECT_ID
    }
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            block_data = response.json()
            return block_data.get("height")
    except Exception as e:
        logging.error(f"Exception in get_current_block: {e}")
        return None

def get_bot_status():
    """Get current bot status and pending operations."""
    try:
        # Get current state
        state = state_manager.state
        pending_ops = state_manager.get_pending_operations()
        
        print("\nBot Status Report:")
        print("=================")
    
        # Check if bot is running
        try:
            with open('run/arbitrage_bot.pid', 'r') as f:
                pid = int(f.read().strip())
            try:
                os.kill(pid, 0)  # Test if process is running
                print("Bot Status: Running")
                print(f"Process ID: {pid}")
            except OSError:
                print("Bot Status: Not Running (stale PID file)")
        except FileNotFoundError:
            print("Bot Status: Not Running")
            
        # Show pending operations
        print("\nPending Operations:")
        if any(pending_ops.values()):
            if pending_ops['orders']:
                print("\nPending Orders:")
                for order_id, details in pending_ops['orders'].items():
                    print(f"- Order {order_id}: {details['status']}")
                    
            if pending_ops['transfers']:
                print("\nPending Transfers:")
                for tx_id, details in pending_ops['transfers'].items():
                    print(f"- Transfer {tx_id}: {details['status']}")
                    
            if pending_ops['withdrawals']:
                print("\nPending Withdrawals:")
                for withdrawal_id, details in pending_ops['withdrawals'].items():
                    print(f"- Withdrawal {withdrawal_id}: {details['status']}")
        else:
            print("No pending operations")
            
        # Show recent completed transactions
        print("\nRecent Completed Transactions:")
        completed = state.get('completed_transactions', [])
        if completed:
            for tx in completed[-5:]:  # Show last 5
                print(f"- {tx['tx_hash']} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(tx['timestamp']))})")
        else:
            print("No completed transactions")
            
    except Exception as e:
        print(f"Error checking status: {e}")

async def check_pending_operations():
    """
    Check and cleanup any pending operations from previous sessions.
    
    Returns:
        bool: True if all operations are completed or cleaned up
    
    Side Effects:
        - Updates bot state
        - Removes completed/failed operations
        - Logs cleanup actions
    
    Known Issues:
        - May miss operations if state file is corrupted
        - Manual intervention needed for some edge cases
        - Cannot recover some failed operations automatically
    """
    try:
        logging.info("Checking for pending operations...")
        pending_ops = state_manager.get_pending_operations()
        
        if any(pending_ops.values()):
            # Handle pending withdrawals
            for withdrawal_id in list(pending_ops['withdrawals'].keys()):
                status = check_withdrawal_status(withdrawal_id)
                if status == 'SUCCESS':
                    # Remove completed withdrawal
                    if withdrawal_id in state_manager.state['active_withdrawals']:
                        del state_manager.state['active_withdrawals'][withdrawal_id]
                        # Mark transaction as complete with type
                        state_manager.complete_transaction(withdrawal_id, tx_type='withdrawal')
                elif status in ['FAILED', 'ROLLED_BACK']:
                    # Remove failed withdrawal
                    if withdrawal_id in state_manager.state['active_withdrawals']:
                        del state_manager.state['active_withdrawals'][withdrawal_id]
                
            # Handle pending orders
            for order_id in list(pending_ops['orders'].keys()):
                status = check_order_status(order_id)
                if status in ['filled', 'canceled', 'expired']:
                    # Remove completed/failed order
                    if order_id in state_manager.state['active_orders']:
                        del state_manager.state['active_orders'][order_id]
            
            # Save state after cleanup
            state_manager.save_state()
            
            # Check if we still have pending operations after cleanup
            remaining_ops = state_manager.get_pending_operations()
            if any(remaining_ops.values()):
                logging.info("Still have pending operations after cleanup")
                return False
            else:
                logging.info("All operations completed and cleaned up")
                return True
            
        else:
            logging.info("No pending operations found")
            return True
            
    except Exception as e:
        logging.error(f"Error in check_pending_operations: {e}")
        return False

async def check_pending_cycle():
    """Check for any incomplete trading cycles on startup."""
    try:
        # Check completed transactions to see if we have pending DEX sells
        completed_withdrawals = [tx for tx in state_manager.state.get('completed_transactions', [])
                               if 'withdrawal' in tx.get('type', '')]
        
        if completed_withdrawals:
            latest_withdrawal = completed_withdrawals[-1]  # Get most recent withdrawal
            if not latest_withdrawal.get('dex_sell_completed'):
                logging.info("Found incomplete cycle - executing DEX sell before starting new trades")
                # Execute DEX sell for the withdrawal amount
                dex_success = execute_trade_on_dex(TRADE_QUANTITY, sell=True)
                if dex_success:
                    # Update the transaction to mark DEX sell as completed
                    latest_withdrawal['dex_sell_completed'] = True
                    state_manager.save_state()
                    logging.info("Completed pending DEX sell from previous cycle")
                else:
                    logging.error("Failed to complete pending DEX sell")
                return True  # Return True to indicate we handled an incomplete cycle
            
        return False  # Return False to indicate no incomplete cycles
        
    except Exception as e:
        logging.error(f"Error in check_pending_cycle: {e}")
        return False

def clear_stale_operations(force=False):
    """Clear stale or force clear all pending operations."""
    now = time.time()
    state = state_manager.state
    cleared = False

    for section in ['active_orders', 'pending_transfers', 'active_withdrawals']:
        for id in list(state.get(section, {}).keys()):
            if force or now - state[section][id].get('timestamp', 0) > 3600:
                logging.warning(f"Clearing {section} entry: {id}")
                del state[section][id]
                cleared = True

    if cleared:
        state_manager.save_state()
        logging.info("Cleared operations")

def signal_handler(signum, _):
    """Handle shutdown signals."""
    logging.info(f"Received shutdown signal {signum}. Cleaning up...")
    clear_stale_operations(force=True)
    sys.exit(0)


async def main():
    """Main function to run the arbitrage bot."""
    try:
        # Force clear all pending operations on startup
        clear_stale_operations(force=True)
        logging.info("Cleared all pending operations on startup")

        # Verify environment first
        if not verify_environment():
            logging.error("Failed environment verification")
            return
            
        # Check pending operations from previous session
        await check_pending_operations()
        
        # Check Cardano balance for pending DEX sells
        has_balance = await check_cardano_balance()
        if has_balance:
            logging.info("Found SHARDS on Cardano - executing DEX sell before starting new trades")
            dex_success = execute_trade_on_dex(TRADE_QUANTITY, sell=True)
            if dex_success:
                logging.info("Successfully sold SHARDS from previous cycle")
            else:
                logging.error("Failed to sell SHARDS from previous cycle")
    
        while True:
            try:
                await check_arbitrage_opportunity()
                await asyncio.sleep(60)
            except Exception as e:
                logging.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(60)
                
    except Exception as e:
        logging.error(f"Fatal error in main: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    # Create required directories
    for dir in ['run', 'logs']:
        if not os.path.exists(dir):
            os.makedirs(dir)
    
    # Setup pid file
    pidfile = pid.PidFile(pidname='arbitrage_bot', piddir='run')
    
    try:
        with pidfile:
            logging.info("Starting arbitrage bot...")
            logging.info(f"PID: {os.getpid()}")
            
            # Handle graceful shutdown
            def signal_handler(signum, frame):
                logging.info("Received shutdown signal. Cleaning up...")
                clear_stale_operations(force=True)
                sys.exit(0)
            
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
            
            # Run the main loop with asyncio
            asyncio.run(main())
            
    except pid.PidFileError:
        logging.error("Bot is already running! Check run/arbitrage_bot.pid")
        sys.exit(1)
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
    except Exception as e:
        logging.error(f"Exception in main: {e}", exc_info=True)
        sys.exit(1)
