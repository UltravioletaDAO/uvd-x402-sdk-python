"""
SDK configuration classes.

This module provides configuration management for the x402 SDK,
including facilitator settings, recipient addresses, and timeouts.

Supports:
- x402 v1 and v2 protocols
- Multi-network payment options
- Per-network recipient configuration
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Literal
import json
import os


# Reserved key inside ``facilitator_by_network`` meaning "every other network".
# Its presence is the ONLY thing that authorises falling back to a facilitator
# that was not named for the network at hand.
FACILITATOR_FALLBACK_KEY = "*"


@dataclass
class NetworkRecipientConfig:
    """
    Configuration for a specific network's recipient address.

    Use this to specify different recipient addresses for different networks.
    """

    recipient: str
    enabled: bool = True


# Alias for backward compatibility
NetworkConfig = NetworkRecipientConfig


@dataclass
class MultiPaymentConfig:
    """
    Configuration for multi-payment support.

    Allows users to offer multiple networks for payment acceptance.
    """

    networks: List[str] = field(default_factory=list)
    default_network: Optional[str] = None

    def __post_init__(self) -> None:
        if self.networks and not self.default_network:
            self.default_network = self.networks[0]


@dataclass
class X402Config:
    """
    Main SDK configuration.

    Attributes:
        facilitator_url: URL of the x402 facilitator service. Used for every
            network unless ``facilitator_by_network`` routes it elsewhere, and
            for the endpoints that are not network-scoped (``/version``,
            ``/blacklist``, ``/api/stats``, ``/transactions``).
        facilitator_by_network: Optional routing table ``network -> facilitator
            URL``, for deployments that settle different networks through
            different facilitators (e.g. base via CDP, avalanche via UVD,
            because CDP does not settle avalanche). The reserved key ``"*"``
            declares the fallback for anything not named. Leave it empty and
            nothing changes: a single facilitator serves every network, exactly
            as before.
        recipient_evm: Default recipient address for EVM chains
        recipient_solana: Recipient address for Solana/SVM chains (also used for Fogo)
        recipient_near: Recipient account for NEAR
        recipient_stellar: Recipient address for Stellar
        recipient_xrpl: Recipient address for XRP Ledger (classic r... address)
        facilitator_solana: Solana/SVM facilitator address (fee payer)
        verify_timeout: Timeout for verify requests (seconds)
        settle_timeout: Timeout for settle requests (seconds)
        supported_networks: List of enabled network names
        network_configs: Per-network recipient overrides
        resource_url: Resource URL sent to facilitator
        description: Description sent to facilitator
        x402_version: Protocol version to use (1, 2, or "auto")
        multi_payment: Multi-payment configuration for accepting multiple networks
    """

    facilitator_url: str = "https://facilitator.ultravioletadao.xyz"

    # Per-network facilitator routing. Empty = single-facilitator behavior.
    facilitator_by_network: Dict[str, str] = field(default_factory=dict)

    # Recipient addresses per network type
    recipient_evm: str = ""
    recipient_solana: str = ""  # Also used for Fogo and other SVM chains
    recipient_near: str = ""
    recipient_stellar: str = ""
    recipient_xrpl: str = ""  # XRP Ledger recipient (classic r... address)

    # Solana/SVM facilitator (fee payer) - same for all SVM chains
    facilitator_solana: str = "F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq"

    # Timeouts
    verify_timeout: float = 30.0
    settle_timeout: float = 55.0  # Must be < Lambda timeout (60s)

    # Network configuration - All 25 networks
    supported_networks: List[str] = field(default_factory=lambda: [
        # EVM chains (15)
        "base", "ethereum", "polygon", "arbitrum", "optimism",
        "avalanche", "celo", "hyperevm", "unichain", "monad",
        "scroll", "skale-base", "skale-base-sepolia",
        "robinhood", "robinhood-testnet",
        # SVM chains (2)
        "solana", "fogo",
        # NEAR (1)
        "near",
        # Stellar (1)
        "stellar",
        # Algorand (2)
        "algorand", "algorand-testnet",
        # Sui (2)
        "sui", "sui-testnet",
        # XRPL (2) - native XRP
        "xrpl-mainnet", "xrpl-testnet",
    ])

    # Per-network recipient overrides
    network_configs: Dict[str, NetworkRecipientConfig] = field(default_factory=dict)

    # Facilitator request metadata
    resource_url: str = ""
    description: str = "x402 payment"

    # x402 protocol version: 1, 2, or "auto" (detect from payload)
    x402_version: Literal[1, 2, "auto"] = "auto"

    # Multi-payment configuration
    multi_payment: Optional[MultiPaymentConfig] = None

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.facilitator_url:
            raise ValueError("facilitator_url is required")

        # At least one recipient is required
        if not any([
            self.recipient_evm,
            self.recipient_solana,
            self.recipient_near,
            self.recipient_stellar,
            self.recipient_xrpl,
        ]):
            raise ValueError("At least one recipient address is required")

        # Normalised routing table. Built here on purpose: a facilitator route
        # that cannot serve an enabled network must blow up at boot, not on the
        # first payment.
        self._facilitator_routes: Dict[str, str] = self._build_facilitator_routes()

    # =========================================================================
    # Facilitator Routing
    # =========================================================================

    @staticmethod
    def network_key(network: str) -> str:
        """Normalise a network identifier for routing-table lookups.

        Unknown identifiers are lowercased rather than rejected: rejection is
        the caller's job (``_build_facilitator_routes`` for keys,
        ``facilitator_url_for`` for lookups), and both give a better message
        than a bare ValueError from the network registry.
        """
        from uvd_x402_sdk.networks import normalize_network

        try:
            return normalize_network(network)
        except ValueError:
            return network.lower()

    def _build_facilitator_routes(self) -> Dict[str, str]:
        """Validate ``facilitator_by_network`` and normalise it into a lookup table.

        Raises:
            ConfigurationError: If a key is not a known network, a value is not
                an http(s) URL, two spellings of the same network disagree, or
                an ENABLED network would be left without a facilitator (and no
                ``"*"`` fallback was declared).
        """
        from uvd_x402_sdk.exceptions import ConfigurationError
        from uvd_x402_sdk.networks import get_network

        routes: Dict[str, str] = {}
        if not self.facilitator_by_network:
            return routes

        for raw_key, raw_url in self.facilitator_by_network.items():
            if not isinstance(raw_url, str) or not raw_url.startswith(("http://", "https://")):
                raise ConfigurationError(
                    f"facilitator_by_network[{raw_key!r}] must be an http(s) URL, "
                    f"got {raw_url!r}",
                    config_key="facilitator_by_network",
                )
            url = raw_url.rstrip("/")

            if raw_key == FACILITATOR_FALLBACK_KEY:
                routes[FACILITATOR_FALLBACK_KEY] = url
                continue

            key = self.network_key(raw_key)
            if get_network(key) is None:
                raise ConfigurationError(
                    f"facilitator_by_network has an unknown network key {raw_key!r}. "
                    f"Use a network name the SDK knows (e.g. 'base', 'avalanche', "
                    f"'eip155:8453') or {FACILITATOR_FALLBACK_KEY!r} for the fallback.",
                    config_key="facilitator_by_network",
                )

            existing = routes.get(key)
            if existing is not None and existing != url:
                raise ConfigurationError(
                    f"facilitator_by_network routes network {key!r} to two different "
                    f"facilitators ({existing} and {url}) — two spellings of the same "
                    f"network disagree.",
                    config_key="facilitator_by_network",
                )
            routes[key] = url

        if FACILITATOR_FALLBACK_KEY not in routes:
            unrouted = [
                network
                for network in self.supported_networks
                if self.is_network_enabled(network)
                and self.network_key(network) not in routes
            ]
            if unrouted:
                raise ConfigurationError(
                    f"facilitator_by_network is set but these enabled networks have no "
                    f"facilitator: {', '.join(unrouted)}. Route each one explicitly, "
                    f"narrow supported_networks to what you actually accept, or add a "
                    f"{FACILITATOR_FALLBACK_KEY!r} entry as the fallback. They will NOT "
                    f"be routed to facilitator_url silently.",
                    config_key="facilitator_by_network",
                )

        return routes

    def facilitator_url_for(self, network: str) -> str:
        """Resolve which facilitator settles a given network.

        With no ``facilitator_by_network`` configured this always returns
        ``facilitator_url`` — the pre-existing behavior, for any input.

        Args:
            network: Network identifier (v1 name or CAIP-2).

        Returns:
            Facilitator base URL for that network.

        Raises:
            ConfigurationError: If a routing table is configured and neither the
                network nor a ``"*"`` fallback is in it. It never guesses.

        Example:
            >>> config = X402Config(
            ...     recipient_evm="0xMerchant...",
            ...     supported_networks=["base", "avalanche"],
            ...     facilitator_by_network={
            ...         "base": "https://api.cdp.coinbase.com/platform/v2/x402",
            ...         "avalanche": "https://facilitator.ultravioletadao.xyz",
            ...     },
            ... )
            >>> config.facilitator_url_for("base")
            'https://api.cdp.coinbase.com/platform/v2/x402'
            >>> config.facilitator_url_for("eip155:43114")  # CAIP-2 for avalanche
            'https://facilitator.ultravioletadao.xyz'
        """
        from uvd_x402_sdk.exceptions import ConfigurationError

        routes: Dict[str, str] = getattr(self, "_facilitator_routes", None) or {}
        if not routes:
            return self.facilitator_url

        if network:
            url = routes.get(self.network_key(network))
            if url:
                return url

        fallback = routes.get(FACILITATOR_FALLBACK_KEY)
        if fallback:
            return fallback

        named = sorted(k for k in routes if k != FACILITATOR_FALLBACK_KEY)
        raise ConfigurationError(
            f"No facilitator is configured for network {network!r}. "
            f"facilitator_by_network routes: {', '.join(named) or '(none)'}. "
            f"Add it, or add a {FACILITATOR_FALLBACK_KEY!r} entry to declare a fallback.",
            config_key="facilitator_by_network",
        )

    def facilitator_routes(self) -> Dict[str, str]:
        """Resolved ``network -> facilitator URL`` for every ENABLED network.

        The boot-time picture of where each network would settle. Useful for
        diagnostics and for :meth:`X402Client.verify_routes`.
        """
        return {
            network: self.facilitator_url_for(network)
            for network in self.supported_networks
            if self.is_network_enabled(network)
        }

    @classmethod
    def from_env(cls) -> "X402Config":
        """
        Create configuration from environment variables.

        Environment variables:
            X402_FACILITATOR_URL: Facilitator URL
            X402_FACILITATOR_BY_NETWORK: JSON object mapping network -> facilitator
                URL, e.g. '{"base": "https://cdp...", "avalanche": "https://uvd..."}'.
                Use the key "*" to declare a fallback. Unset = single facilitator.
            X402_RECIPIENT_EVM: EVM recipient address
            X402_RECIPIENT_SOLANA: Solana recipient address
            X402_RECIPIENT_NEAR: NEAR recipient account
            X402_RECIPIENT_STELLAR: Stellar recipient address
            X402_RECIPIENT_XRPL: XRP Ledger recipient address
            X402_FACILITATOR_SOLANA: Solana fee payer address
            X402_VERIFY_TIMEOUT: Verify request timeout
            X402_SETTLE_TIMEOUT: Settle request timeout
            X402_RESOURCE_URL: Resource URL for facilitator
            X402_DESCRIPTION: Description for facilitator
        """
        return cls(
            facilitator_url=os.environ.get(
                "X402_FACILITATOR_URL",
                "https://facilitator.ultravioletadao.xyz",
            ),
            facilitator_by_network=cls._parse_facilitator_by_network_env(
                os.environ.get("X402_FACILITATOR_BY_NETWORK", "")
            ),
            recipient_evm=os.environ.get("X402_RECIPIENT_EVM", ""),
            recipient_solana=os.environ.get("X402_RECIPIENT_SOLANA", ""),
            recipient_near=os.environ.get("X402_RECIPIENT_NEAR", ""),
            recipient_stellar=os.environ.get("X402_RECIPIENT_STELLAR", ""),
            recipient_xrpl=os.environ.get("X402_RECIPIENT_XRPL", ""),
            facilitator_solana=os.environ.get(
                "X402_FACILITATOR_SOLANA",
                "F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq",
            ),
            verify_timeout=float(os.environ.get("X402_VERIFY_TIMEOUT", "30")),
            settle_timeout=float(os.environ.get("X402_SETTLE_TIMEOUT", "55")),
            resource_url=os.environ.get("X402_RESOURCE_URL", ""),
            description=os.environ.get("X402_DESCRIPTION", "x402 payment"),
        )

    @staticmethod
    def _parse_facilitator_by_network_env(raw: str) -> Dict[str, str]:
        """Parse X402_FACILITATOR_BY_NETWORK. Malformed JSON fails loudly."""
        from uvd_x402_sdk.exceptions import ConfigurationError

        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ConfigurationError(
                f"X402_FACILITATOR_BY_NETWORK is not valid JSON: {exc}",
                config_key="facilitator_by_network",
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigurationError(
                "X402_FACILITATOR_BY_NETWORK must be a JSON object mapping "
                "network -> facilitator URL",
                config_key="facilitator_by_network",
            )
        return {str(k): v for k, v in parsed.items()}

    def get_recipient(self, network: str) -> str:
        """
        Get recipient address for a specific network.

        First checks network_configs for overrides, then falls back to
        network-type default.

        Args:
            network: Network name (e.g., 'base', 'solana', 'fogo')

        Returns:
            Recipient address for the network
        """
        # Check for network-specific override
        if network in self.network_configs:
            return self.network_configs[network].recipient

        # Fall back to network-type default
        from uvd_x402_sdk.networks import get_network, NetworkType

        network_config = get_network(network)
        if not network_config:
            return self.recipient_evm  # Default to EVM

        # SVM chains (Solana, Fogo, etc.) use the same recipient
        if NetworkType.is_svm(network_config.network_type):
            return self.recipient_solana
        elif network_config.network_type == NetworkType.NEAR:
            return self.recipient_near
        elif network_config.network_type == NetworkType.STELLAR:
            return self.recipient_stellar
        elif network_config.network_type == NetworkType.XRPL:
            return self.recipient_xrpl
        else:
            return self.recipient_evm

    def is_network_enabled(self, network: str) -> bool:
        """
        Check if a network is enabled.

        Args:
            network: Network name

        Returns:
            True if network is in supported_networks and not disabled
        """
        if network not in self.supported_networks:
            return False

        # Check network-specific config
        if network in self.network_configs:
            return self.network_configs[network].enabled

        return True

    def get_supported_chain_ids(self) -> List[int]:
        """
        Get list of supported EVM chain IDs.

        Returns:
            List of chain IDs for enabled EVM networks
        """
        from uvd_x402_sdk.networks import get_network, NetworkType

        chain_ids = []
        for network_name in self.supported_networks:
            network = get_network(network_name)
            if network and network.network_type == NetworkType.EVM and network.chain_id > 0:
                if self.is_network_enabled(network_name):
                    chain_ids.append(network.chain_id)

        return chain_ids

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "facilitator_url": self.facilitator_url,
            "facilitator_by_network": dict(self.facilitator_by_network),
            "recipient_evm": self.recipient_evm,
            "recipient_solana": self.recipient_solana,
            "recipient_near": self.recipient_near,
            "recipient_stellar": self.recipient_stellar,
            "recipient_xrpl": self.recipient_xrpl,
            "facilitator_solana": self.facilitator_solana,
            "verify_timeout": self.verify_timeout,
            "settle_timeout": self.settle_timeout,
            "supported_networks": self.supported_networks,
            "resource_url": self.resource_url,
            "description": self.description,
        }
