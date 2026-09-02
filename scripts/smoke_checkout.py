"""Bounded Stage 01 checkout smoke/load runner."""

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from uuid import uuid4

import httpx
from pydantic import ValidationError

from packages.models.checkout import CheckoutResponse, PaymentResponse


@dataclass(frozen=True)
class Arguments:
    """Validated smoke runner inputs."""

    gateway_url: str
    payment_url: str
    count: int
    timeout_seconds: float
    readiness_timeout_seconds: float


def _bounded_count(value: str) -> int:
    count = int(value)
    if not 1 <= count <= 20:
        raise argparse.ArgumentTypeError("count must be between 1 and 20")
    return count


def _positive_float(value: str) -> float:
    result = float(value)
    if not 0 < result <= 60:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 60 seconds")
    return result


def parse_arguments() -> Arguments:
    """Parse bounded command-line and smoke-specific environment options."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url",
        default=os.getenv("SMOKE_GATEWAY_URL", "http://127.0.0.1:8001"),
    )
    parser.add_argument(
        "--payment-url",
        default=os.getenv("SMOKE_PAYMENT_URL", "http://127.0.0.1:8004"),
    )
    parser.add_argument(
        "--count",
        type=_bounded_count,
        default=_bounded_count(os.getenv("SMOKE_REQUEST_COUNT", "1")),
        help="number of unique checkouts (1-20); the first is also retried once",
    )
    parser.add_argument("--timeout", type=_positive_float, default=5.0)
    parser.add_argument("--readiness-timeout", type=_positive_float, default=30.0)
    parsed = parser.parse_args()
    return Arguments(
        gateway_url=str(parsed.gateway_url).rstrip("/"),
        payment_url=str(parsed.payment_url).rstrip("/"),
        count=int(parsed.count),
        timeout_seconds=float(parsed.timeout),
        readiness_timeout_seconds=float(parsed.readiness_timeout),
    )


async def wait_until_ready(
    client: httpx.AsyncClient,
    *,
    gateway_url: str,
    deadline_seconds: float,
) -> None:
    """Poll gateway readiness until a fixed monotonic deadline."""

    deadline = time.monotonic() + deadline_seconds
    last_failure = "no response"
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"{gateway_url}/health/ready")
            if response.status_code == 200:
                return
            last_failure = f"HTTP {response.status_code}"
        except httpx.RequestError as error:
            last_failure = type(error).__name__
        await asyncio.sleep(0.5)
    raise RuntimeError(f"gateway did not become ready: {last_failure}")


def _require_success(response: httpx.Response, *, operation: str) -> None:
    if response.is_error:
        raise RuntimeError(f"{operation} failed with HTTP {response.status_code}: {response.text}")


async def run_smoke(arguments: Arguments) -> None:
    """Perform bounded unique checkouts, persistence reads, and one safe retry."""

    timeout = httpx.Timeout(arguments.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        await wait_until_ready(
            client,
            gateway_url=arguments.gateway_url,
            deadline_seconds=arguments.readiness_timeout_seconds,
        )

        for index in range(arguments.count):
            suffix = uuid4().hex
            idempotency_key = f"smoke-{suffix}"
            request_id = f"smoke-request-{suffix}"
            payload = {
                "customer_id": f"smoke-customer-{index}",
                "sku": "widget-001",
                "quantity": 2,
            }
            response = await client.post(
                f"{arguments.gateway_url}/checkout",
                json=payload,
                headers={
                    "Idempotency-Key": idempotency_key,
                    "X-Request-ID": request_id,
                },
            )
            _require_success(response, operation="checkout")
            checkout = CheckoutResponse.model_validate(response.json())
            if (
                checkout.request_id != request_id
                or response.headers.get("X-Request-ID") != request_id
            ):
                raise RuntimeError("checkout did not preserve the request ID")
            if checkout.idempotent_replay:
                raise RuntimeError("a unique checkout was incorrectly marked as a replay")

            read_request_id = f"smoke-read-{suffix}"
            read_response = await client.get(
                f"{arguments.payment_url}/payments/{checkout.payment_id}",
                headers={"X-Request-ID": read_request_id},
            )
            _require_success(read_response, operation="payment lookup")
            persisted = PaymentResponse.model_validate(read_response.json())
            if (
                persisted.request_id != read_request_id
                or read_response.headers.get("X-Request-ID") != read_request_id
            ):
                raise RuntimeError("payment lookup did not preserve the request ID")
            if (persisted.payment_id, persisted.order_id, persisted.total_cents) != (
                checkout.payment_id,
                checkout.order_id,
                checkout.total_cents,
            ):
                raise RuntimeError("persisted payment does not match checkout response")

            print(
                f"checkout={index + 1}/{arguments.count} request_id={request_id} "
                f"payment_id={checkout.payment_id} total_cents={checkout.total_cents}"
            )
            print(
                f"persistence request_id={read_request_id} payment_id={persisted.payment_id} "
                "verified=true"
            )

            if index == 0:
                replay_request_id = f"smoke-replay-{suffix}"
                replay_response = await client.post(
                    f"{arguments.gateway_url}/checkout",
                    json=payload,
                    headers={
                        "Idempotency-Key": idempotency_key,
                        "X-Request-ID": replay_request_id,
                    },
                )
                _require_success(replay_response, operation="idempotent checkout retry")
                replay = CheckoutResponse.model_validate(replay_response.json())
                if (
                    replay.request_id != replay_request_id
                    or replay_response.headers.get("X-Request-ID") != replay_request_id
                    or replay.payment_id != checkout.payment_id
                    or not replay.idempotent_replay
                ):
                    raise RuntimeError("checkout retry was not served from the original payment")
                print(
                    f"replay request_id={replay_request_id} payment_id={replay.payment_id} "
                    "idempotent_replay=true"
                )


def main() -> int:
    """CLI entry point with concise failure output."""

    try:
        asyncio.run(run_smoke(parse_arguments()))
    except (
        OSError,
        RuntimeError,
        ValidationError,
        argparse.ArgumentError,
        httpx.HTTPError,
        ValueError,
    ) as error:
        print(f"smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
