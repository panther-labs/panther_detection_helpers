import datetime
import os
import unittest

import boto3

from panther_detection_helpers import caching
from moto import mock_dynamodb


@mock_dynamodb
class TestCaching(unittest.TestCase):
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
        self.panther_key = caching.reset_counter("panther")
        self.labs_key = caching.reset_counter("labs")
        self.string_set_key = caching.put_string_set("strs", ["a", "b"])

    def test_set_counter_ops(self):
        self.assertEqual(caching.get_counter("panther"), 0)
        self.assertEqual(caching.increment_counter("panther", 1), 1)
        self.assertEqual(caching.increment_counter("panther", -2), -1)
        # something's weird when the val kwarg is zero. not sure it ever worked
        #    global_helpers/panther_oss_helpers.py", line 227, in increment_counter
        #    return response["Attributes"][_COUNT_COL].to_integral_value()
        # self.assertEqual(caching.increment_counter("panther", 0), -1)
        self.assertEqual(caching.increment_counter("panther", 11), 10)
        self.assertEqual(caching.get_counter("panther"), 10)
        caching.reset_counter("panther")
        self.assertEqual(caching.get_counter("panther"), 0)
        self.assertEqual(caching.get_counter("labs"), 0)
        self.assertEqual(caching.get_counter("does-not-exist"), 0)
        # Set TTL
        exp_time = datetime.datetime.strptime("2023-04-01T00:00 +00:00", "%Y-%m-%dT%H:%M %z")
        caching.set_key_expiration("panther", int(exp_time.timestamp()))
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        # Check TTL
        # moto may not be timezone aware when running dynamodb mock.. we ultimately want to confirm
        # that the expiresAt attribute is equal to exp_time.
        self.assertEqual(panther_item["Item"]["expiresAt"], exp_time.timestamp())

        ### TEST TYPE CONVERSIONS ON set_key_expiration
        # Set TTL as a string-with-decimals, expect back an int
        exp_time_2 = "1675238400.0000"
        caching.set_key_expiration("panther", exp_time_2)
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        self.assertEqual(panther_item["Item"]["expiresAt"], 1675238400)

        # Set TTL as a string-without-decimals, expect back an int
        exp_time_2 = "1675238800"
        caching.set_key_expiration("panther", exp_time_2)
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        self.assertEqual(panther_item["Item"]["expiresAt"], 1675238800)

        # Use datetime.timestamp() with millis, which gives back a float
        exp_time_2 = datetime.datetime.strptime(
            "2023-02-01T00:00.123 +00:00", "%Y-%m-%dT%H:%M.%f %z"
        )
        caching.set_key_expiration("panther", int(exp_time_2.timestamp()))
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        self.assertEqual(panther_item["Item"]["expiresAt"], int(exp_time_2.timestamp()))

        # provide a timestamp that's seconds, not an actual epoch timestamp
        now = int(datetime.datetime.now().timestamp())

        # Set expiration time
        caching.set_key_expiration("panther", "86400")
        panther_item = self._temp_table.get_item(
            Key={"key": "panther"}, ProjectionExpression=f"{caching._COUNT_COL}, {caching._TTL_COL}"
        )
        self.assertEqual(panther_item["Item"]["expiresAt"], now + 86400)

    def test_stringset_ops(self):
        self.assertEqual(caching.add_to_string_set("strs2", ["b", "a"]), {"a", "b"})
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
        self.assertEqual(caching.add_to_string_set("strs", ["c"]), {"a", "b", "c"})
        self.assertEqual(caching.add_to_string_set("strs", set()), {"a", "b", "c"})
        self.assertEqual(caching.add_to_string_set("strs", {"b", "c", "d"}), {"a", "b", "c", "d"})
        # tuple is allowed also
        self.assertEqual(caching.add_to_string_set("strs", ("e", "a")), {"a", "b", "c", "d", "e"})
        # empty string is allowed
        self.assertEqual(caching.add_to_string_set("strs", ""), {"a", "b", "c", "d", "e", ""})
        # list is allowed
        self.assertEqual(caching.add_to_string_set("strs", ["g"]), {"a", "b", "c", "d", "e", "", "g"})
        # removal tests
        self.assertEqual(caching.remove_from_string_set("strs", ""), {"a", "b", "c", "d", "e", "g"})
        # empty set test
        self.assertEqual(caching.put_string_set("fake2", []), None)
        # Reset the stringset
        caching.reset_string_set("strs")
        self.assertEqual(caching.get_string_set("strs"), set())

    def test_monitoring_does_not_explode(self) -> None:
        caching.monitoring.USE_MONITORING = True
        self.assertEqual(caching.get_string_set("strs"), {"a", "b"})
