import functools
import logging
import os
from typing import Any, Callable, Dict, Optional, Union

import datadog
from ddtrace import Span, tracer
from ddtrace.constants import SPAN_MEASURED_KEY

_service_env_var = os.getenv("DD_SERVICE") or "unknown"

# Used for Datadog trace error tracking
# See https://docs.datadoghq.com/tracing/trace_collection/tracing_naming_convention/#errors
ERROR_MESSAGE_TAG = "error.message"
ERROR_TYPE_TAG = "error.type"
ERROR_STACK_TAG = "error.stack"

USE_MONITORING = os.getenv("DD_ENV", "").lower() in ["prod", "dev"] and bool(
    os.getenv("USE_DETECTION_HELPER_MONITORING")
)
logging.info("panther_detection_helpers.monitoring DD_ENV = %s", os.getenv("DD_ENV", ""))
logging.info(
    "panther_detection_helpers.monitoring USE_DETECTION_HELPER_MONITORING = %s",
    os.getenv("USE_DETECTION_HELPER_MONITORING"),
)
logging.info("panther_detection_helpers.monitoring USE_MONITORING = %s", USE_MONITORING)


# pylint: disable=too-many-arguments
def trace(
    name: str,
    measured: bool = False,
    service: Optional[str] = None,
    resource: Optional[str] = None,
    span_type: Optional[str] = None,
    tags: Optional[Dict[Union[str, bytes], str]] = None,
) -> Span:
    """
    trace wraps the ddtrace tracer.trace function and adds the lambda name as the service if not set
    and adds the SPAN_MEASURED_KEY tag to the span, which is required to generate metrics for the span.
    The returned span must be finished with either finish_span() below, or context management.

        span = trace("my_operation_name", resource="some_resource", tags={"my_tag": "my_value"})
        ...
        finish_span(span)
    or
        with trace("my_operation_name") as span:
            ...do some things...
            ...span is automatically finished when context exits...
    """
    if service is None:
        service = _service_env_var

    span = tracer.trace(name=name, service=service, resource=resource, span_type=span_type)

    if tags is None:
        tags = {}

    if measured:
        tags[SPAN_MEASURED_KEY] = "true"

    span.set_tags(tags)

    return span


def finish_span(
    span: Span,
    error_message: Optional[str] = None,
    error_type: Optional[str] = None,
    error_stack: Optional[str] = None,
    tags: Optional[Dict[Union[str, bytes], str]] = None,
) -> None:
    """
    finish_span wraps the ddtrace Span.finish function and adds the error message, type and stack if set.
        span = trace("my_operation_name", resource="some_resource", tags={"my_tag": "my_value"})
        ...
        finish_span(span, error_message="my_error_message", error_type="network_error", error_stack=some_stack_trace)
    """
    error_tags: Dict[Union[str, bytes], str] = {}
    if error_message:
        error_tags[ERROR_MESSAGE_TAG] = error_message
    if error_type:
        error_tags[ERROR_TYPE_TAG] = error_type
    if error_stack:
        error_tags[ERROR_STACK_TAG] = error_stack

    if error_tags:
        span.error = 1
        span.set_tags(error_tags)
    if tags:
        span.set_tags(tags)

    span.finish()


# pylint: disable=too-many-arguments
def wrap(
    name: str,
    measured: bool = False,
    service: Optional[str] = None,
    resource: Optional[str] = None,
    span_type: Optional[str] = None,
    tags: Optional[Dict[Union[str, bytes], str]] = None,
) -> Callable[..., Any]:
    """
    wrap is a function decorator to trace a function and adds logging.
    If Datadog is not enabled, no tracing or logging is done.
    Setting `measured` to true adds the SPAN_MEASURED_KEY tag to the span, which is required to generate metrics for the span.
    callers may use the @wrap decorator as follows:
        @wrap(name="span_operation_name")
        def my_function():
          ...
    """

    def plain_wrap_decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def func_wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return func_wrapper

    def dd_wrap_decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def func_wrapper(*args: Any, **kwargs: Any) -> Any:
            extras = {
                "name": name,
                "service": service,
                "resource": resource,
                "span_type": span_type,
                "measured": measured,
                "tags": tags,
            }

            try:
                logging.debug("calling %s", name, extra=extras)

                with trace(
                    name=name,
                    service=service,
                    resource=resource,
                    span_type=span_type,
                    measured=measured,
                    tags=tags,
                ):
                    with datadog.statsd.timed("example_metric.timer"):
                        return func(*args, **kwargs)

            except Exception as err:  # pylint: disable=broad-except
                logging.error(
                    "failed to call kv store caching func %s: %s",
                    name,
                    err,
                    extra=extras | {"error": str(err)},
                )
                raise err

        return func_wrapper

    return dd_wrap_decorator if USE_MONITORING else plain_wrap_decorator
