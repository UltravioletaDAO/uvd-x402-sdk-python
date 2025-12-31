"""
Pydantic models for x402 payment data structures.

These models define the structure of payloads exchanged between:
- Frontend -> Backend (X-PAYMENT header)
- Backend -> Facilitator (verify/settle requests)
- Facilitator -> Backend (responses)

Supports both x402 v1 and v2 protocols:
- v1: network as string ("base", "solana")
- v2: network as CAIP-2 ("eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
"""

from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator


# =============================================================================
# Network-Specific Payload Content Models
# =============================================================================


class EVMAuthorization(BaseModel):
    """
    ERC-3009 TransferWithAuthorization data for EVM chains.

    This represents a signed EIP-712 message authorizing USDC transfer.
    The facilitator uses this to call transferWithAuthorization() on-chain.
    """

    from_address: str = Field(..., alias="from", description="Sender wallet address")
    to: str = Field(..., description="Recipient wallet address")
    value: str = Field(..., description="Amount in token base units (e.g., 1000000 for $1 USDC)")
    validAfter: str = Field(..., description="Unix timestamp after which auth is valid")
    validBefore: str = Field(..., description="Unix timestamp before which auth is valid")
    nonce: str = Field(..., description="Random 32-byte nonce (prevents replay attacks)")

    class Config:
        populate_by_name = True


class EVMPayloadContent(BaseModel):
    """
    Complete EVM payment payload with signature and authorization.
    """

    signature: str = Field(..., description="Full signature (r + s + v)")
    authorization: EVMAuthorization


class SVMPayloadContent(BaseModel):
    """
    SVM (Solana Virtual Machine) payment payload containing a partially-signed transaction.

    Works with all SVM-compatible chains: Solana, Fogo, etc.

    The transaction structure must follow the facilitator's requirements:
    1. Instruction 0: SetComputeUnitLimit (units: 20,000)
    2. Instruction 1: SetComputeUnitPrice (microLamports: 1)
    3. Instruction 2: TransferChecked (USDC transfer)

    Fee payer is the facilitator - user signature only authorizes USDC transfer.
    """

    transaction: str = Field(
        ..., description="Base64-encoded partially-signed VersionedTransaction"
    )


# Alias for backward compatibility
SolanaPayloadContent = SVMPayloadContent


class NEARPayloadContent(BaseModel):
    """
    NEAR payment payload using NEP-366 meta-transactions.

    Contains a Borsh-serialized SignedDelegateAction that wraps ft_transfer.
    User signs the delegate action, facilitator submits and pays gas.

    NEP-366 Structure:
    - DelegateAction: sender_id, receiver_id, actions, nonce, max_block_height, public_key
    - SignedDelegateAction: delegate_action + ed25519 signature
    - Hash prefix: 2^30 + 366 = 0x4000016E (little-endian)
    """

    signedDelegateAction: str = Field(
        ..., description="Base64 Borsh-encoded SignedDelegateAction"
    )


class StellarPayloadContent(BaseModel):
    """
    Stellar payment payload using Soroban Authorization Entries.

    Contains XDR-encoded authorization for SAC (Soroban Asset Contract) transfer.
    User signs auth entry, facilitator wraps in fee-bump transaction.
    """

    from_address: str = Field(..., alias="from", description="G... public key of sender")
    to: str = Field(..., description="G... public key of recipient")
    amount: str = Field(..., description="Amount in stroops (7 decimals)")
    tokenContract: str = Field(..., description="C... Soroban USDC contract address")
    authorizationEntryXdr: str = Field(
        ..., description="Base64 XDR-encoded SorobanAuthorizationEntry"
    )
    nonce: int = Field(..., description="Random 64-bit nonce")
    signatureExpirationLedger: int = Field(
        ..., description="Ledger number when authorization expires"
    )

    class Config:
        populate_by_name = True


class SuiPayloadContent(BaseModel):
    """
    Sui payment payload using sponsored transactions.

    Contains a user-signed programmable transaction that the facilitator sponsors.
    The facilitator pays gas (in SUI), user pays ZERO SUI.

    Transaction Flow:
    1. User creates a programmable transaction for token transfer
    2. User signs the transaction (setSender + setGasOwner for sponsorship)
    3. Transaction is sent to facilitator with sender signature
    4. Facilitator adds sponsor signature and pays gas
    5. Facilitator submits to Sui network

    Required Fields (all mandatory for facilitator deserialization):
    - transactionBytes: BCS-encoded TransactionData
    - senderSignature: User's Ed25519 or Secp256k1 signature
    - from: Sender Sui address (0x + 64 hex)
    - to: Recipient Sui address (0x + 64 hex)
    - amount: Transfer amount in base units
    - coinObjectId: The Sui coin object ID used for the transfer (CRITICAL!)
    """

    transactionBytes: str = Field(
        ..., description="Base64-encoded BCS serialized TransactionData"
    )
    senderSignature: str = Field(
        ..., description="Base64-encoded user signature (Ed25519 or Secp256k1)"
    )
    from_address: str = Field(
        ..., alias="from", description="Sender Sui address (0x + 64 hex chars)"
    )
    to: str = Field(
        ..., description="Recipient Sui address (0x + 64 hex chars)"
    )
    amount: str = Field(
        ..., description="Amount in token base units (e.g., '1000000' for 1 USDC)"
    )
    coinObjectId: str = Field(
        ..., description="Sui coin object ID used for the transfer (REQUIRED by facilitator)"
    )

    class Config:
        populate_by_name = True


# Union type for all payload contents
PayloadContent = Union[
    EVMPayloadContent,
    SVMPayloadContent,
    NEARPayloadContent,
    StellarPayloadContent,
    SuiPayloadContent,
]


class PaymentPayload(BaseModel):
    """
    Complete x402 payment payload as received in X-PAYMENT header (after base64 decoding).

    Supports both x402 v1 and v2 protocols:
    - v1: network as string ("base", "solana")
    - v2: network as CAIP-2 ("eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")

    The payload format varies by network type:
    - EVM: Contains signature + authorization (EIP-712)
    - SVM: Contains partially-signed transaction (Solana, Fogo, etc.)
    - NEAR: Contains SignedDelegateAction (NEP-366)
    - Stellar: Contains Soroban authorization entry XDR
    """

    x402Version: int = Field(default=1, description="x402 protocol version (1 or 2)")
    scheme: Literal["exact"] = Field(
        default="exact", description="Payment scheme (only 'exact' supported)"
    )
    network: str = Field(..., description="Network identifier (v1: 'base', v2: 'eip155:8453')")
    payload: Dict[str, Any] = Field(..., description="Network-specific payload content")

    @field_validator("x402Version")
    @classmethod
    def validate_version(cls, v: int) -> int:
        if v not in (1, 2):
            raise ValueError(f"Unsupported x402 version: {v}. Expected: 1 or 2")
        return v

    @field_validator("scheme")
    @classmethod
    def validate_scheme(cls, v: str) -> str:
        if v != "exact":
            raise ValueError(f"Unsupported scheme: {v}. Only 'exact' is supported")
        return v

    def is_v2(self) -> bool:
        """Check if this payload uses x402 v2 format."""
        # v2 uses CAIP-2 format (contains colon) OR explicitly sets version 2
        return self.x402Version == 2 or ":" in self.network

    def get_normalized_network(self) -> str:
        """
        Get the network name in v1 format (normalized).

        Handles both v1 ("base") and v2 CAIP-2 ("eip155:8453") formats.

        Returns:
            Normalized network name (e.g., "base", "solana")
        """
        from uvd_x402_sdk.networks.base import normalize_network
        return normalize_network(self.network)

    def get_evm_payload(self) -> EVMPayloadContent:
        """Parse payload as EVM format."""
        return EVMPayloadContent(**self.payload)

    def get_svm_payload(self) -> SVMPayloadContent:
        """Parse payload as SVM format (Solana, Fogo, etc.)."""
        return SVMPayloadContent(**self.payload)

    def get_solana_payload(self) -> SolanaPayloadContent:
        """Parse payload as Solana format (alias for get_svm_payload)."""
        return self.get_svm_payload()

    def get_near_payload(self) -> NEARPayloadContent:
        """Parse payload as NEAR format."""
        return NEARPayloadContent(**self.payload)

    def get_stellar_payload(self) -> StellarPayloadContent:
        """Parse payload as Stellar format."""
        return StellarPayloadContent(**self.payload)

    def get_sui_payload(self) -> SuiPayloadContent:
        """Parse payload as Sui format (sponsored transaction)."""
        return SuiPayloadContent(**self.payload)


class PaymentRequirements(BaseModel):
    """
    Payment requirements sent to the facilitator for verify/settle operations.

    This tells the facilitator what payment parameters to validate.
    Supports both x402 v1 and v2 formats.
    """

    scheme: Literal["exact"] = Field(default="exact")
    network: str = Field(..., description="Network identifier (v1 or CAIP-2)")
    maxAmountRequired: str = Field(..., description="Expected amount in token base units")
    resource: str = Field(..., description="Resource being purchased (URL or description)")
    description: str = Field(..., description="Human-readable description of purchase")
    mimeType: str = Field(default="application/json")
    payTo: str = Field(..., description="Recipient address for the payment")
    maxTimeoutSeconds: int = Field(default=60, description="Max settlement timeout")
    asset: str = Field(..., description="Token contract address/identifier")
    extra: Optional[Dict[str, str]] = Field(
        default=None, description="EIP-712 domain params for EVM chains"
    )


# =============================================================================
# x402 v2 Models
# =============================================================================


class PaymentOption(BaseModel):
    """
    Single payment option in x402 v2 accepts array.

    Represents one way the client can pay for the resource.
    """

    network: str = Field(..., description="CAIP-2 network identifier (e.g., 'eip155:8453')")
    asset: str = Field(..., description="Token contract/mint address")
    amount: str = Field(..., description="Required amount in token base units")
    payTo: str = Field(..., description="Recipient address for this network")
    extra: Optional[Dict[str, Any]] = Field(
        default=None, description="Network-specific extra data"
    )


class PaymentRequirementsV2(BaseModel):
    """
    x402 v2 payment requirements with multiple payment options.

    The 'accepts' array allows servers to offer multiple ways to pay,
    and clients can choose based on their wallet/network preferences.
    """

    x402Version: int = Field(default=2, description="Protocol version (must be 2)")
    scheme: Literal["exact"] = Field(default="exact")
    resource: str = Field(..., description="Resource being purchased")
    description: str = Field(..., description="Human-readable description")
    mimeType: str = Field(default="application/json")
    maxTimeoutSeconds: int = Field(default=60)
    accepts: List[PaymentOption] = Field(
        ..., description="List of acceptable payment options"
    )

    def get_option_for_network(self, network: str) -> Optional[PaymentOption]:
        """
        Get the payment option for a specific network.

        Args:
            network: Network identifier (v1 or CAIP-2)

        Returns:
            PaymentOption if found, None otherwise
        """
        from uvd_x402_sdk.networks.base import normalize_network, to_caip2_network

        # Normalize the requested network
        try:
            normalized = normalize_network(network)
        except ValueError:
            return None

        for option in self.accepts:
            try:
                option_normalized = normalize_network(option.network)
                if option_normalized == normalized:
                    return option
            except ValueError:
                continue

        return None

    def get_supported_networks(self) -> List[str]:
        """
        Get list of supported networks (normalized names).

        Returns:
            List of network names (e.g., ['base', 'solana', 'near'])
        """
        from uvd_x402_sdk.networks.base import normalize_network

        networks = []
        for option in self.accepts:
            try:
                networks.append(normalize_network(option.network))
            except ValueError:
                continue
        return networks


class VerifyRequest(BaseModel):
    """
    Request body for facilitator /verify endpoint.
    """

    x402Version: int = 1
    paymentPayload: PaymentPayload
    paymentRequirements: PaymentRequirements


class VerifyResponse(BaseModel):
    """
    Response from facilitator /verify endpoint.
    """

    isValid: bool = Field(..., description="Whether the payment signature is valid")
    payer: Optional[str] = Field(None, description="Verified payer address")
    message: Optional[str] = Field(None, description="Error message if invalid")
    invalidReason: Optional[str] = Field(None, description="Specific reason for invalidity")
    errors: List[str] = Field(default_factory=list, description="List of validation errors")


class SettleRequest(BaseModel):
    """
    Request body for facilitator /settle endpoint.
    """

    x402Version: int = 1
    paymentPayload: PaymentPayload
    paymentRequirements: PaymentRequirements


class SettleResponse(BaseModel):
    """
    Response from facilitator /settle endpoint.
    """

    success: bool = Field(..., description="Whether settlement succeeded")
    transaction: Optional[str] = Field(None, description="Transaction hash on-chain")
    tx_hash: Optional[str] = Field(None, description="Alternative field for tx hash")
    payer: Optional[str] = Field(None, description="Verified payer address")
    message: Optional[str] = Field(None, description="Error message if failed")
    errors: List[str] = Field(default_factory=list, description="List of errors")

    def get_transaction_hash(self) -> Optional[str]:
        """Get transaction hash from either field."""
        return self.transaction or self.tx_hash


class PaymentResult(BaseModel):
    """
    Final result of a successful payment processing.

    This is the return type from X402Client.process_payment().
    """

    success: bool = Field(default=True)
    payer_address: str = Field(..., description="Verified wallet address that paid")
    transaction_hash: Optional[str] = Field(None, description="On-chain transaction hash")
    network: str = Field(..., description="Network where payment was settled")
    amount_usd: Decimal = Field(..., description="Amount paid in USD")

    class Config:
        json_encoders = {Decimal: str}


class Payment402Response(BaseModel):
    """
    Standard 402 Payment Required response body.

    This is returned to clients when payment is required.
    """

    error: str = Field(default="Payment required")
    recipient: str = Field(..., description="Default recipient address (EVM)")
    recipients: Optional[Dict[str, str]] = Field(
        None,
        description="Network-specific recipients (evm, solana, near, stellar)",
    )
    facilitator: Optional[str] = Field(
        None, description="Solana facilitator address (fee payer)"
    )
    amount: str = Field(..., description="Amount required in USD")
    token: str = Field(default="USDC")
    supportedChains: List[Union[int, str]] = Field(
        ..., description="List of supported chain IDs or network names"
    )
    message: str = Field(..., description="Human-readable message")

    class Config:
        json_encoders = {Decimal: str}
