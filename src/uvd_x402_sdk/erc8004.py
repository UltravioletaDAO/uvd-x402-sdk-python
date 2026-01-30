"""
ERC-8004 Trustless Agents client for x402 SDK.

This module provides integration with the ERC-8004 reputation system,
enabling agents to build verifiable reputation through on-chain feedback.

Features:
- Query agent identity from Identity Registry
- Query agent reputation from Reputation Registry
- Submit feedback with proof of payment
- Revoke feedback

Example:
    >>> from uvd_x402_sdk.erc8004 import Erc8004Client
    >>>
    >>> client = Erc8004Client()
    >>>
    >>> # Get agent identity
    >>> identity = await client.get_identity("ethereum", 42)
    >>> print(f"Agent: {identity.agent_uri}")
    >>>
    >>> # Get agent reputation
    >>> reputation = await client.get_reputation("ethereum", 42)
    >>> print(f"Score: {reputation.summary.summary_value}")
    >>>
    >>> # Submit feedback after payment
    >>> result = await client.submit_feedback(
    ...     network="ethereum",
    ...     agent_id=42,
    ...     value=95,
    ...     tag1="quality",
    ...     proof=settle_response.proof_of_payment,
    ... )
"""

from enum import Enum
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field

# ERC-8004 extension identifier
ERC8004_EXTENSION_ID = "8004-reputation"

# Supported networks for ERC-8004
Erc8004Network = Literal["ethereum", "ethereum-sepolia"]


class Erc8004ContractAddresses(BaseModel):
    """Contract addresses for ERC-8004 on a network."""

    identity_registry: Optional[str] = None
    reputation_registry: Optional[str] = None
    validation_registry: Optional[str] = None


# Contract addresses per network
ERC8004_CONTRACTS: dict[str, Erc8004ContractAddresses] = {
    "ethereum": Erc8004ContractAddresses(
        identity_registry="0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
        reputation_registry="0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
    ),
    "ethereum-sepolia": Erc8004ContractAddresses(
        identity_registry="0x8004A818BFB912233c491871b3d84c89A494BD9e",
        reputation_registry="0x8004B663056A597Dffe9eCcC1965A193B7388713",
        validation_registry="0x8004Cb1BF31DAf7788923b405b754f57acEB4272",
    ),
}


class ProofOfPayment(BaseModel):
    """
    Cryptographic proof of a settled payment for reputation submission.

    This proof is returned when settling with the 8004-reputation extension
    and is required for submitting authorized feedback.
    """

    transaction_hash: str = Field(..., alias="transactionHash")
    block_number: int = Field(..., alias="blockNumber")
    network: str
    payer: str
    payee: str
    amount: str
    token: str
    timestamp: int
    payment_hash: str = Field(..., alias="paymentHash")

    class Config:
        populate_by_name = True


class AgentService(BaseModel):
    """Agent service entry."""

    name: str
    endpoint: str
    version: Optional[str] = None


class AgentRegistration(BaseModel):
    """Agent registration reference."""

    agent_id: int = Field(..., alias="agentId")
    agent_registry: str = Field(..., alias="agentRegistry")

    class Config:
        populate_by_name = True


class AgentIdentity(BaseModel):
    """Agent identity information from the Identity Registry."""

    agent_id: int = Field(..., alias="agentId")
    owner: str
    agent_uri: str = Field(..., alias="agentUri")
    agent_wallet: Optional[str] = Field(None, alias="agentWallet")
    network: str

    class Config:
        populate_by_name = True


class AgentRegistrationFile(BaseModel):
    """Agent registration file structure (resolved from agentURI)."""

    type_: str = Field("https://eips.ethereum.org/EIPS/eip-8004#agent-v1", alias="type")
    name: str
    description: str
    image: Optional[str] = None
    services: list[AgentService] = Field(default_factory=list)
    x402_support: bool = Field(False, alias="x402Support")
    active: bool = True
    registrations: list[AgentRegistration] = Field(default_factory=list)
    supported_trust: list[str] = Field(default_factory=list, alias="supportedTrust")

    class Config:
        populate_by_name = True


class ReputationSummary(BaseModel):
    """Reputation summary for an agent."""

    agent_id: int = Field(..., alias="agentId")
    count: int
    summary_value: int = Field(..., alias="summaryValue")
    summary_value_decimals: int = Field(..., alias="summaryValueDecimals")
    network: str

    class Config:
        populate_by_name = True


class FeedbackEntry(BaseModel):
    """Individual feedback entry."""

    client: str
    feedback_index: int = Field(..., alias="feedbackIndex")
    value: int
    value_decimals: int = Field(..., alias="valueDecimals")
    tag1: str
    tag2: str
    is_revoked: bool = Field(..., alias="isRevoked")

    class Config:
        populate_by_name = True


class ReputationResponse(BaseModel):
    """Reputation query response."""

    agent_id: int = Field(..., alias="agentId")
    summary: ReputationSummary
    feedback: Optional[list[FeedbackEntry]] = None
    network: str

    class Config:
        populate_by_name = True


class FeedbackParams(BaseModel):
    """Parameters for submitting reputation feedback."""

    agent_id: int = Field(..., alias="agentId")
    value: int
    value_decimals: int = Field(0, alias="valueDecimals")
    tag1: str = ""
    tag2: str = ""
    endpoint: str = ""
    feedback_uri: str = Field("", alias="feedbackUri")
    feedback_hash: Optional[str] = Field(None, alias="feedbackHash")
    proof: Optional[ProofOfPayment] = None

    class Config:
        populate_by_name = True


class FeedbackRequest(BaseModel):
    """Feedback request body for POST /feedback."""

    x402_version: int = Field(1, alias="x402Version")
    network: str
    feedback: FeedbackParams

    class Config:
        populate_by_name = True


class FeedbackResponse(BaseModel):
    """Feedback response from POST /feedback."""

    success: bool
    transaction: Optional[str] = None
    feedback_index: Optional[int] = Field(None, alias="feedbackIndex")
    error: Optional[str] = None
    network: str

    class Config:
        populate_by_name = True


class SettleResponseWithProof(BaseModel):
    """Extended settle response with ERC-8004 proof of payment."""

    success: bool
    transaction_hash: Optional[str] = Field(None, alias="transactionHash")
    network: Optional[str] = None
    error: Optional[str] = None
    payer: Optional[str] = None
    proof_of_payment: Optional[ProofOfPayment] = Field(None, alias="proofOfPayment")

    class Config:
        populate_by_name = True


class Erc8004Client:
    """
    Client for ERC-8004 Trustless Agents API.

    Provides methods for:
    - Querying agent identity
    - Querying agent reputation
    - Submitting reputation feedback
    - Revoking feedback

    Example:
        >>> client = Erc8004Client()
        >>>
        >>> # Get agent identity
        >>> identity = await client.get_identity("ethereum", 42)
        >>> print(identity.agent_uri)
        >>>
        >>> # Get agent reputation
        >>> reputation = await client.get_reputation("ethereum", 42)
        >>> print(f"Score: {reputation.summary.summary_value}")
        >>>
        >>> # Submit feedback after payment
        >>> result = await client.submit_feedback(
        ...     network="ethereum",
        ...     agent_id=42,
        ...     value=95,
        ...     tag1="quality",
        ...     proof=settle_response.proof_of_payment,
        ... )
    """

    def __init__(
        self,
        base_url: str = "https://facilitator.ultravioletadao.xyz",
        timeout: float = 30.0,
    ):
        """
        Initialize the ERC-8004 client.

        Args:
            base_url: Base URL of the facilitator API
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "Erc8004Client":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.aclose()

    async def get_identity(
        self,
        network: Erc8004Network,
        agent_id: int,
    ) -> AgentIdentity:
        """
        Get agent identity from the Identity Registry.

        Args:
            network: Network where agent is registered
            agent_id: Agent's tokenId

        Returns:
            Agent identity information

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/identity/{network}/{agent_id}"
        response = await self._client.get(url)
        response.raise_for_status()
        return AgentIdentity.model_validate(response.json())

    async def resolve_agent_uri(self, agent_uri: str) -> AgentRegistrationFile:
        """
        Resolve agent registration file from agentURI.

        Args:
            agent_uri: URI pointing to agent registration file

        Returns:
            Resolved agent registration file

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        # Handle IPFS URIs
        url = agent_uri
        if agent_uri.startswith("ipfs://"):
            cid = agent_uri.replace("ipfs://", "")
            url = f"https://ipfs.io/ipfs/{cid}"

        response = await self._client.get(url)
        response.raise_for_status()
        return AgentRegistrationFile.model_validate(response.json())

    async def get_reputation(
        self,
        network: Erc8004Network,
        agent_id: int,
        *,
        tag1: Optional[str] = None,
        tag2: Optional[str] = None,
        include_feedback: bool = False,
    ) -> ReputationResponse:
        """
        Get agent reputation from the Reputation Registry.

        Args:
            network: Network where agent is registered
            agent_id: Agent's tokenId
            tag1: Filter by primary tag
            tag2: Filter by secondary tag
            include_feedback: Include individual feedback entries

        Returns:
            Reputation summary and optionally individual feedback entries

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        params: dict[str, Any] = {}
        if tag1:
            params["tag1"] = tag1
        if tag2:
            params["tag2"] = tag2
        if include_feedback:
            params["includeFeedback"] = "true"

        url = f"{self.base_url}/reputation/{network}/{agent_id}"
        response = await self._client.get(url, params=params or None)
        response.raise_for_status()
        return ReputationResponse.model_validate(response.json())

    async def submit_feedback(
        self,
        network: Erc8004Network,
        agent_id: int,
        value: int,
        *,
        value_decimals: int = 0,
        tag1: str = "",
        tag2: str = "",
        endpoint: str = "",
        feedback_uri: str = "",
        feedback_hash: Optional[str] = None,
        proof: Optional[ProofOfPayment] = None,
        x402_version: int = 1,
    ) -> FeedbackResponse:
        """
        Submit reputation feedback for an agent.

        Requires proof of payment for authorized feedback submission.

        Args:
            network: Network where feedback will be submitted
            agent_id: Agent's tokenId
            value: Feedback value (e.g., 95 for 95/100)
            value_decimals: Decimal places for value interpretation (0-18)
            tag1: Primary categorization tag
            tag2: Secondary categorization tag
            endpoint: Service endpoint that was used
            feedback_uri: URI to off-chain feedback file
            feedback_hash: Keccak256 hash of feedback content
            proof: Proof of payment (required for authorized feedback)
            x402_version: x402 protocol version

        Returns:
            Feedback response with transaction hash

        Example:
            >>> # After settling a payment with ERC-8004 extension
            >>> result = await client.submit_feedback(
            ...     network="ethereum",
            ...     agent_id=42,
            ...     value=95,
            ...     tag1="quality",
            ...     proof=settle_response.proof_of_payment,
            ... )
        """
        request = FeedbackRequest(
            x402_version=x402_version,
            network=network,
            feedback=FeedbackParams(
                agent_id=agent_id,
                value=value,
                value_decimals=value_decimals,
                tag1=tag1,
                tag2=tag2,
                endpoint=endpoint,
                feedback_uri=feedback_uri,
                feedback_hash=feedback_hash,
                proof=proof,
            ),
        )

        url = f"{self.base_url}/feedback"
        try:
            response = await self._client.post(
                url,
                json=request.model_dump(by_alias=True, exclude_none=True),
            )
            response.raise_for_status()
            return FeedbackResponse.model_validate(response.json())
        except httpx.HTTPStatusError as e:
            return FeedbackResponse(
                success=False,
                error=f"Facilitator error: {e.response.status_code} - {e.response.text}",
                network=network,
            )
        except Exception as e:
            return FeedbackResponse(
                success=False,
                error=str(e),
                network=network,
            )

    async def revoke_feedback(
        self,
        network: Erc8004Network,
        agent_id: int,
        feedback_index: int,
        *,
        x402_version: int = 1,
    ) -> FeedbackResponse:
        """
        Revoke previously submitted feedback.

        Only the original submitter can revoke their feedback.

        Args:
            network: Network where feedback was submitted
            agent_id: Agent ID
            feedback_index: Index of feedback to revoke
            x402_version: x402 protocol version

        Returns:
            Revocation result
        """
        url = f"{self.base_url}/feedback/revoke"
        try:
            response = await self._client.post(
                url,
                json={
                    "x402Version": x402_version,
                    "network": network,
                    "agentId": agent_id,
                    "feedbackIndex": feedback_index,
                },
            )
            response.raise_for_status()
            return FeedbackResponse.model_validate(response.json())
        except httpx.HTTPStatusError as e:
            return FeedbackResponse(
                success=False,
                error=f"Facilitator error: {e.response.status_code} - {e.response.text}",
                network=network,
            )
        except Exception as e:
            return FeedbackResponse(
                success=False,
                error=str(e),
                network=network,
            )

    def get_contracts(self, network: Erc8004Network) -> Optional[Erc8004ContractAddresses]:
        """
        Get ERC-8004 contract addresses for a network.

        Args:
            network: Network to get contracts for

        Returns:
            Contract addresses or None if not deployed
        """
        return ERC8004_CONTRACTS.get(network)

    def is_available(self, network: str) -> bool:
        """
        Check if ERC-8004 is available on a network.

        Args:
            network: Network to check

        Returns:
            True if ERC-8004 contracts are deployed
        """
        return network in ERC8004_CONTRACTS

    async def get_feedback_metadata(self) -> dict[str, Any]:
        """
        Get feedback endpoint metadata.

        Returns:
            Endpoint information for /feedback
        """
        url = f"{self.base_url}/feedback"
        response = await self._client.get(url)
        response.raise_for_status()
        return response.json()

    async def append_response(
        self,
        network: Erc8004Network,
        agent_id: int,
        feedback_index: int,
        response_text: str,
        *,
        response_uri: Optional[str] = None,
        x402_version: int = 1,
    ) -> FeedbackResponse:
        """
        Append a response to existing feedback.

        Allows agents to respond to feedback they received.
        Only the agent (identity owner) can append responses.

        Args:
            network: Network where feedback was submitted
            agent_id: Agent ID
            feedback_index: Index of feedback to respond to
            response_text: Response content
            response_uri: Optional URI to off-chain response file
            x402_version: x402 protocol version

        Returns:
            Response result

        Example:
            >>> # Agent responds to feedback
            >>> result = await client.append_response(
            ...     network="ethereum",
            ...     agent_id=42,
            ...     feedback_index=1,
            ...     response_text="Thank you for your feedback!",
            ... )
        """
        url = f"{self.base_url}/feedback/response"
        payload: dict[str, Any] = {
            "x402Version": x402_version,
            "network": network,
            "agentId": agent_id,
            "feedbackIndex": feedback_index,
            "response": response_text,
        }
        if response_uri:
            payload["responseUri"] = response_uri

        try:
            response = await self._client.post(url, json=payload)
            response.raise_for_status()
            return FeedbackResponse.model_validate(response.json())
        except httpx.HTTPStatusError as e:
            return FeedbackResponse(
                success=False,
                error=f"Facilitator error: {e.response.status_code} - {e.response.text}",
                network=network,
            )
        except Exception as e:
            return FeedbackResponse(
                success=False,
                error=str(e),
                network=network,
            )


def build_erc8004_payment_requirements(
    amount: str,
    recipient: str,
    resource: str,
    *,
    network: str = "base",
    description: str = "Payment for resource access",
    mime_type: str = "application/json",
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """
    Build payment requirements with ERC-8004 extension.

    Adds the 8004-reputation extension to include proof of payment
    in settlement responses for reputation submission.

    Args:
        amount: Amount in human-readable format (e.g., "1.00")
        recipient: Recipient address
        resource: Resource URL being protected
        network: Chain name (e.g., "base", "ethereum")
        description: Description of the resource
        mime_type: MIME type of the resource
        timeout_seconds: Maximum timeout in seconds

    Returns:
        Payment requirements with ERC-8004 extension

    Example:
        >>> requirements = build_erc8004_payment_requirements(
        ...     amount="1.00",
        ...     recipient="0x...",
        ...     resource="https://api.example.com/service",
        ...     network="ethereum",
        ... )
        >>> # Settlement will include proofOfPayment
        >>> result = await facilitator.settle(payment, requirements)
        >>> print(result.proof_of_payment)
    """
    return {
        "scheme": "exact",
        "network": network,
        "maxAmountRequired": str(int(float(amount) * 1_000_000)),  # 6 decimals
        "resource": resource,
        "description": description,
        "mimeType": mime_type,
        "payTo": recipient,
        "maxTimeoutSeconds": timeout_seconds,
        "extra": {
            ERC8004_EXTENSION_ID: {
                "includeProof": True,
            },
        },
    }
