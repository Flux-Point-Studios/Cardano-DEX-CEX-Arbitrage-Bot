# Cardano DEX/CEX Arbitrage Bot 🤖

An open-source arbitrage bot for maintaining price consistency between Cardano DEXs and Gleec Exchange. This bot monitors price differences and executes trades automatically when profitable opportunities arise.

## Overview

This bot helps maintain price consistency for tokens listed on both Cardano DEXs (via DexHunter) and Gleec Exchange by:
- Monitoring price differences in real-time
- Executing automated buy/sell orders when profitable opportunities are found
- Managing the complete trade cycle including withdrawals and deposits
- Providing detailed logging and state management
- Supporting both DEX-to-CEX and CEX-to-DEX arbitrage

## Prerequisites

- Python 3.8+
- Cardano node (for transaction submission)
- Gleec Exchange API credentials
- Blockfrost API key
- Cardano wallet with payment and stake keys
- Sufficient ADA for transaction fees
- The token you want to arbitrage must be listed on both Gleec Exchange and Cardano DEXs

---

## Installation

1. Clone the repository:
```bash
git clone https://github.com/Flux-Point-Studios/Cardano-DEX-CEX-Arbitrage-Bot.git
```

2. Create and activate a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: .\venv\Scripts\activate
```

3. Install required packages:
```bash
pip install -r requirements.txt
```

4. Copy the example environment file and configure your settings:
```bash
cp .env.example .env
```

## Configuration

Create a `.env` file with the following required variables:

```env
# Network Configuration
NETWORK_ID=1  # 1 for mainnet, 0 for testnet
PROTOCOL_MAGIC=764824073  # Mainnet magic number

# API Keys
GLEEC_API_KEY=your_gleec_api_key
GLEEC_SECRET_KEY=your_gleec_secret_key
BLOCKFROST_PROJECT_ID=your_blockfrost_project_id
MAESTRO_API_KEY=your_maestro_api_key

# Cardano Wallet Configuration
CARDANO_ADDRESS=your_cardano_address
SIGNING_KEY_JSON=your_signing_key_json
VERIFICATION_KEY_JSON=your_verification_key_json
STAKE_SIGNING_KEY_JSON=your_stake_signing_key_json
STAKE_VERIFICATION_KEY_JSON=your_stake_verification_key_json

# Token Configuration
TRADE_QUANTITY=100  # Amount of tokens per trade
ARBITRAGE_THRESHOLD=1.0  # Minimum price difference percentage
```

#### Getting Your Keys Using Demeter.run (Optional)
1. Visit [Demeter.run](https://demeter.run) and create an account
2. Create a new project:
   - Click "New Project"
   - Select "Hosting"
   - Enable "Activate to enter repository URL" and enter ```https://github.com/Flux-Point-Studios/Cardano-DEX-CEX-Arbitrage-Bot.git```

3. Open Terminal in Demeter.run and enter these commands:

```bash
# Create a working directory
mkdir wallet-keys
cd wallet-keys

# Generate payment key pair
cardano-cli address key-gen \
--normal-key \
--verification-key-file payment.vkey \
--signing-key-file payment.skey

# Generate stake key pair
cardano-cli stake-address key-gen \
--verification-key-file stake.vkey \
--signing-key-file stake.skey

# Build your wallet address
cardano-cli address build \
--payment-verification-key-file payment.vkey \
--stake-verification-key-file stake.vkey \
--mainnet \
--out-file payment.addr

# Display your address
cat payment.addr
```

4. Export Keys in Required Format:
```bash
# Convert payment signing key to JSON
cardano-cli key convert-cardano-address-key \
--shelley-payment-key \
--signing-key-file payment.skey \
--out-file payment.json

# Convert stake signing key to JSON
cardano-cli key convert-cardano-address-key \
--shelley-stake-key \
--signing-key-file stake.skey \
--out-file stake.json

# Generate verification key JSONs
cardano-cli key verification-key \
--signing-key-file payment.json \
--verification-key-file payment-verification.json

cardano-cli key verification-key \
--signing-key-file stake.json \
--verification-key-file stake-verification.json
```

5. Copy the contents of these files for your .env:
   - `payment.json` → SIGNING_KEY_JSON
   - `payment-verification.json` → VERIFICATION_KEY_JSON
   - `stake.json` → STAKE_SIGNING_KEY_JSON
   - `stake-verification.json` → STAKE_VERIFICATION_KEY_JSON
   - `payment.addr` → CARDANO_ADDRESS

6. Fund Your New Wallet:
   - Copy the address from payment.addr
   - Send ADA and tokens to this address to fund
   - Wait for confirmations

#### Important Security Notes
- **NEVER** share your signing keys or store them in unsecured locations
- Keep multiple secure backups of your keys
- The address generated must be funded before it can be used
- Verify all transactions on a small scale before running the bot with larger amounts

#### Verifying Your Setup
After setting up your keys and funding your address, you can verify everything is working:

```bash
# Check your address balance
cardano-cli query utxo \
--address $(cat payment.addr) \
--mainnet
```

This should show your current balance and any tokens at the address.


## Usage

1. Start the bot:
```bash
./bot_control.py start
```

2. Check bot status:
```bash
./bot_control.py status
```

3. Stop the bot:
```bash
./bot_control.py stop
```

Monitor the logs in `logs/bot.log` for detailed operation information.

---

## Security Considerations

- Never share your API keys or wallet keys
- Start with small trade amounts while testing
- Monitor the bot's performance regularly
- Keep your dependencies updated
- Review transaction parameters before deploying
- Set reasonable thresholds to avoid unnecessary trades

---

## Customization

To use this bot with your token:

1. Update the token constants in the code:
```python
SHARDS_POLICY_ID = 'your_token_policy_id'
SHARDS_ASSET_NAME = 'your_token_asset_name_hex'
SHARDS_TOKEN_ID = f"{SHARDS_POLICY_ID}{SHARDS_ASSET_NAME}"
```

2. Adjust trade parameters in your `.env` file:
```env
TRADE_QUANTITY=your_preferred_quantity
ARBITRAGE_THRESHOLD=your_preferred_threshold
```

---

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. For major changes, please open an issue first to discuss what you would like to change.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## Disclaimer

Trading cryptocurrencies carries risk. This bot is provided as-is with no guarantees. Users are responsible for:
- Testing thoroughly before deployment
- Setting appropriate trade limits
- Monitoring bot operation
- Managing their own funds and keys
- Complying with relevant regulations

---

## Support

If you encounter any issues or have feature requests, feel free to:

- **Create an Issue**: Submit a bug report or feature request in the [Issues](#) section.
- **Join the Community**: Connect with us on [Discord](https://discord.gg/MfYUMnfrJM) for discussions and updates.

---

## Useful Links

Here are some resources to help you get started:

- [Gleec Exchange API Documentation](https://api.exchange.gleec.com/)
- [DexHunter v3 API Swagger Documentation](https://api-us.dexhunterv3.app/swagger/index.html#/)

---

## Acknowledgments

- DexHunter API for DEX integrations
- Gleec Exchange for CEX trading capabilities
- Blockfrost for Cardano network interaction
- The Cardano community for ongoing support 🙏

---

## Project Status

This project is marked as completed/closed.
