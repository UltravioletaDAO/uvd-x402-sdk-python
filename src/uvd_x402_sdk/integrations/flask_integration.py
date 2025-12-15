"""
Flask integration for x402 payments.

Provides:
- FlaskX402: Extension for initializing x402 with Flask apps
- flask_require_payment: Decorator for protecting routes
"""

from decimal import Decimal
from functools import wraps
from typing import Any, Callable, Optional, TypeVar, Union

try:
    from flask import Flask, request, jsonify, make_response, g
except ImportError:
    raise ImportError(
        "Flask is required for Flask integration. "
        "Install with: pip install uvd-x402-sdk[flask]"
    )

from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.exceptions import X402Error
from uvd_x402_sdk.response import create_402_response, create_402_headers

F = TypeVar("F", bound=Callable[..., Any])


class FlaskX402:
    """
    Flask extension for x402 payment integration.

    Example:
        >>> from flask import Flask
        >>> from uvd_x402_sdk.integrations import FlaskX402
        >>>
        >>> app = Flask(__name__)
        >>> x402 = FlaskX402(app, recipient_address="0xYourWallet...")
        >>>
        >>> @app.route("/premium")
        >>> @x402.require_payment(amount_usd=Decimal("1.00"))
        >>> def premium():
        ...     return {"message": f"Paid by {g.payment_result.payer_address}"}
    """

    def __init__(
        self,
        app: Optional[Flask] = None,
        config: Optional[X402Config] = None,
        recipient_address: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize Flask x402 extension.

        Args:
            app: Flask application (optional, can use init_app later)
            config: X402Config object
            recipient_address: Default recipient for EVM chains
            **kwargs: Additional config parameters
        """
        self.app = app
        self._config = config
        self._config_kwargs = {"recipient_evm": recipient_address, **kwargs}
        self._client: Optional[X402Client] = None

        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        """
        Initialize extension with Flask app.

        Args:
            app: Flask application
        """
        self.app = app

        # Get config from app config if not provided
        if self._config is None:
            self._config = X402Config(
                facilitator_url=app.config.get(
                    "X402_FACILITATOR_URL",
                    "https://facilitator.ultravioletadao.xyz",
                ),
                recipient_evm=app.config.get("X402_RECIPIENT_EVM", "")
                or self._config_kwargs.get("recipient_evm", ""),
                recipient_solana=app.config.get("X402_RECIPIENT_SOLANA", ""),
                recipient_near=app.config.get("X402_RECIPIENT_NEAR", ""),
                recipient_stellar=app.config.get("X402_RECIPIENT_STELLAR", ""),
            )

        self._client = X402Client(config=self._config)

        # Store extension on app
        if not hasattr(app, "extensions"):
            app.extensions = {}
        app.extensions["x402"] = self

    @property
    def client(self) -> X402Client:
        """Get the x402 client."""
        if self._client is None:
            raise RuntimeError("FlaskX402 not initialized. Call init_app() first.")
        return self._client

    @property
    def config(self) -> X402Config:
        """Get the x402 config."""
        if self._config is None:
            raise RuntimeError("FlaskX402 not initialized. Call init_app() first.")
        return self._config

    def require_payment(
        self,
        amount_usd: Optional[Union[Decimal, float, str]] = None,
        amount_callback: Optional[Callable[[], Decimal]] = None,
        message: Optional[str] = None,
        store_in_g: bool = True,
    ) -> Callable[[F], F]:
        """
        Decorator that requires payment for a Flask route.

        Args:
            amount_usd: Fixed payment amount in USD
            amount_callback: Callable that returns dynamic amount
            message: Custom message for 402 response
            store_in_g: Store PaymentResult in flask.g.payment_result

        Returns:
            Decorated route function

        Example:
            >>> @app.route("/api/premium")
            >>> @x402.require_payment(amount_usd="5.00")
            >>> def premium_endpoint():
            ...     return {"payer": g.payment_result.payer_address}
        """

        def decorator(func: F) -> F:
            @wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                # Get payment header
                payment_header = request.headers.get("X-PAYMENT")

                # Determine amount
                if amount_callback:
                    required_amount = Decimal(str(amount_callback()))
                elif amount_usd:
                    required_amount = Decimal(str(amount_usd))
                else:
                    raise ValueError("Either amount_usd or amount_callback is required")

                # No payment header - return 402
                if not payment_header:
                    response_body = create_402_response(
                        amount_usd=required_amount,
                        config=self.config,
                        message=message,
                    )
                    response = make_response(jsonify(response_body), 402)
                    for key, value in create_402_headers().items():
                        response.headers[key] = value
                    return response

                # Process payment
                try:
                    result = self.client.process_payment(
                        x_payment_header=payment_header,
                        expected_amount_usd=required_amount,
                    )

                    # Store result in g
                    if store_in_g:
                        g.payment_result = result

                    return func(*args, **kwargs)

                except X402Error as e:
                    response_body = create_402_response(
                        amount_usd=required_amount,
                        config=self.config,
                        message=message,
                    )
                    response_body["error"] = e.message
                    response_body["details"] = e.details
                    response = make_response(jsonify(response_body), 402)
                    for key, value in create_402_headers().items():
                        response.headers[key] = value
                    return response

            return wrapper  # type: ignore

        return decorator


def flask_require_payment(
    amount_usd: Union[Decimal, float, str],
    message: Optional[str] = None,
) -> Callable[[F], F]:
    """
    Standalone decorator using global Flask x402 extension.

    This decorator uses the x402 extension stored in current_app.extensions.

    Example:
        >>> @app.route("/api/resource")
        >>> @flask_require_payment(amount_usd="1.00")
        >>> def resource():
        ...     return {"success": True}
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from flask import current_app, g

            # Get extension from app
            if "x402" not in current_app.extensions:
                raise RuntimeError(
                    "FlaskX402 not initialized. "
                    "Add FlaskX402(app, ...) to your app setup."
                )

            x402_ext: FlaskX402 = current_app.extensions["x402"]
            required_amount = Decimal(str(amount_usd))

            # Get payment header
            payment_header = request.headers.get("X-PAYMENT")

            # No payment - return 402
            if not payment_header:
                response_body = create_402_response(
                    amount_usd=required_amount,
                    config=x402_ext.config,
                    message=message,
                )
                response = make_response(jsonify(response_body), 402)
                for key, value in create_402_headers().items():
                    response.headers[key] = value
                return response

            # Process payment
            try:
                result = x402_ext.client.process_payment(
                    x_payment_header=payment_header,
                    expected_amount_usd=required_amount,
                )
                g.payment_result = result
                return func(*args, **kwargs)

            except X402Error as e:
                response_body = e.to_dict()
                response = make_response(jsonify(response_body), 402)
                return response

        return wrapper  # type: ignore

    return decorator
