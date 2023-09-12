import json
import os
import time
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence, Set, Union

import boto3

from panther_detection_helpers import tracing

# Helper functions for accessing Dynamo key-value store.
#
# Keys can be any string specified by rules and policies,
# values are integer counters and/or string sets.
#
# Use kv_table() if you want to interact with the table directly.
_KV_TABLE = None
_COUNT_COL = "intCount"
_STRING_SET_COL = "stringSet"
_DICT_COL = "dictionary"
_TTL_COL = "expiresAt"

FIPS_ENABLED = os.getenv("ENABLE_FIPS", "").lower() == "true"
FIPS_SUFFIX = "-fips." + os.getenv("AWS_REGION", "") + ".amazonaws.com"


def kv_table() -> boto3.resource:
    """Lazily build key-value table resource"""
    # pylint: disable=global-statement
    global _KV_TABLE
    if not _KV_TABLE:
        # pylint: disable=no-member
        _KV_TABLE = boto3.resource(
            "dynamodb",
            endpoint_url="https://dynamodb" + FIPS_SUFFIX if FIPS_ENABLED else None,
        ).Table("panther-kv-store")
    return _KV_TABLE


@tracing.wrap(
    name="panther_detection_helpers.caching.get_string_set",
    resource="get_string_set",
    measured=True,
)
def ttl_expired(response: dict) -> bool:
    """Checks whether a response from the panther-kv table has passed it's TTL date"""
    # This can be used when the TTL timing is very exacting and DDB's cleanup is too slow
    expiration = response.get("Item", {}).get(_TTL_COL, 0)
    return expiration and float(expiration) <= (datetime.now()).timestamp()


@tracing.wrap(
    name="panther_detection_helpers.caching.get_counter",
    resource="get_counter",
    measured=True,
)
def get_counter(key: str, force_ttl_check: bool = False) -> int:
    """Get a counter's current value (defaulting to 0 if key does not exist)."""
    response = kv_table().get_item(
        Key={"key": key},
        ProjectionExpression=f"{_COUNT_COL}, {_TTL_COL}",
    )
    if force_ttl_check and ttl_expired(response):
        return 0
    return response.get("Item", {}).get(_COUNT_COL, 0)


@tracing.wrap(
    name="panther_detection_helpers.caching.increment_counter",
    resource="increment_counter",
    measured=True,
)
def increment_counter(key: str, val: int = 1) -> int:
    """Increment a counter in the table.

    Args:
        key: The name of the counter (need not exist yet)
        val: How much to add to the counter

    Returns:
        The new value of the count
    """
    response = kv_table().update_item(
        Key={"key": key},
        ReturnValues="UPDATED_NEW",
        UpdateExpression="ADD #col :incr",
        ExpressionAttributeNames={"#col": _COUNT_COL},
        ExpressionAttributeValues={":incr": val},
    )

    # Numeric values are returned as decimal.Decimal
    return response["Attributes"][_COUNT_COL].to_integral_value()


@tracing.wrap(
    name="panther_detection_helpers.caching.reset_counter",
    resource="reset_counter",
    measured=True,
)
def reset_counter(key: str) -> None:
    """Reset a counter to 0."""
    kv_table().put_item(Item={"key": key, _COUNT_COL: 0})


@tracing.wrap(
    name="panther_detection_helpers.caching.set_key_expiration",
    resource="set_key_expiration",
    measured=True,
)
def set_key_expiration(key: str, epoch_seconds: int) -> None:
    """Configure the key to automatically expire at the given time.

    DynamoDB typically deletes expired items within 48 hours of expiration.

    Args:
        key: The name of the counter
        epoch_seconds: When you want the counter to expire (set to 0 to disable)
    """
    if isinstance(epoch_seconds, str):
        epoch_seconds = float(epoch_seconds)
    if isinstance(epoch_seconds, float):
        epoch_seconds = int(epoch_seconds)
    if not isinstance(epoch_seconds, int):
        return
    # if we are given an epoch seconds that is less than
    # 604800 ( aka seven days ), then add the epoch seconds to
    # the timestamp of now
    if epoch_seconds < 604801:
        epoch_seconds = int(datetime.now().timestamp()) + epoch_seconds
    kv_table().update_item(
        Key={"key": key},
        UpdateExpression="SET expiresAt = :time",
        ExpressionAttributeValues={":time": epoch_seconds},
    )


@tracing.wrap(
    name="panther_detection_helpers.caching.put_dictionary",
    resource="put_dictionary",
    measured=True,
)
def put_dictionary(key: str, val: dict, epoch_seconds: Optional[int] = None) -> None:
    """Overwrite a dictionary under the given key.

    The value must be JSON serializable, and therefore cannot contain:
        - Sets
        - Complex numbers or formulas
        - Custom objects
        - Keys that are not strings

    Args:
        key: The name of the dictionary
        val: A Python dictionary
        epoch_seconds: (Optional) Set string expiration time
    """
    if not isinstance(val, (dict, Mapping)):
        raise TypeError("panther_oss_helpers.put_dictionary: value is not a dictionary")

    try:
        # Serialize 'val' to a JSON string
        data = json.dumps(val)
    except TypeError as exc:
        raise ValueError(
            "panther_oss_helpers.put_dictionary: "
            "value is a dictionary, but it is not JSON serializable"
        ) from exc

    # Store the item in DynamoDB
    kv_table().put_item(Item={"key": key, _DICT_COL: data})

    if epoch_seconds:
        set_key_expiration(key, epoch_seconds)


@tracing.wrap(
    name="panther_detection_helpers.caching.get_dictionary",
    resource="get_dictionary",
    measured=True,
)
def get_dictionary(key: str, force_ttl_check: bool = False) -> dict:
    # Retrieve the item from DynamoDB
    response = kv_table().get_item(Key={"key": key})

    item = response.get("Item", {}).get(_DICT_COL, {})

    # Check if the item was not found, if so return empty dictionary
    if not item:
        return {}

    if force_ttl_check and ttl_expired(response):
        return {}

    try:
        # Deserialize from JSON to a Python dictionary
        return json.loads(item)
    except json.decoder.JSONDecodeError as exc:
        raise ValueError(
            "panther_oss_helpers.get_dictionary: "
            "Data found in DynamoDB could not be decoded into JSON"
        ) from exc


@tracing.wrap(
    name="panther_detection_helpers.caching.get_string_set",
    resource="get_string_set",
    measured=True,
)
def get_string_set(key: str, force_ttl_check: bool = False) -> Set[str]:
    """Get a string set's current value (defaulting to empty set if key does not exit)."""
    response = kv_table().get_item(
        Key={"key": key},
        ProjectionExpression=f"{_STRING_SET_COL}, {_TTL_COL}",
    )
    if force_ttl_check and ttl_expired(response):
        return set()
    return response.get("Item", {}).get(_STRING_SET_COL, set())


@tracing.wrap(
    name="panther_detection_helpers.caching.put_string_set",
    resource="put_string_set",
    measured=True,
)
def put_string_set(key: str, val: Sequence[str], epoch_seconds: Optional[int] = None) -> None:
    """Overwrite a string set under the given key.

    This is faster than (reset_string_set + add_string_set) if you know exactly what the contents
    of the set should be.

    Args:
        key: The name of the string set
        val: A list/set/tuple of strings to store
        epoch_seconds: (Optional) Set string expiration time
    """
    if not val:
        # Can't put an empty string set - remove it instead
        reset_string_set(key)
    else:
        kv_table().put_item(Item={"key": key, _STRING_SET_COL: set(val)})
    if epoch_seconds:
        set_key_expiration(key, epoch_seconds)


@tracing.wrap(
    name="panther_detection_helpers.caching.add_to_string_set",
    resource="add_to_string_set",
    measured=True,
)
def add_to_string_set(key: str, val: Union[str, Sequence[str]]) -> Set[str]:
    """Add one or more strings to a set.

    Args:
        key: The name of the string set
        val: Either a single string or a list/tuple/set of strings to add

    Returns:
        The new value of the string set
    """
    if isinstance(val, str):
        item_value = {val}
    else:
        item_value = set(val)
        if not item_value:
            # We can't add empty sets, just return the existing value instead
            return get_string_set(key)

    response = kv_table().update_item(
        Key={"key": key},
        ReturnValues="UPDATED_NEW",
        UpdateExpression="ADD #col :ss",
        ExpressionAttributeNames={"#col": _STRING_SET_COL},
        ExpressionAttributeValues={":ss": item_value},
    )
    return response["Attributes"][_STRING_SET_COL]


@tracing.wrap(
    name="panther_detection_helpers.caching.remove_from_string_set",
    resource="remove_from_string_set",
    measured=True,
)
def remove_from_string_set(key: str, val: Union[str, Sequence[str]]) -> Set[str]:
    """Remove one or more strings from a set.

    Args:
        key: The name of the string set
        val: Either a single string or a list/tuple/set of strings to remove

    Returns:
        The new value of the string set
    """
    if isinstance(val, str):
        item_value = {val}
    else:
        item_value = set(val)
        if not item_value:
            # We can't remove empty sets, just return the existing value instead
            return get_string_set(key)

    response = kv_table().update_item(
        Key={"key": key},
        ReturnValues="UPDATED_NEW",
        UpdateExpression="DELETE #col :ss",
        ExpressionAttributeNames={"#col": _STRING_SET_COL},
        ExpressionAttributeValues={":ss": item_value},
    )
    return response["Attributes"][_STRING_SET_COL]


@tracing.wrap(
    name="panther_detection_helpers.caching.reset_string_set",
    resource="reset_string_set",
    measured=True,
)
def reset_string_set(key: str) -> None:
    """Reset a string set to empty."""
    kv_table().update_item(
        Key={"key": key},
        UpdateExpression="REMOVE #col",
        ExpressionAttributeNames={"#col": _STRING_SET_COL},
    )


@tracing.wrap(
    name="panther_detection_helpers.caching.evaluate_threshold",
    resource="evaluate_threshold",
    measured=True,
)
def evaluate_threshold(key: str, threshold: int = 10, expiry_seconds: int = 3600) -> bool:
    hourly_error_count = increment_counter(key)
    if hourly_error_count == 1:
        set_key_expiration(key, int(time.time()) + expiry_seconds)
    # If it exceeds our threshold, reset and then return an alert
    elif hourly_error_count >= threshold:
        reset_counter(key)
        return True
    return False


@tracing.wrap(
    name="panther_detection_helpers.caching.check_account_age",
    resource="check_account_age",
    measured=True,
)
def check_account_age(key: Any) -> bool:
    """
    Searches DynamoDB for stored user_id or account_id string stored by indicator creation
    rules for new user / account creation
    """
    if isinstance(key, str) and key != "":
        return bool(get_string_set(key))
    return False
