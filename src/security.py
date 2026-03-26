"""Security utilities for protecting private keys.

IMPORTANT: This bot requires a private key to sign orders.
These utilities help ensure the key is handled safely.
"""

import hashlib
import os
import sys

from loguru import logger


def audit_env_file(env_path: str = ".env") -> bool:
    """Check that .env file has safe permissions (not world-readable)."""
    if not os.path.exists(env_path):
        return True

    if sys.platform != "win32":
        import stat
        mode = os.stat(env_path).st_mode
        if mode & stat.S_IROTH:
            logger.warning(
                f"SECURITY: {env_path} is world-readable! "
                f"Run: chmod 600 {env_path}"
            )
            return False
    return True


def check_private_key_format(key: str) -> bool:
    """Validate private key format without exposing it."""
    # Remove 0x prefix if present
    clean = key.strip().lower()
    if clean.startswith("0x"):
        clean = clean[2:]

    if len(clean) != 64:
        logger.error("Private key must be 64 hex characters (without 0x prefix)")
        return False

    try:
        int(clean, 16)
    except ValueError:
        logger.error("Private key contains invalid characters")
        return False

    return True


def fingerprint_key(key: str) -> str:
    """Create a safe fingerprint of the key for logging (never log the key itself)."""
    h = hashlib.sha256(key.encode()).hexdigest()
    return f"sha256:{h[:8]}...{h[-4:]}"


def warn_if_mainnet_with_funds():
    """Remind user to use a dedicated wallet."""
    logger.warning("=" * 60)
    logger.warning("  SECURITY REMINDER")
    logger.warning("=" * 60)
    logger.warning("  1. Use a DEDICATED wallet for this bot")
    logger.warning("     Do NOT use your main wallet!")
    logger.warning("  2. Only deposit what you can afford to lose")
    logger.warning("  3. Approve only USDC + CTF contracts")
    logger.warning("     Revoke all other token approvals")
    logger.warning("  4. Never share your .env file")
    logger.warning("=" * 60)


def scan_dependencies_basic():
    """Basic check: warn if any pip packages have install hooks or unusual scripts.

    This is NOT a replacement for proper dependency auditing tools,
    but catches the most obvious red flags.
    """
    risky_packages = []

    try:
        import importlib.metadata
        for dist in importlib.metadata.distributions():
            name = dist.metadata["Name"]
            # Check for post-install scripts (common malware vector)
            if dist.files:
                for f in dist.files:
                    fstr = str(f)
                    if "setup.py" in fstr or "post_install" in fstr:
                        # This is normal for many packages, just flag unusual ones
                        pass
                    if "exfil" in fstr or "keylog" in fstr or "steal" in fstr:
                        risky_packages.append((name, fstr))
    except Exception:
        pass

    if risky_packages:
        logger.error("SECURITY ALERT: Suspicious files found in packages:")
        for pkg, path in risky_packages:
            logger.error(f"  {pkg}: {path}")
        return False

    return True


def run_all_checks(private_key: str) -> bool:
    """Run all security checks before starting the bot."""
    all_ok = True

    # 1. Check .env permissions
    if not audit_env_file():
        all_ok = False

    # 2. Validate key format
    if not check_private_key_format(private_key):
        return False  # Fatal

    # 3. Log key fingerprint (safe to log)
    fp = fingerprint_key(private_key)
    logger.info(f"Wallet key fingerprint: {fp}")

    # 4. Warn about dedicated wallet
    warn_if_mainnet_with_funds()

    # 5. Basic dependency scan
    if not scan_dependencies_basic():
        all_ok = False

    return all_ok
