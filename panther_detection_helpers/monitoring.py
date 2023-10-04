import functools
import logging
import os
from typing import Any, Callable, Dict, Optional, Union

import datadog

_service_env_var = os.getenv("DD_SERVICE") or "unknown"

DD_ENV = os.getenv("DD_ENV", "").lower()
USE_DETECTION_HELPER_MONITORING = bool(os.getenv("USE_DETECTION_HELPER_MONITORING"))
USE_MONITORING = DD_ENV in ["prod", "dev"] and USE_DETECTION_HELPER_MONITORING

logging.debug(
    "panther_detection_helpers.monitoring USE_MONITORING",
    extra={
        "DD_ENV": DD_ENV,
        "USE_DETECTION_HELPER_MONITORING": USE_DETECTION_HELPER_MONITORING,
        "USE_MONITORING": USE_MONITORING,
    },
)


def wrap(name: str, tags: Optional[Dict[Union[str, bytes], str]] = None) -> Callable[..., Any]:
    """
    wrap is a function decorator to add metrics to a function and adds logging.
    If Datadog is not enabled, no metrics or logging is done.
    callers may use the @wrap decorator as follows:
        @wrap(name="operation_name")
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
                "op_name": name,
                "tags": tags,
            }

            try:
                logging.debug("calling %s", name, extra=extras)
                with datadog.statsd.timed(f"{name}.execution_time"):
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
