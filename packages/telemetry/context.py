"""W3C trace-context and request-correlation helpers."""

from collections.abc import Mapping, MutableMapping
from contextvars import ContextVar, Token

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_trace_context = TraceContextTextMapPropagator()


def bind_request_id(request_id: str) -> Token[str | None]:
    """Bind a validated request ID to the current async context."""

    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Restore the request ID context after a request completes."""

    _request_id.reset(token)


def get_request_id() -> str | None:
    """Return the request ID associated with the current execution context."""

    return _request_id.get()


def current_trace_id() -> str | None:
    """Return the active W3C trace ID as 32 lowercase hex characters."""

    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return trace.format_trace_id(span_context.trace_id)


def current_span_id() -> str | None:
    """Return the active span ID as 16 lowercase hex characters."""

    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return trace.format_span_id(span_context.span_id)


def inject_trace_context(headers: MutableMapping[str, str]) -> None:
    """Inject only the current W3C ``traceparent`` context into HTTP headers."""

    _trace_context.inject(headers)


def extract_trace_context(headers: Mapping[str, str]) -> Context:
    """Extract a remote W3C trace context from HTTP headers."""

    return _trace_context.extract(headers)


def attach_trace_context(context: Context) -> Token[Context]:
    """Attach an extracted context and return the opaque detach token."""

    return otel_context.attach(context)


def detach_trace_context(token: Token[Context]) -> None:
    """Detach a context previously returned by :func:`attach_trace_context`."""

    otel_context.detach(token)
