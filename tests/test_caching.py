import datetime
import json
import os
import unittest
from abc import ABC

import boto3
from moto import mock_dynamodb

from panther_detection_helpers import caching


@mock_dynamodb
class DynamoBaseTest(ABC):
    # pylint: disable=protected-access,assignment-from-no-return
    def setUp(self):
        os.environ["AWS_DEFAULT_REGION"] = "us-west-2"
        self._temp_dynamo = boto3.resource("dynamodb")
        self._temp_table = self._temp_dynamo.create_table(
            TableName="panther-kv-store",
            KeySchema=[
                {
                    "AttributeName": "key",
                    "KeyType": "HASH",
                }
            ],
            AttributeDefinitions=[
                {
                    "AttributeName": "key",
                    "AttributeType": "S",
                }
            ],
            ProvisionedThroughput={
                "ReadCapacityUnits": 5,
                "WriteCapacityUnits": 5,
            },
        )
        caching._KV_TABLE = self._temp_table
        caching.reset_counter("panther")
        caching.reset_counter("labs")
        caching.put_string_set("strs", ["a", "b"])
        caching.put_dictionary("d", {"z": "y"})


@mock_dynamodb
class TestCachingCounter(DynamoBaseTest, unittest.TestCase):
    def test_unset_counter(self):
        self.assertEqual(caching.get_counter("panther"), 0)

    def test_simple_increment(self):
        self.assertEqual(caching.increment_counter("panther"), 1)
        self.assertEqual(caching.get_counter("panther"), 1)

    def test_decrement(self):
        self.assertEqual(caching.increment_counter("panther", -2), -2)
        self.assertEqual(caching.get_counter("panther"), -2)

    def test_large_increment(self):
        self.assertEqual(caching.increment_counter("panther", 11), 11)
        self.assertEqual(caching.get_counter("panther"), 11)

    def test_sequence_with_reset(self):
        self.assertEqual(caching.increment_counter("panther"), 1)
        self.assertEqual(caching.get_counter("panther"), 1)

        self.assertEqual(caching.increment_counter("panther", -2), -1)
        self.assertEqual(caching.get_counter("panther"), -1)

        self.assertEqual(caching.increment_counter("panther", 11), 10)
        self.assertEqual(caching.get_counter("panther"), 10)

        caching.reset_counter("panther")
        self.assertEqual(caching.get_counter("panther"), 0)

    def test_still_unset(self):
        self.assertEqual(caching.get_counter("labs"), 0)

    def test_nonexistent(self):
        self.assertEqual(caching.get_counter("does-not-exist"), 0)

    def test_simple_ttl(self):
        # Set TTL
        exp_time = datetime.datetime.strptime("2023-04-01T00:00 +00:00", "%Y-%m-%dT%H:%M %z")
        caching.increment_counter("panther", epoch_seconds=int(exp_time.timestamp()))
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        # moto may not be timezone aware when running dynamodb mock.. we ultimately want to confirm
        # that the expiresAt attribute is equal to exp_time.
        self.assertEqual(panther_item["Item"][caching._TTL_COL], exp_time.timestamp())
        self.assertEqual(panther_item["Item"][caching._COUNT_COL], 1)

    def test_ttl_and_set_increment(self):
        # Set TTL
        exp_time = datetime.datetime.strptime("2023-04-01T00:00 +00:00", "%Y-%m-%dT%H:%M %z")
        caching.increment_counter("panther", 20, int(exp_time.timestamp()))
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        # moto may not be timezone aware when running dynamodb mock.. we ultimately want to confirm
        # that the expiresAt attribute is equal to exp_time.
        self.assertEqual(panther_item["Item"][caching._TTL_COL], exp_time.timestamp())
        self.assertEqual(panther_item["Item"][caching._COUNT_COL], 20)

    def test_ttl_decimal_string_conversion(self):
        # Set TTL as a string-with-decimals, expect back an int
        exp_time = "1675238400.0000"
        caching.increment_counter("panther", epoch_seconds=exp_time)
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        self.assertEqual(panther_item["Item"][caching._TTL_COL], 1675238400)
        self.assertEqual(panther_item["Item"][caching._COUNT_COL], 1)

    def test_ttl_int_string_conversion(self):
        # Set TTL as a string-without-decimals, expect back an int
        exp_time = "1675238800"
        caching.increment_counter("panther", epoch_seconds=exp_time)
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        self.assertEqual(panther_item["Item"][caching._TTL_COL], 1675238800)
        self.assertEqual(panther_item["Item"][caching._COUNT_COL], 1)

    def test_ttl_float_conversion(self):
        # Use datetime.timestamp() with millis, which gives back a float
        exp_time = datetime.datetime.strptime(
            "2023-02-01T00:00.123 +00:00", "%Y-%m-%dT%H:%M.%f %z"
        )
        caching.increment_counter("panther", epoch_seconds=(exp_time.timestamp()))
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        self.assertEqual(panther_item["Item"][caching._TTL_COL], int(exp_time.timestamp()))
        self.assertEqual(panther_item["Item"][caching._COUNT_COL], 1)

    def test_ttl_small_delta(self):
        # provide a timestamp that's seconds, not an actual epoch timestamp
        now = int(datetime.datetime.now().timestamp())

        # Set expiration time
        caching.increment_counter("panther", epoch_seconds="86400")
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        self.assertEqual(panther_item["Item"][caching._TTL_COL], now + 86400)
        self.assertEqual(panther_item["Item"][caching._COUNT_COL], 1)

    def test_default_ttl(self):
        caching.increment_counter("panther")
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        self.assertGreater(panther_item["Item"][caching._TTL_COL], int(datetime.datetime.now().timestamp()) + caching._EPOCH_SECONDS_DELTA_DEFAULT - 10)
        self.assertLess(panther_item["Item"][caching._TTL_COL], int(datetime.datetime.now().timestamp()) + caching._EPOCH_SECONDS_DELTA_DEFAULT + 10)
        self.assertEqual(panther_item["Item"][caching._COUNT_COL], 1)


@mock_dynamodb
class TestCachingStringSet(DynamoBaseTest, unittest.TestCase):
    def test_new_add(self):
        self.assertEqual(caching.get_string_set("strs2"), set())
        self.assertEqual(caching.add_to_string_set("strs2", ["b", "a"]), {"a", "b"})
        self.assertEqual(caching.get_string_set("strs2"), {"a", "b"})

    def test_existing_add(self):
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
        self.assertEqual(caching.add_to_string_set("strs", ["c"]), {"a", "b", "c"})
        self.assertEqual(caching.get_string_set("strs"), {"a", "b", "c"})

    def test_add_empty(self):
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
        self.assertEqual(caching.add_to_string_set("strs", set()), {"a", "b"})
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})

    def test_add_duplicate(self):
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
        self.assertEqual(caching.add_to_string_set("strs", ["a"]), {"a", "b"})
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})

    def test_add_tuple(self):
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
        self.assertEqual(caching.add_to_string_set("strs", ("e", "d")), {"a", "b", "d", "e"})
        self.assertEqual(caching.get_string_set("strs"), {"a", "b", "d", "e"})

    def test_add_empty_string(self):
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
        self.assertEqual(caching.add_to_string_set("strs", ""), {"a", "b", ""})
        self.assertEqual(caching.get_string_set("strs"), {"a", "b", ""})

    def test_add_set(self):
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
        self.assertEqual(caching.add_to_string_set("strs", {"g"}), {"a", "b", "g"})
        self.assertEqual(caching.get_string_set("strs"), {"a", "b", "g"})

    def test_remove(self):
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
        self.assertEqual(caching.remove_from_string_set("strs", "a"), {"b"})
        self.assertEqual(caching.get_string_set("strs"), {"b"})

    def test_put_empty(self):
        self.assertEqual(caching.get_string_set("fake2"), set())
        caching.put_string_set("fake2", [])
        self.assertEqual(caching.get_string_set("fake2"), set())

    def test_put_replace(self):
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
        caching.put_string_set("strs", ["c", "d"])
        self.assertEqual(caching.get_string_set("strs"), {"c", "d"})

    def test_reset(self):
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
        caching.reset_string_set("strs")
        self.assertEqual(caching.get_string_set("strs"), set())

    def test_add_with_ttl(self):
        # Set TTL
        exp_time = datetime.datetime.strptime("2023-04-01T00:00 +00:00", "%Y-%m-%dT%H:%M %z")
        caching.add_to_string_set("strs", ["v"], epoch_seconds=int(exp_time.timestamp()))
        strs_item = self._temp_table.get_item(
            Key={"key": "strs"}, ProjectionExpression=f"{caching._STRING_SET_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        self.assertEqual(strs_item["Item"][caching._TTL_COL], exp_time.timestamp())
        self.assertEqual(strs_item["Item"][caching._STRING_SET_COL], {"a", "b", "v"})

    def test_add_with_default_ttl(self):
        # Set TTL
        caching.add_to_string_set("strs", ["w"])
        strs_item = self._temp_table.get_item(
            Key={"key": "strs"}, ProjectionExpression=f"{caching._STRING_SET_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        self.assertGreater(strs_item["Item"][caching._TTL_COL], int(datetime.datetime.now().timestamp()) + caching._EPOCH_SECONDS_DELTA_DEFAULT - 10)
        self.assertLess(strs_item["Item"][caching._TTL_COL], int(datetime.datetime.now().timestamp()) + caching._EPOCH_SECONDS_DELTA_DEFAULT + 10)
        self.assertEqual(strs_item["Item"][caching._STRING_SET_COL], {"a", "b", "w"})

    def test_remove_with_ttl(self):
        # Set TTL
        exp_time = datetime.datetime.strptime("2023-04-01T00:00 +00:00", "%Y-%m-%dT%H:%M %z")
        caching.remove_from_string_set("strs", ["a"], epoch_seconds=int(exp_time.timestamp()))
        strs_item = self._temp_table.get_item(
            Key={"key": "strs"}, ProjectionExpression=f"{caching._STRING_SET_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        self.assertEqual(strs_item["Item"][caching._TTL_COL], exp_time.timestamp())
        self.assertEqual(strs_item["Item"][caching._STRING_SET_COL], {"b"})

    def test_remove_with_default_ttl(self):
        # Set TTL
        caching.remove_from_string_set("strs", ["b"])
        strs_item = self._temp_table.get_item(
            Key={"key": "strs"}, ProjectionExpression=f"{caching._STRING_SET_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        self.assertGreater(strs_item["Item"][caching._TTL_COL], int(datetime.datetime.now().timestamp()) + caching._EPOCH_SECONDS_DELTA_DEFAULT - 10)
        self.assertLess(strs_item["Item"][caching._TTL_COL], int(datetime.datetime.now().timestamp()) + caching._EPOCH_SECONDS_DELTA_DEFAULT + 10)
        self.assertEqual(strs_item["Item"][caching._STRING_SET_COL], {"a"})

    def test_put_with_ttl(self):
        # Set TTL
        exp_time = datetime.datetime.strptime("2023-04-01T00:00 +00:00", "%Y-%m-%dT%H:%M %z")
        caching.put_string_set("strs", ["v", "x"], epoch_seconds=int(exp_time.timestamp()))
        strs_item = self._temp_table.get_item(
            Key={"key": "strs"}, ProjectionExpression=f"{caching._STRING_SET_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        self.assertEqual(strs_item["Item"][caching._TTL_COL], exp_time.timestamp())
        self.assertEqual(strs_item["Item"][caching._STRING_SET_COL], {"v", "x"})

    def test_put_with_default_ttl(self):
        # Set TTL
        caching.put_string_set("strs", ["w", "y"])
        strs_item = self._temp_table.get_item(
            Key={"key": "strs"}, ProjectionExpression=f"{caching._STRING_SET_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        self.assertGreater(strs_item["Item"][caching._TTL_COL], int(datetime.datetime.now().timestamp()) + caching._EPOCH_SECONDS_DELTA_DEFAULT - 10)
        self.assertLess(strs_item["Item"][caching._TTL_COL], int(datetime.datetime.now().timestamp()) + caching._EPOCH_SECONDS_DELTA_DEFAULT + 10)
        self.assertEqual(strs_item["Item"][caching._STRING_SET_COL], {"w", "y"})


@mock_dynamodb
class TestCachingDictionary(DynamoBaseTest, unittest.TestCase):
    def test_new(self):
        self.assertEqual(caching.get_dictionary("dict"), dict())
        caching.put_dictionary("dict", {"b": "a"})
        self.assertEqual(caching.get_dictionary("dict"), {"b": "a"})

    def test_overwrite(self):
        self.assertEqual(caching.get_dictionary("d"), {"z": "y"})
        caching.put_dictionary("d", {"c": "e"})
        self.assertEqual(caching.get_dictionary("d"), {"c": "e"})

    def test_empty(self):
        self.assertEqual(caching.get_dictionary("d"), {"z": "y"})
        caching.put_dictionary("d", dict())
        self.assertEqual(caching.get_dictionary("d"), dict())

    def test_put_with_ttl(self):
        # Set TTL
        exp_time = datetime.datetime.strptime("2023-04-01T00:00 +00:00", "%Y-%m-%dT%H:%M %z")
        caching.put_dictionary("d", {"v": "x"}, epoch_seconds=int(exp_time.timestamp()))
        dict_item = self._temp_table.get_item(
            Key={"key": "d"}, ProjectionExpression=f"{caching._DICT_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        self.assertEqual(dict_item["Item"][caching._TTL_COL], exp_time.timestamp())
        self.assertEqual(json.loads(dict_item["Item"][caching._DICT_COL]), {"v": "x"})

    def test_put_with_default_ttl(self):
        # Set TTL
        caching.put_dictionary("d", {"w": "y"})
        dict_item = self._temp_table.get_item(
            Key={"key": "d"}, ProjectionExpression=f"{caching._DICT_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        self.assertGreater(dict_item["Item"][caching._TTL_COL], int(datetime.datetime.now().timestamp()) + caching._EPOCH_SECONDS_DELTA_DEFAULT - 10)
        self.assertLess(dict_item["Item"][caching._TTL_COL], int(datetime.datetime.now().timestamp()) + caching._EPOCH_SECONDS_DELTA_DEFAULT + 10)
        self.assertEqual(json.loads(dict_item["Item"][caching._DICT_COL]), {"w": "y"})


@mock_dynamodb
class TestUsingMonitoring(DynamoBaseTest, unittest.TestCase):
    def test_monitoring_does_not_explode(self) -> None:
        caching.monitoring.USE_MONITORING = True
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})