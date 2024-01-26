import json
import os
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence, Set, Union

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from . import monitoring

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
_EPOCH_SECONDS_DELTA_DEFAULT = 90 * (60 * 60 * 24)  # 90 days

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


@monitoring.wrap(name="panther_detection_helpers.caching.ttl_expired")
def ttl_expired(response: dict) -> bool:
    """Checks whether a response from the panther-kv table has passed it's TTL date

    Args:
        response: The value returned from the KV Store for which to check the TTL

    Returns:
        Whether the response is expired according to its TTL
    """
    # This can be used when the TTL timing is very exacting and DDB's cleanup is too slow
    expiration = response.get("Item", {}).get(_TTL_COL, 0)
    return expiration and float(expiration) <= (datetime.now()).timestamp()


@monitoring.wrap(name="panther_detection_helpers.caching.get_counter")
def get_counter(key: str, force_ttl_check: bool = False) -> int:
    """Get a counter's current value (defaulting to 0 if key does not exist).

    Args:
        key: The name of the counter
        force_ttl_check: Whether to force a TTL check (rather than relying on underlying eventually-consistent mechanisms)

    Returns:
        The counter's current count
    """
    response = kv_table().get_item(
        Key={"key": key},
        ProjectionExpression=f"{_COUNT_COL}, {_TTL_COL}",
    )
    if force_ttl_check and ttl_expired(response):
        return 0
    return response.get("Item", {}).get(_COUNT_COL, 0)


@monitoring.wrap(name="panther_detection_helpers.caching.increment_counter")
def increment_counter(key: str, val: int = 1, epoch_seconds: Optional[int] = None) -> int:
    """Increment a counter in the table.

    Args:
        key: The name of the counter (need not exist yet)
        val (Optional): How much to add to the counter. Default: 1
        epoch_seconds: (Optional) How long until the counter expires in seconds. Default: 90 days from now

    Returns:
        The new value of the count
    """

    try:
        response = kv_table().update_item(
            Key={"key": key},
            ReturnValues="UPDATED_NEW",
            UpdateExpression="ADD #col :incr SET #ttlcol = :time",
            ExpressionAttributeNames={"#col": _COUNT_COL, "#ttlcol": _TTL_COL},
            ExpressionAttributeValues={
                ":incr": val,
                ":time": _finalize_epoch_seconds(epoch_seconds),
            },
            ConditionExpression=Attr(_TTL_COL).gte(
                _finalize_epoch_seconds(int(datetime.now().timestamp()))
            ),
        )
        return response["Attributes"][_COUNT_COL].to_integral_value()
    except ClientError as exception:
        # Ignore the ConditionalCheckFailedException, bubble up
        # other exceptions.
        if exception.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    # If we got here, the conditional update failed and we know the key is expired
    response = kv_table().update_item(
        Key={"key": key},
        ReturnValues="UPDATED_NEW",
        UpdateExpression="SET #col = :incr, #ttlcol = :time",
        ExpressionAttributeNames={"#col": _COUNT_COL, "#ttlcol": _TTL_COL},
        ExpressionAttributeValues={
            ":incr": val,
            ":time": _finalize_epoch_seconds(epoch_seconds),
        },
    )
    return val


@monitoring.wrap(name="panther_detection_helpers.caching.reset_counter")
def reset_counter(key: str) -> None:
    """Reset a counter to 0.

    Args:
        key: The name of the counter to reset
    """
    kv_table().update_item(
        Key={"key": key},
        UpdateExpression="REMOVE #col",
        ExpressionAttributeNames={"#col": _COUNT_COL},
    )


@monitoring.wrap(name="panther_detection_helpers.caching.set_key_expiration")
def set_key_expiration(key: str, epoch_seconds: Optional[int]) -> None:
    """Configure the key to automatically expire at the given time.

    DynamoDB typically deletes expired items within 48 hours of expiration.

    Args:
        key: The name of the counter
        epoch_seconds: (Optional) How long until the counter expires in seconds.
                       Default: 90 days from now (set to 0 to disable)
    """
    kv_table().update_item(
        Key={"key": key},
        UpdateExpression="SET #ttlcol = :time",
        ExpressionAttributeNames={"#ttlcol": _TTL_COL},
        ExpressionAttributeValues={":time": _finalize_epoch_seconds(epoch_seconds)},
    )


def _finalize_epoch_seconds(epoch_seconds: Optional[int]) -> int:
    if isinstance(epoch_seconds, str):
        epoch_seconds = float(epoch_seconds)
    if isinstance(epoch_seconds, float):
        epoch_seconds = int(epoch_seconds)
    if not isinstance(epoch_seconds, int):
        epoch_seconds = int(datetime.now().timestamp()) + _EPOCH_SECONDS_DELTA_DEFAULT
    # if we are given an epoch seconds that is less than
    # 604800 ( aka seven days ), then add the epoch seconds to
    # the timestamp of now
    if epoch_seconds < 604801:
        epoch_seconds = int(datetime.now().timestamp()) + epoch_seconds
    return epoch_seconds


@monitoring.wrap(name="panther_detection_helpers.caching.put_dictionary")
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
        epoch_seconds: (Optional) How long until the counter expires in seconds. Default: 90 days from now
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
    kv_table().put_item(
        Item={"key": key, _DICT_COL: data, _TTL_COL: _finalize_epoch_seconds(epoch_seconds)}
    )


@monitoring.wrap(name="panther_detection_helpers.caching.get_dictionary")
def get_dictionary(key: str, force_ttl_check: bool = False) -> dict:
    """Retrieve a dictionary under the given key

    Args:
        key: The name of the dictionary
        force_ttl_check: Whether to force a TTL check (rather than relying on underlying eventually-consistent mechanisms)

    Returns:
        The retrieved dictionary
    """
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


@monitoring.wrap(name="panther_detection_helpers.caching.get_string_set")
def get_string_set(key: str, force_ttl_check: bool = False) -> Set[str]:
    """Get a string set's current value (defaulting to empty set if key does not exit).

    Args:
        key: The name of the string set
        force_ttl_check: Whether to force a TTL check (rather than relying on underlying eventually-consistent mechanisms)

    Returns:
        The retrieved string set
    """
    response = kv_table().get_item(
        Key={"key": key},
        ProjectionExpression=f"{_STRING_SET_COL}, {_TTL_COL}",
    )
    if force_ttl_check and ttl_expired(response):
        return set()
    return response.get("Item", {}).get(_STRING_SET_COL, set())


@monitoring.wrap(name="panther_detection_helpers.caching.put_string_set")
def put_string_set(key: str, val: Set[str], epoch_seconds: Optional[int] = None) -> None:
    """Overwrite a string set under the given key.

    This is faster than (reset_string_set + add_string_set) if you know exactly what the contents
    of the set should be.

    Args:
        key: The name of the string set
        val: A list/set/tuple of strings to store
        epoch_seconds: (Optional) How long until the counter expires in seconds. Default: 90 days from now
    """
    if not val:
        # Can't put an empty string set - remove it instead
        reset_string_set(key)
    else:
        kv_table().put_item(
            Item={
                "key": key,
                _STRING_SET_COL: set(val),
                _TTL_COL: _finalize_epoch_seconds(epoch_seconds),
            }
        )


@monitoring.wrap(name="panther_detection_helpers.caching.add_to_string_set")
def add_to_string_set(
    key: str,
    val: Union[str, Sequence[str]],
    epoch_seconds: Optional[int] = None,
) -> Set[str]:
    """Add one or more strings to a set.

    Args:
        key: The name of the string set
        val: Either a single string or a list/tuple/set of strings to add
        epoch_seconds: (Optional) How long until the counter expires in seconds. Default: 90 days from now

    Returns:
        The new value of the string set
    """
    if isinstance(val, str):
        item_value = {val}
    else:
        item_value = set(val)
        if not item_value:
            # We can't add empty sets, just return the existing value instead
            return get_string_set(key, force_ttl_check=True)

    try:
        response = kv_table().update_item(
            Key={"key": key},
            ReturnValues="UPDATED_NEW",
            UpdateExpression="ADD #col :ss SET #ttlcol = :time",
            ExpressionAttributeNames={"#col": _STRING_SET_COL, "#ttlcol": _TTL_COL},
            ExpressionAttributeValues={
                ":ss": item_value,
                ":time": _finalize_epoch_seconds(epoch_seconds),
            },
            ConditionExpression=Attr(_TTL_COL).gte(
                _finalize_epoch_seconds(int(datetime.now().timestamp()))
            ),
        )
        current_string_set = response["Attributes"].get(_STRING_SET_COL, None)
        if current_string_set is None:
            current_string_set = get_string_set(key, force_ttl_check=True)
        return current_string_set
    except ClientError as exception:
        # Ignore the ConditionalCheckFailedException, bubble up
        # other exceptions.
        if exception.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    # If we got here, the conditional update failed and we know the key is expired
    response = kv_table().update_item(
        Key={"key": key},
        ReturnValues="UPDATED_NEW",
        UpdateExpression="SET #col = :ss, #ttlcol = :time",
        ExpressionAttributeNames={"#col": _STRING_SET_COL, "#ttlcol": _TTL_COL},
        ExpressionAttributeValues={
            ":ss": item_value,
            ":time": _finalize_epoch_seconds(epoch_seconds),
        },
    )

    current_string_set = response["Attributes"].get(_STRING_SET_COL, None)
    if current_string_set is None:
        current_string_set = get_string_set(key, force_ttl_check=True)
    return current_string_set


@monitoring.wrap(name="panther_detection_helpers.caching.remove_from_string_set")
def remove_from_string_set(
    key: str,
    val: Union[str, Sequence[str]],
    epoch_seconds: Optional[int] = None,
) -> Set[str]:
    """Remove one or more strings from a set.

    Args:
        key: The name of the string set
        val: Either a single string or a list/tuple/set of strings to remove
        epoch_seconds: (Optional) How long until the counter expires in seconds. Default: 90 days from now

    Returns:
        The new value of the string set
    """
    if isinstance(val, str):
        item_value = {val}
    else:
        item_value = set(val)
        if not item_value:
            # We can't remove empty sets, just return the existing value instead
            return get_string_set(key, force_ttl_check=True)

    try:
        response = kv_table().update_item(
            Key={"key": key},
            ReturnValues="UPDATED_NEW",
            UpdateExpression="DELETE #col :ss SET #ttlcol = :time",
            ExpressionAttributeNames={"#col": _STRING_SET_COL, "#ttlcol": _TTL_COL},
            ExpressionAttributeValues={
                ":ss": item_value,
                ":time": _finalize_epoch_seconds(epoch_seconds),
            },
            ConditionExpression=Attr(_TTL_COL).gte(
                _finalize_epoch_seconds(int(datetime.now().timestamp()))
            ),
        )
        return response["Attributes"].get(_STRING_SET_COL, set())
    except ClientError as exception:
        # Ignore the ConditionalCheckFailedException, bubble up
        # other exceptions.
        if exception.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    # If we got here, the conditional update failed and we know the key is expired
    reset_string_set(key)
    return set()


@monitoring.wrap(name="panther_detection_helpers.caching.reset_string_set")
def reset_string_set(key: str) -> None:
    """Reset a string set to empty.

    Args:
        key: The name to reset
    """
    kv_table().update_item(
        Key={"key": key},
        UpdateExpression="REMOVE #col",
        ExpressionAttributeNames={"#col": _STRING_SET_COL},
    )


@monitoring.wrap(name="panther_detection_helpers.caching.evaluate_threshold")
def evaluate_threshold(key: str, threshold: int = 10, expiry_seconds: int = 3600) -> bool:
    """
    Increment counter and check whether the count meets the threshold. If so, reset and alert.
    Args:
        key: The name to evaluate
        threshold: (Optional) The threshold to meet or exceed. Default: 10
        expiry_seconds: (Optional) How many seconds from now to expire

    Returns: Whether we met the threshold
    """
    hourly_error_count = increment_counter(key, epoch_seconds=expiry_seconds)
    # If it exceeds our threshold, reset and then return an alert
    if hourly_error_count >= threshold:
        reset_counter(key)
        return True
    return False


@monitoring.wrap(name="panther_detection_helpers.caching.check_account_age")
def check_account_age(key: Any) -> bool:
    """
    Searches DynamoDB for stored user_id or account_id string stored by indicator creation
    rules for new user / account creation

    Args:
        key: The name to check
    """
    if isinstance(key, str) and key != "":
        return bool(get_string_set(key, force_ttl_check=True))
    return False
