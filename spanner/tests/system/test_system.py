# Copyright 2016 Google LLC All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import math
import operator
import os
import struct
import threading
import time
import unittest

from google.cloud.spanner_v1.proto.type_pb2 import ARRAY
from google.cloud.spanner_v1.proto.type_pb2 import BOOL
from google.cloud.spanner_v1.proto.type_pb2 import BYTES
from google.cloud.spanner_v1.proto.type_pb2 import DATE
from google.cloud.spanner_v1.proto.type_pb2 import FLOAT64
from google.cloud.spanner_v1.proto.type_pb2 import INT64
from google.cloud.spanner_v1.proto.type_pb2 import STRING
from google.cloud.spanner_v1.proto.type_pb2 import TIMESTAMP
from google.cloud.spanner_v1.proto.type_pb2 import Type
from google.gax.grpc import exc_to_code
from google.gax import errors
from grpc import StatusCode

from google.cloud._helpers import UTC
from google.cloud.exceptions import GrpcRendezvous
from google.cloud.spanner_v1._helpers import TimestampWithNanoseconds
from google.cloud.spanner import Client
from google.cloud.spanner import KeyRange
from google.cloud.spanner import KeySet
from google.cloud.spanner import BurstyPool

from test_utils.retry import RetryErrors
from test_utils.retry import RetryInstanceState
from test_utils.retry import RetryResult
from test_utils.system import unique_resource_id
from tests._fixtures import DDL_STATEMENTS


CREATE_INSTANCE = os.getenv(
    'GOOGLE_CLOUD_TESTS_CREATE_SPANNER_INSTANCE') is not None

if CREATE_INSTANCE:
    INSTANCE_ID = 'google-cloud' + unique_resource_id('-')
else:
    INSTANCE_ID = os.environ.get('GOOGLE_CLOUD_TESTS_SPANNER_INSTANCE',
                                 'google-cloud-python-systest')
EXISTING_INSTANCES = []
COUNTERS_TABLE = 'counters'
COUNTERS_COLUMNS = ('name', 'value')


class Config(object):
    """Run-time configuration to be modified at set-up.

    This is a mutable stand-in to allow test set-up to modify
    global state.
    """
    CLIENT = None
    INSTANCE_CONFIG = None
    INSTANCE = None


def _retry_on_unavailable(exc):
    """Retry only errors whose status code is 'UNAVAILABLE'."""
    return exc.code() == StatusCode.UNAVAILABLE


def _has_all_ddl(database):
    return len(database.ddl_statements) == len(DDL_STATEMENTS)


def _list_instances():
    return list(Config.CLIENT.list_instances())


def setUpModule():
    Config.CLIENT = Client()
    retry = RetryErrors(GrpcRendezvous, error_predicate=_retry_on_unavailable)

    configs = list(retry(Config.CLIENT.list_instance_configs)())

    # Defend against back-end returning configs for regions we aren't
    # actually allowed to use.
    configs = [config for config in configs if '-us-' in config.name]

    if len(configs) < 1:
        raise ValueError('List instance configs failed in module set up.')

    Config.INSTANCE_CONFIG = configs[0]
    config_name = configs[0].name

    instances = retry(_list_instances)()
    EXISTING_INSTANCES[:] = instances

    if CREATE_INSTANCE:
        Config.INSTANCE = Config.CLIENT.instance(INSTANCE_ID, config_name)
        created_op = Config.INSTANCE.create()
        created_op.result(30)  # block until completion

    else:
        Config.INSTANCE = Config.CLIENT.instance(INSTANCE_ID)
        Config.INSTANCE.reload()


def tearDownModule():
    if CREATE_INSTANCE:
        Config.INSTANCE.delete()


class TestInstanceAdminAPI(unittest.TestCase):

    def setUp(self):
        self.instances_to_delete = []

    def tearDown(self):
        for instance in self.instances_to_delete:
            instance.delete()

    def test_list_instances(self):
        instances = list(Config.CLIENT.list_instances())
        # We have added one new instance in `setUpModule`.
        if CREATE_INSTANCE:
            self.assertEqual(len(instances), len(EXISTING_INSTANCES) + 1)
        for instance in instances:
            instance_existence = (instance in EXISTING_INSTANCES or
                                  instance == Config.INSTANCE)
            self.assertTrue(instance_existence)

    def test_reload_instance(self):
        # Use same arguments as Config.INSTANCE (created in `setUpModule`)
        # so we can use reload() on a fresh instance.
        instance = Config.CLIENT.instance(
            INSTANCE_ID, Config.INSTANCE_CONFIG.name)
        # Make sure metadata unset before reloading.
        instance.display_name = None

        instance.reload()
        self.assertEqual(instance.display_name, Config.INSTANCE.display_name)

    @unittest.skipUnless(CREATE_INSTANCE, 'Skipping instance creation')
    def test_create_instance(self):
        ALT_INSTANCE_ID = 'new' + unique_resource_id('-')
        instance = Config.CLIENT.instance(
            ALT_INSTANCE_ID, Config.INSTANCE_CONFIG.name)
        operation = instance.create()
        # Make sure this instance gets deleted after the test case.
        self.instances_to_delete.append(instance)

        # We want to make sure the operation completes.
        operation.result(30)  # raises on failure / timeout.

        # Create a new instance instance and make sure it is the same.
        instance_alt = Config.CLIENT.instance(
            ALT_INSTANCE_ID, Config.INSTANCE_CONFIG.name)
        instance_alt.reload()

        self.assertEqual(instance, instance_alt)
        self.assertEqual(instance.display_name, instance_alt.display_name)

    def test_update_instance(self):
        OLD_DISPLAY_NAME = Config.INSTANCE.display_name
        NEW_DISPLAY_NAME = 'Foo Bar Baz'
        Config.INSTANCE.display_name = NEW_DISPLAY_NAME
        operation = Config.INSTANCE.update()

        # We want to make sure the operation completes.
        operation.result(30)  # raises on failure / timeout.

        # Create a new instance instance and reload it.
        instance_alt = Config.CLIENT.instance(INSTANCE_ID, None)
        self.assertNotEqual(instance_alt.display_name, NEW_DISPLAY_NAME)
        instance_alt.reload()
        self.assertEqual(instance_alt.display_name, NEW_DISPLAY_NAME)

        # Make sure to put the instance back the way it was for the
        # other test cases.
        Config.INSTANCE.display_name = OLD_DISPLAY_NAME
        Config.INSTANCE.update()


class _TestData(object):
    TABLE = 'contacts'
    COLUMNS = ('contact_id', 'first_name', 'last_name', 'email')
    ROW_DATA = (
        (1, u'Phred', u'Phlyntstone', u'phred@example.com'),
        (2, u'Bharney', u'Rhubble', u'bharney@example.com'),
        (3, u'Wylma', u'Phlyntstone', u'wylma@example.com'),
    )
    ALL = KeySet(all_=True)
    SQL = 'SELECT * FROM contacts ORDER BY contact_id'

    def _assert_timestamp(self, value, nano_value):
        self.assertIsInstance(value, datetime.datetime)
        self.assertIsNone(value.tzinfo)
        self.assertIs(nano_value.tzinfo, UTC)

        self.assertEqual(value.year, nano_value.year)
        self.assertEqual(value.month, nano_value.month)
        self.assertEqual(value.day, nano_value.day)
        self.assertEqual(value.hour, nano_value.hour)
        self.assertEqual(value.minute, nano_value.minute)
        self.assertEqual(value.second, nano_value.second)
        self.assertEqual(value.microsecond, nano_value.microsecond)
        if isinstance(value, TimestampWithNanoseconds):
            self.assertEqual(value.nanosecond, nano_value.nanosecond)
        else:
            self.assertEqual(value.microsecond * 1000, nano_value.nanosecond)

    def _check_rows_data(self, rows_data, expected=None):
        if expected is None:
            expected = self.ROW_DATA

        self.assertEqual(len(rows_data), len(expected))
        for row, expected in zip(rows_data, expected):
            self._check_row_data(row, expected)

    def _check_row_data(self, row_data, expected):
        self.assertEqual(len(row_data), len(expected))
        for found_cell, expected_cell in zip(row_data, expected):
            if isinstance(found_cell, TimestampWithNanoseconds):
                self._assert_timestamp(expected_cell, found_cell)
            elif isinstance(found_cell, float) and math.isnan(found_cell):
                self.assertTrue(math.isnan(expected_cell))
            else:
                self.assertEqual(found_cell, expected_cell)


class TestDatabaseAPI(unittest.TestCase, _TestData):
    DATABASE_NAME = 'test_database' + unique_resource_id('_')

    @classmethod
    def setUpClass(cls):
        pool = BurstyPool()
        cls._db = Config.INSTANCE.database(
            cls.DATABASE_NAME, ddl_statements=DDL_STATEMENTS, pool=pool)
        operation = cls._db.create()
        operation.result(30)  # raises on failure / timeout.

    @classmethod
    def tearDownClass(cls):
        cls._db.drop()

    def setUp(self):
        self.to_delete = []

    def tearDown(self):
        for doomed in self.to_delete:
            doomed.drop()

    def test_list_databases(self):
        # Since `Config.INSTANCE` is newly created in `setUpModule`, the
        # database created in `setUpClass` here will be the only one.
        database_names = [
            database.name for database in Config.INSTANCE.list_databases()]
        self.assertTrue(self._db.name in database_names)

    def test_create_database(self):
        pool = BurstyPool()
        temp_db_id = 'temp_db' + unique_resource_id('_')
        temp_db = Config.INSTANCE.database(temp_db_id, pool=pool)
        operation = temp_db.create()
        self.to_delete.append(temp_db)

        # We want to make sure the operation completes.
        operation.result(30)  # raises on failure / timeout.

        database_ids = [
            database.database_id
            for database in Config.INSTANCE.list_databases()]
        self.assertIn(temp_db_id, database_ids)

    def test_update_database_ddl(self):
        pool = BurstyPool()
        temp_db_id = 'temp_db' + unique_resource_id('_')
        temp_db = Config.INSTANCE.database(temp_db_id, pool=pool)
        create_op = temp_db.create()
        self.to_delete.append(temp_db)

        # We want to make sure the operation completes.
        create_op.result(120)  # raises on failure / timeout.

        operation = temp_db.update_ddl(DDL_STATEMENTS)

        # We want to make sure the operation completes.
        operation.result(120)  # raises on failure / timeout.

        temp_db.reload()

        self.assertEqual(len(temp_db.ddl_statements), len(DDL_STATEMENTS))

    def test_db_batch_insert_then_db_snapshot_read(self):
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        with self._db.batch() as batch:
            batch.delete(self.TABLE, self.ALL)
            batch.insert(self.TABLE, self.COLUMNS, self.ROW_DATA)

        with self._db.snapshot(read_timestamp=batch.committed) as snapshot:
            from_snap = list(snapshot.read(self.TABLE, self.COLUMNS, self.ALL))

        self._check_rows_data(from_snap)

    def test_db_run_in_transaction_then_snapshot_execute_sql(self):
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        with self._db.batch() as batch:
            batch.delete(self.TABLE, self.ALL)

        def _unit_of_work(transaction, test):
            rows = list(transaction.read(test.TABLE, test.COLUMNS, self.ALL))
            test.assertEqual(rows, [])

            transaction.insert_or_update(
                test.TABLE, test.COLUMNS, test.ROW_DATA)

        self._db.run_in_transaction(_unit_of_work, test=self)

        with self._db.snapshot() as after:
            rows = list(after.execute_sql(self.SQL))
        self._check_rows_data(rows)

    def test_db_run_in_transaction_twice(self):
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        with self._db.batch() as batch:
            batch.delete(self.TABLE, self.ALL)

        def _unit_of_work(transaction, test):
            transaction.insert_or_update(
                test.TABLE, test.COLUMNS, test.ROW_DATA)

        self._db.run_in_transaction(_unit_of_work, test=self)
        self._db.run_in_transaction(_unit_of_work, test=self)

        with self._db.snapshot() as after:
            rows = list(after.execute_sql(self.SQL))
        self._check_rows_data(rows)

    def test_db_run_in_transaction_twice_4181(self):
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        with self._db.batch() as batch:
            batch.delete(COUNTERS_TABLE, self.ALL)

        def _unit_of_work(transaction, name):
            transaction.insert(COUNTERS_TABLE, COUNTERS_COLUMNS, [[name, 0]])

        self._db.run_in_transaction(_unit_of_work, name='id_1')

        with self.assertRaises(errors.RetryError) as expected:
            self._db.run_in_transaction(_unit_of_work, name='id_1')

        self.assertEqual(
            exc_to_code(expected.exception.cause), StatusCode.ALREADY_EXISTS)

        self._db.run_in_transaction(_unit_of_work, name='id_2')

        with self._db.snapshot() as after:
            rows = list(after.read(
                COUNTERS_TABLE, COUNTERS_COLUMNS, self.ALL))
        self.assertEqual(len(rows), 2)


class TestSessionAPI(unittest.TestCase, _TestData):
    DATABASE_NAME = 'test_sessions' + unique_resource_id('_')
    ALL_TYPES_TABLE = 'all_types'
    ALL_TYPES_COLUMNS = (
        'list_goes_on',
        'are_you_sure',
        'raw_data',
        'hwhen',
        'approx_value',
        'eye_d',
        'description',
        'exactly_hwhen',
    )
    SOME_DATE = datetime.date(2011, 1, 17)
    SOME_TIME = datetime.datetime(1989, 1, 17, 17, 59, 12, 345612)
    NANO_TIME = TimestampWithNanoseconds(1995, 8, 31, nanosecond=987654321)
    OTHER_NAN, = struct.unpack('<d', b'\x01\x00\x01\x00\x00\x00\xf8\xff')
    BYTES_1 = b'Ymlu'
    BYTES_2 = b'Ym9vdHM='
    ALL_TYPES_ROWDATA = (
        ([], False, None, None, 0.0, None, None, None),
        ([1], True, BYTES_1, SOME_DATE, 0.0, 19, u'dog', SOME_TIME),
        ([5, 10], True, BYTES_1, None, 1.25, 99, u'cat', None),
        ([], False, BYTES_2, None, float('inf'), 107, u'frog', None),
        ([3, None, 9], False, None, None, float('-inf'), 207, u'bat', None),
        ([], False, None, None, float('nan'), 1207, u'owl', None),
        ([], False, None, None, OTHER_NAN, 2000, u'virus', NANO_TIME),
    )

    @classmethod
    def setUpClass(cls):
        pool = BurstyPool()
        cls._db = Config.INSTANCE.database(
            cls.DATABASE_NAME, ddl_statements=DDL_STATEMENTS, pool=pool)
        operation = cls._db.create()
        operation.result(30)  # raises on failure / timeout.

    @classmethod
    def tearDownClass(cls):
        cls._db.drop()

    def setUp(self):
        self.to_delete = []

    def tearDown(self):
        for doomed in self.to_delete:
            doomed.delete()

    def test_session_crud(self):
        retry_true = RetryResult(operator.truth)
        retry_false = RetryResult(operator.not_)
        session = self._db.session()
        self.assertFalse(session.exists())
        session.create()
        retry_true(session.exists)()
        session.delete()
        retry_false(session.exists)()

    def test_batch_insert_then_read(self):
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        batch = session.batch()
        batch.delete(self.TABLE, self.ALL)
        batch.insert(self.TABLE, self.COLUMNS, self.ROW_DATA)
        batch.commit()

        snapshot = session.snapshot(read_timestamp=batch.committed)
        rows = list(snapshot.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_rows_data(rows)

    def test_batch_insert_then_read_string_array_of_string(self):
        TABLE = 'string_plus_array_of_string'
        COLUMNS = ['id', 'name', 'tags']
        ROWDATA = [
            (0, None, None),
            (1, 'phred', ['yabba', 'dabba', 'do']),
            (2, 'bharney', []),
            (3, 'wylma', ['oh', None, 'phred']),
        ]
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        with session.batch() as batch:
            batch.delete(TABLE, self.ALL)
            batch.insert(TABLE, COLUMNS, ROWDATA)

        snapshot = session.snapshot(read_timestamp=batch.committed)
        rows = list(snapshot.read(TABLE, COLUMNS, self.ALL))
        self._check_rows_data(rows, expected=ROWDATA)

    def test_batch_insert_then_read_all_datatypes(self):
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        with session.batch() as batch:
            batch.delete(self.ALL_TYPES_TABLE, self.ALL)
            batch.insert(
                self.ALL_TYPES_TABLE,
                self.ALL_TYPES_COLUMNS,
                self.ALL_TYPES_ROWDATA)

        snapshot = session.snapshot(read_timestamp=batch.committed)
        rows = list(snapshot.read(
            self.ALL_TYPES_TABLE, self.ALL_TYPES_COLUMNS, self.ALL))
        self._check_rows_data(rows, expected=self.ALL_TYPES_ROWDATA)

    def test_batch_insert_or_update_then_query(self):
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        with session.batch() as batch:
            batch.insert_or_update(self.TABLE, self.COLUMNS, self.ROW_DATA)

        snapshot = session.snapshot(read_timestamp=batch.committed)
        rows = list(snapshot.execute_sql(self.SQL))
        self._check_rows_data(rows)

    @RetryErrors(exception=GrpcRendezvous)
    def test_transaction_read_and_insert_then_rollback(self):
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        with session.batch() as batch:
            batch.delete(self.TABLE, self.ALL)

        transaction = session.transaction()
        transaction.begin()

        rows = list(transaction.read(self.TABLE, self.COLUMNS, self.ALL))
        self.assertEqual(rows, [])

        transaction.insert(self.TABLE, self.COLUMNS, self.ROW_DATA)

        # Inserted rows can't be read until after commit.
        rows = list(transaction.read(self.TABLE, self.COLUMNS, self.ALL))
        self.assertEqual(rows, [])
        transaction.rollback()

        rows = list(session.read(self.TABLE, self.COLUMNS, self.ALL))
        self.assertEqual(rows, [])

    def _transaction_read_then_raise(self, transaction):
        rows = list(transaction.read(self.TABLE, self.COLUMNS, self.ALL))
        self.assertEqual(len(rows), 0)
        transaction.insert(self.TABLE, self.COLUMNS, self.ROW_DATA)
        raise CustomException()

    @RetryErrors(exception=GrpcRendezvous)
    def test_transaction_read_and_insert_then_exception(self):
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        with session.batch() as batch:
            batch.delete(self.TABLE, self.ALL)

        with self.assertRaises(CustomException):
            session.run_in_transaction(self._transaction_read_then_raise)

        # Transaction was rolled back.
        rows = list(session.read(self.TABLE, self.COLUMNS, self.ALL))
        self.assertEqual(rows, [])

    @RetryErrors(exception=GrpcRendezvous)
    def test_transaction_read_and_insert_or_update_then_commit(self):
        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        with session.batch() as batch:
            batch.delete(self.TABLE, self.ALL)

        with session.transaction() as transaction:
            rows = list(transaction.read(self.TABLE, self.COLUMNS, self.ALL))
            self.assertEqual(rows, [])

            transaction.insert_or_update(
                self.TABLE, self.COLUMNS, self.ROW_DATA)

            # Inserted rows can't be read until after commit.
            rows = list(transaction.read(self.TABLE, self.COLUMNS, self.ALL))
            self.assertEqual(rows, [])

        rows = list(session.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_rows_data(rows)

    def _transaction_concurrency_helper(self, unit_of_work, pkey):
        INITIAL_VALUE = 123
        NUM_THREADS = 3     # conforms to equivalent Java systest.

        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        with session.batch() as batch:
            batch.insert_or_update(
                COUNTERS_TABLE, COUNTERS_COLUMNS, [[pkey, INITIAL_VALUE]])

        # We don't want to run the threads' transactions in the current
        # session, which would fail.
        txn_sessions = []

        for _ in range(NUM_THREADS):
            txn_session = self._db.session()
            txn_sessions.append(txn_session)
            txn_session.create()
            self.to_delete.append(txn_session)

        threads = [
            threading.Thread(
                target=txn_session.run_in_transaction,
                args=(unit_of_work, pkey))
            for txn_session in txn_sessions]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        keyset = KeySet(keys=[(pkey,)])
        rows = list(session.read(
            COUNTERS_TABLE, COUNTERS_COLUMNS, keyset))
        self.assertEqual(len(rows), 1)
        _, value = rows[0]
        self.assertEqual(value, INITIAL_VALUE + len(threads))

    def _read_w_concurrent_update(self, transaction, pkey):
        keyset = KeySet(keys=[(pkey,)])
        rows = list(transaction.read(
            COUNTERS_TABLE, COUNTERS_COLUMNS, keyset))
        self.assertEqual(len(rows), 1)
        pkey, value = rows[0]
        transaction.update(
            COUNTERS_TABLE, COUNTERS_COLUMNS, [[pkey, value + 1]])

    def test_transaction_read_w_concurrent_updates(self):
        PKEY = 'read_w_concurrent_updates'
        self._transaction_concurrency_helper(
            self._read_w_concurrent_update, PKEY)

    def _query_w_concurrent_update(self, transaction, pkey):
        SQL = 'SELECT * FROM counters WHERE name = @name'
        rows = list(transaction.execute_sql(
            SQL,
            params={'name': pkey},
            param_types={'name': Type(code=STRING)},
        ))
        self.assertEqual(len(rows), 1)
        pkey, value = rows[0]
        transaction.update(
            COUNTERS_TABLE, COUNTERS_COLUMNS, [[pkey, value + 1]])

    def test_transaction_query_w_concurrent_updates(self):
        PKEY = 'query_w_concurrent_updates'
        self._transaction_concurrency_helper(
            self._query_w_concurrent_update, PKEY)

    def test_transaction_read_w_abort(self):

        retry = RetryInstanceState(_has_all_ddl)
        retry(self._db.reload)()

        session = self._db.session()
        session.create()

        trigger = _ReadAbortTrigger()

        with session.batch() as batch:
            batch.delete(COUNTERS_TABLE, self.ALL)
            batch.insert(
                COUNTERS_TABLE,
                COUNTERS_COLUMNS,
                [[trigger.KEY1, 0], [trigger.KEY2, 0]])

        provoker = threading.Thread(
            target=trigger.provoke_abort, args=(self._db,))
        handler = threading.Thread(
            target=trigger.handle_abort, args=(self._db,))

        provoker.start()
        trigger.provoker_started.wait()

        handler.start()
        trigger.handler_done.wait()

        provoker.join()
        handler.join()

        rows = list(session.read(COUNTERS_TABLE, COUNTERS_COLUMNS, self.ALL))
        self._check_row_data(
            rows, expected=[[trigger.KEY1, 1], [trigger.KEY2, 1]])

    @staticmethod
    def _row_data(max_index):
        for index in range(max_index):
            yield [
                index,
                'First%09d' % (index,),
                'Last%09d' % (max_index - index),
                'test-%09d@example.com' % (index,),
            ]

    def _set_up_table(self, row_count, db=None):
        if db is None:
            db = self._db
            retry = RetryInstanceState(_has_all_ddl)
            retry(db.reload)()

        session = db.session()
        session.create()
        self.to_delete.append(session)

        def _unit_of_work(transaction, test):
            transaction.delete(test.TABLE, test.ALL)
            transaction.insert(
                test.TABLE, test.COLUMNS, test._row_data(row_count))

        committed = session.run_in_transaction(_unit_of_work, test=self)

        return session, committed

    def test_read_with_single_keys_index(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        session, committed = self._set_up_table(row_count)
        self.to_delete.append(session)
        expected = [[row[1], row[2]] for row in self._row_data(row_count)]
        row = 5
        keyset = [[expected[row][0], expected[row][1]]]
        results_iter = session.read(self.TABLE,
                                    columns,
                                    KeySet(keys=keyset),
                                    index='name'
        )
        rows = list(results_iter)
        self.assertEqual(rows, [expected[row]])

    def test_empty_read_with_single_keys_index(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        session, committed = self._set_up_table(row_count)
        self.to_delete.append(session)
        keyset = [["Non", "Existent"]]
        results_iter = session.read(self.TABLE,
                                    columns,
                                    KeySet(keys=keyset),
                                    index='name'
        )
        rows = list(results_iter)
        self.assertEqual(rows, [])

    def test_read_with_multiple_keys_index(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        session, committed = self._set_up_table(row_count)
        self.to_delete.append(session)
        expected = [[row[1], row[2]] for row in self._row_data(row_count)]
        rows = list(session.read(self.TABLE,
                                 columns,
                                 KeySet(keys=expected),
                                 index='name')
        )
        self.assertEqual(rows, expected)

    def test_snapshot_read_w_various_staleness(self):
        from datetime import datetime
        from google.cloud._helpers import UTC
        ROW_COUNT = 400
        session, committed = self._set_up_table(ROW_COUNT)
        all_data_rows = list(self._row_data(ROW_COUNT))

        before_reads = datetime.utcnow().replace(tzinfo=UTC)

        # Test w/ read timestamp
        read_tx = session.snapshot(read_timestamp=committed)
        rows = list(read_tx.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(rows, all_data_rows)

        # Test w/ min read timestamp
        min_read_ts = session.snapshot(min_read_timestamp=committed)
        rows = list(min_read_ts.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(rows, all_data_rows)

        staleness = datetime.utcnow().replace(tzinfo=UTC) - before_reads

        # Test w/ max staleness
        max_staleness = session.snapshot(max_staleness=staleness)
        rows = list(max_staleness.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(rows, all_data_rows)

        # Test w/ exact staleness
        exact_staleness = session.snapshot(exact_staleness=staleness)
        rows = list(exact_staleness.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(rows, all_data_rows)

        # Test w/ strong
        strong = session.snapshot()
        rows = list(strong.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(rows, all_data_rows)

    def test_multiuse_snapshot_read_isolation_strong(self):
        ROW_COUNT = 40
        session, committed = self._set_up_table(ROW_COUNT)
        all_data_rows = list(self._row_data(ROW_COUNT))
        strong = session.snapshot(multi_use=True)

        before = list(strong.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(before, all_data_rows)

        with self._db.batch() as batch:
            batch.delete(self.TABLE, self.ALL)

        after = list(strong.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(after, all_data_rows)

    def test_multiuse_snapshot_read_isolation_read_timestamp(self):
        ROW_COUNT = 40
        session, committed = self._set_up_table(ROW_COUNT)
        all_data_rows = list(self._row_data(ROW_COUNT))
        read_ts = session.snapshot(read_timestamp=committed, multi_use=True)

        before = list(read_ts.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(before, all_data_rows)

        with self._db.batch() as batch:
            batch.delete(self.TABLE, self.ALL)

        after = list(read_ts.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(after, all_data_rows)

    def test_multiuse_snapshot_read_isolation_exact_staleness(self):
        ROW_COUNT = 40

        session, committed = self._set_up_table(ROW_COUNT)
        all_data_rows = list(self._row_data(ROW_COUNT))

        time.sleep(1)
        delta = datetime.timedelta(microseconds=1000)

        exact = session.snapshot(exact_staleness=delta, multi_use=True)

        before = list(exact.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(before, all_data_rows)

        with self._db.batch() as batch:
            batch.delete(self.TABLE, self.ALL)

        after = list(exact.read(self.TABLE, self.COLUMNS, self.ALL))
        self._check_row_data(after, all_data_rows)

    def test_read_w_index(self):
        ROW_COUNT = 2000
        # Indexed reads cannot return non-indexed columns
        MY_COLUMNS = self.COLUMNS[0], self.COLUMNS[2]
        EXTRA_DDL = [
            'CREATE INDEX contacts_by_last_name ON contacts(last_name)',
        ]
        pool = BurstyPool()
        temp_db = Config.INSTANCE.database(
            'test_read_w_index', ddl_statements=DDL_STATEMENTS + EXTRA_DDL,
            pool=pool)
        operation = temp_db.create()
        self.to_delete.append(_DatabaseDropper(temp_db))

        # We want to make sure the operation completes.
        operation.result(30)  # raises on failure / timeout.

        session, committed = self._set_up_table(ROW_COUNT, db=temp_db)

        snapshot = session.snapshot(read_timestamp=committed)
        rows = list(snapshot.read(
            self.TABLE, MY_COLUMNS, self.ALL, index='contacts_by_last_name'))

        expected = list(reversed(
            [(row[0], row[2]) for row in self._row_data(ROW_COUNT)]))
        self._check_rows_data(rows, expected)

    def test_read_w_single_key(self):
        ROW_COUNT = 40
        session, committed = self._set_up_table(ROW_COUNT)

        snapshot = session.snapshot(read_timestamp=committed)
        rows = list(snapshot.read(
            self.TABLE, self.COLUMNS, KeySet(keys=[(0,)])))

        all_data_rows = list(self._row_data(ROW_COUNT))
        expected = [all_data_rows[0]]
        self._check_row_data(rows, expected)

    def test_empty_read(self):
        ROW_COUNT = 40
        session, committed = self._set_up_table(ROW_COUNT)
        rows = list(session.read(
            self.TABLE, self.COLUMNS, KeySet(keys=[(40,)])))
        self._check_row_data(rows, [])

    def test_read_w_multiple_keys(self):
        ROW_COUNT = 40
        indices = [0, 5, 17]
        session, committed = self._set_up_table(ROW_COUNT)

        snapshot = session.snapshot(read_timestamp=committed)
        rows = list(snapshot.read(
            self.TABLE, self.COLUMNS,
            KeySet(keys=[(index,) for index in indices])))

        all_data_rows = list(self._row_data(ROW_COUNT))
        expected = [row for row in all_data_rows if row[0] in indices]
        self._check_row_data(rows, expected)

    def test_read_w_limit(self):
        ROW_COUNT = 3000
        LIMIT = 100
        session, committed = self._set_up_table(ROW_COUNT)

        snapshot = session.snapshot(read_timestamp=committed)
        rows = list(snapshot.read(
            self.TABLE, self.COLUMNS, self.ALL, limit=LIMIT))

        all_data_rows = list(self._row_data(ROW_COUNT))
        expected = all_data_rows[:LIMIT]
        self._check_row_data(rows, expected)

    def test_read_w_ranges(self):
        ROW_COUNT = 3000
        START = 1000
        END = 2000
        session, committed = self._set_up_table(ROW_COUNT)
        snapshot = session.snapshot(read_timestamp=committed, multi_use=True)
        all_data_rows = list(self._row_data(ROW_COUNT))

        single_key = KeyRange(start_closed=[START], end_open=[START + 1])
        keyset = KeySet(ranges=(single_key,))
        rows = list(snapshot.read(self.TABLE, self.COLUMNS, keyset))
        expected = all_data_rows[START : START+1]
        self._check_rows_data(rows, expected)

        closed_closed = KeyRange(start_closed=[START], end_closed=[END])
        keyset = KeySet(ranges=(closed_closed,))
        rows = list(snapshot.read(
            self.TABLE, self.COLUMNS, keyset))
        expected = all_data_rows[START : END+1]
        self._check_row_data(rows, expected)

        closed_open = KeyRange(start_closed=[START], end_open=[END])
        keyset = KeySet(ranges=(closed_open,))
        rows = list(snapshot.read(
            self.TABLE, self.COLUMNS, keyset))
        expected = all_data_rows[START : END]
        self._check_row_data(rows, expected)

        open_open = KeyRange(start_open=[START], end_open=[END])
        keyset = KeySet(ranges=(open_open,))
        rows = list(snapshot.read(
            self.TABLE, self.COLUMNS, keyset))
        expected = all_data_rows[START+1 : END]
        self._check_row_data(rows, expected)

        open_closed = KeyRange(start_open=[START], end_closed=[END])
        keyset = KeySet(ranges=(open_closed,))
        rows = list(snapshot.read(
            self.TABLE, self.COLUMNS, keyset))
        expected = all_data_rows[START+1 : END+1]
        self._check_row_data(rows, expected)

    def test_read_with_range_keys_index_single_key(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        session, _ = self._set_up_table(row_count)
        self.to_delete.append(session)
        start = 3
        krange = KeyRange(start_closed=data[start], end_open=data[start + 1])
        keyset = KeySet(ranges=(krange,))
        rows = list(session.read(self.TABLE, columns, keyset, index='name'))
        self.assertEqual(rows, data[start : start+1])

    def test_read_with_range_keys_index_closed_closed(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        session, _ = self._set_up_table(row_count)
        self.to_delete.append(session)
        start, end = 3, 7
        krange = KeyRange(start_closed=data[start], end_closed=data[end])
        keyset = KeySet(ranges=(krange,))
        rows = list(session.read(self.TABLE, columns, keyset, index='name'))
        self.assertEqual(rows, data[start : end+1])

    def test_read_with_range_keys_index_closed_open(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        session, _ = self._set_up_table(row_count)
        self.to_delete.append(session)
        start, end = 3, 7
        krange = KeyRange(start_closed=data[start], end_open=data[end])
        keyset = KeySet(ranges=(krange,))
        rows = list(session.read(self.TABLE, columns, keyset, index='name'))
        self.assertEqual(rows, data[start:end])

    def test_read_with_range_keys_index_open_closed(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        session, _ = self._set_up_table(row_count)
        self.to_delete.append(session)
        start, end = 3, 7
        krange = KeyRange(start_open=data[start], end_closed=data[end])
        keyset = KeySet(ranges=(krange,))
        rows = list(session.read(self.TABLE, columns, keyset, index='name'))
        self.assertEqual(rows, data[start+1 : end+1])

    def test_read_with_range_keys_index_open_open(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        session, _ = self._set_up_table(row_count)
        self.to_delete.append(session)
        start, end = 3, 7
        krange = KeyRange(start_open=data[start], end_open=data[end])
        keyset = KeySet(ranges=(krange,))
        rows = list(session.read(self.TABLE, columns, keyset, index='name'))
        self.assertEqual(rows, data[start+1 : end])

    def test_read_with_range_keys_index_limit_closed_closed(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        session, _ = self._set_up_table(row_count)
        self.to_delete.append(session)
        start, end, limit = 3, 7, 2
        krange = KeyRange(start_closed=data[start], end_closed=data[end])
        keyset = KeySet(ranges=(krange,))
        rows = list(session.read(self.TABLE,
                                 columns,
                                 keyset,
                                 index='name',
                                 limit=limit))
        expected = data[start : end+1]
        self.assertEqual(rows, expected[:limit])

    def test_read_with_range_keys_index_limit_closed_open(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        session, _ = self._set_up_table(row_count)
        self.to_delete.append(session)
        start, end, limit = 3, 7, 2
        krange = KeyRange(start_closed=data[start], end_open=data[end])
        keyset = KeySet(ranges=(krange,))
        rows = list(session.read(self.TABLE,
                                 columns,
                                 keyset,
                                 index='name',
                                 limit=limit))
        expected = data[start:end]
        self.assertEqual(rows, expected[:limit])

    def test_read_with_range_keys_index_limit_open_closed(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        session, _ = self._set_up_table(row_count)
        self.to_delete.append(session)
        start, end, limit = 3, 7, 2
        krange = KeyRange(start_open=data[start], end_closed=data[end])
        keyset = KeySet(ranges=(krange,))
        rows = list(session.read(self.TABLE,
                                 columns,
                                 keyset,
                                 index='name',
                                 limit=limit))
        expected = data[start+1 : end+1]
        self.assertEqual(rows, expected[:limit])

    def test_read_with_range_keys_index_limit_open_open(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        session, _ = self._set_up_table(row_count)
        self.to_delete.append(session)
        start, end, limit = 3, 7, 2
        krange = KeyRange(start_open=data[start], end_open=data[end])
        keyset = KeySet(ranges=(krange,))
        rows = list(session.read(self.TABLE,
                                 columns,
                                 keyset,
                                 index='name',
                                 limit=limit))
        expected = data[start+1 : end]
        self.assertEqual(rows, expected[:limit])

    def test_read_with_range_keys_and_index_closed_closed(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]

        session, committed = self._set_up_table(row_count)
        self.to_delete.append(session)
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        keyrow, start, end = 1, 3, 7
        closed_closed = KeyRange(start_closed=data[start],
                                 end_closed=data[end])
        keys = [data[keyrow],]
        keyset = KeySet(keys=keys, ranges=(closed_closed,))
        rows = list(session.read(self.TABLE,
                                 columns,
                                 keyset,
                                 index='name')
        )
        expected = ([data[keyrow]] + data[start : end+1])
        self.assertEqual(rows, expected)

    def test_read_with_range_keys_and_index_closed_open(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        session, committed = self._set_up_table(row_count)
        self.to_delete.append(session)
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        keyrow, start, end = 1, 3, 7
        closed_open = KeyRange(start_closed=data[start],
                               end_open=data[end])
        keys = [data[keyrow],]
        keyset = KeySet(keys=keys, ranges=(closed_open,))
        rows = list(session.read(self.TABLE,
                                 columns,
                                 keyset,
                                 index='name')
        )
        expected = ([data[keyrow]] + data[start : end])
        self.assertEqual(rows, expected)

    def test_read_with_range_keys_and_index_open_closed(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        session, committed = self._set_up_table(row_count)
        self.to_delete.append(session)
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        keyrow, start, end = 1, 3, 7
        open_closed = KeyRange(start_open=data[start],
                               end_closed=data[end])
        keys = [data[keyrow],]
        keyset = KeySet(keys=keys, ranges=(open_closed,))
        rows = list(session.read(self.TABLE,
                                 columns,
                                 keyset,
                                 index='name')
        )
        expected = ([data[keyrow]] + data[start+1 : end+1])
        self.assertEqual(rows, expected)

    def test_read_with_range_keys_and_index_open_open(self):
        row_count = 10
        columns = self.COLUMNS[1], self.COLUMNS[2]
        session, committed = self._set_up_table(row_count)
        self.to_delete.append(session)
        data = [[row[1], row[2]] for row in self._row_data(row_count)]
        keyrow, start, end = 1, 3, 7
        open_open = KeyRange(start_open=data[start],
                             end_open=data[end])
        keys = [data[keyrow],]
        keyset = KeySet(keys=keys, ranges=(open_open,))
        rows = list(session.read(self.TABLE,
                                 columns,
                                 keyset,
                                 index='name')
        )
        expected = ([data[keyrow]] + data[start+1 : end])
        self.assertEqual(rows, expected)

    def test_execute_sql_w_manual_consume(self):
        ROW_COUNT = 3000
        session, committed = self._set_up_table(ROW_COUNT)

        snapshot = session.snapshot(read_timestamp=committed)
        streamed = snapshot.execute_sql(self.SQL)
        keyset = KeySet(all_=True)
        rows = list(session.read(self.TABLE, self.COLUMNS, keyset))
        self.assertEqual(list(streamed), rows)
        self.assertEqual(streamed._current_row, [])
        self.assertEqual(streamed._pending_chunk, None)

    def _check_sql_results(
            self, snapshot, sql, params, param_types, expected, order=True):
        if order and 'ORDER' not in sql:
            sql += ' ORDER BY eye_d'
        rows = list(snapshot.execute_sql(
            sql, params=params, param_types=param_types))
        self._check_rows_data(rows, expected=expected)

    def test_multiuse_snapshot_execute_sql_isolation_strong(self):
        ROW_COUNT = 40
        SQL = 'SELECT * FROM {}'.format(self.TABLE)
        session, committed = self._set_up_table(ROW_COUNT)
        all_data_rows = list(self._row_data(ROW_COUNT))
        strong = session.snapshot(multi_use=True)

        before = list(strong.execute_sql(SQL))
        self._check_row_data(before, all_data_rows)

        with self._db.batch() as batch:
            batch.delete(self.TABLE, self.ALL)

        after = list(strong.execute_sql(SQL))
        self._check_row_data(after, all_data_rows)

    def test_execute_sql_returning_array_of_struct(self):
        SQL = (
            "SELECT ARRAY(SELECT AS STRUCT C1, C2 "
            "FROM (SELECT 'a' AS C1, 1 AS C2 "
            "UNION ALL SELECT 'b' AS C1, 2 AS C2) "
            "ORDER BY C1 ASC)"
        )
        session = self._db.session()
        session.create()
        self.to_delete.append(session)
        snapshot = session.snapshot()
        self._check_sql_results(
            snapshot,
            sql=SQL,
            params=None,
            param_types=None,
            expected=[
                [[['a', 1], ['b', 2]]],
            ])

    def test_invalid_type(self):
        table = 'counters'
        columns = ('name', 'value')
        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        valid_input = (('', 0),)
        with session.batch() as batch:
            batch.delete(table, self.ALL)
            batch.insert(table, columns, valid_input)

        invalid_input = ((0, ''),)
        with self.assertRaises(errors.RetryError) as exc_info:
            with session.batch() as batch:
                batch.delete(table, self.ALL)
                batch.insert(table, columns, invalid_input)

        cause = exc_info.exception.cause
        self.assertEqual(cause.code(), StatusCode.FAILED_PRECONDITION)
        error_msg = (
            'Invalid value for column value in table '
            'counters: Expected INT64.')
        self.assertEqual(cause.details(), error_msg)

    def test_execute_sql_w_query_param(self):
        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        with session.batch() as batch:
            batch.delete(self.ALL_TYPES_TABLE, self.ALL)
            batch.insert(
                self.ALL_TYPES_TABLE,
                self.ALL_TYPES_COLUMNS,
                self.ALL_TYPES_ROWDATA)

        snapshot = session.snapshot(
            read_timestamp=batch.committed, multi_use=True)

        # Cannot equality-test array values.  See below for a test w/
        # array of IDs.

        self._check_sql_results(
            snapshot,
            sql='SELECT eye_d FROM all_types WHERE are_you_sure = @sure',
            params={'sure': True},
            param_types={'sure': Type(code=BOOL)},
            expected=[(19,), (99,)],
        )

        self._check_sql_results(
            snapshot,
            sql='SELECT eye_d FROM all_types WHERE raw_data = @bytes_1',
            params={'bytes_1': self.BYTES_1},
            param_types={'bytes_1': Type(code=BYTES)},
            expected=[(19,), (99,)],
        )

        self._check_sql_results(
            snapshot,
            sql='SELECT eye_d FROM all_types WHERE hwhen = @hwhen',
            params={'hwhen': self.SOME_DATE},
            param_types={'hwhen': Type(code=DATE)},
            expected=[(19,)],
        )

        self._check_sql_results(
            snapshot,
            sql='SELECT eye_d FROM all_types WHERE exactly_hwhen = @hwhen',
            params={'hwhen': self.SOME_TIME},
            param_types={'hwhen': Type(code=TIMESTAMP)},
            expected=[(19,)],
        )

        self._check_sql_results(
            snapshot,
            sql=('SELECT eye_d FROM all_types WHERE approx_value >= @lower'
                 ' AND approx_value < @upper '),
            params={'lower': 0.0, 'upper': 1.0},
            param_types={
                'lower': Type(code=FLOAT64), 'upper': Type(code=FLOAT64)},
            expected=[(None,), (19,)],
        )

        self._check_sql_results(
            snapshot,
            sql='SELECT description FROM all_types WHERE eye_d = @my_id',
            params={'my_id': 19},
            param_types={'my_id': Type(code=INT64)},
            expected=[(u'dog',)],
        )

        self._check_sql_results(
            snapshot,
            sql='SELECT description FROM all_types WHERE eye_d = @my_id',
            params={'my_id': None},
            param_types={'my_id': Type(code=INT64)},
            expected=[],
        )

        self._check_sql_results(
            snapshot,
            sql='SELECT eye_d FROM all_types WHERE description = @description',
            params={'description': u'dog'},
            param_types={'description': Type(code=STRING)},
            expected=[(19,)],
        )

        self._check_sql_results(
            snapshot,
            sql='SELECT eye_d FROM all_types WHERE exactly_hwhen = @hwhen',
            params={'hwhen': self.SOME_TIME},
            param_types={'hwhen': Type(code=TIMESTAMP)},
            expected=[(19,)],
        )

        int_array_type = Type(code=ARRAY, array_element_type=Type(code=INT64))

        self._check_sql_results(
            snapshot,
            sql=('SELECT description FROM all_types '
                 'WHERE eye_d in UNNEST(@my_list)'),
            params={'my_list': [19, 99]},
            param_types={'my_list': int_array_type},
            expected=[(u'dog',), (u'cat',)],
        )

        str_array_type = Type(code=ARRAY, array_element_type=Type(code=STRING))

        self._check_sql_results(
            snapshot,
            sql=('SELECT eye_d FROM all_types '
                 'WHERE description in UNNEST(@my_list)'),
            params={'my_list': []},
            param_types={'my_list': str_array_type},
            expected=[],
        )

        self._check_sql_results(
            snapshot,
            sql=('SELECT eye_d FROM all_types '
                 'WHERE description in UNNEST(@my_list)'),
            params={'my_list': [u'dog', u'cat']},
            param_types={'my_list': str_array_type},
            expected=[(19,), (99,)],
        )

        self._check_sql_results(
            snapshot,
            sql='SELECT @v',
            params={'v': None},
            param_types={'v': Type(code=STRING)},
            expected=[(None,)],
            order=False,
        )

    def test_execute_sql_w_query_param_transfinite(self):
        session = self._db.session()
        session.create()
        self.to_delete.append(session)

        with session.batch() as batch:
            batch.delete(self.ALL_TYPES_TABLE, self.ALL)
            batch.insert(
                self.ALL_TYPES_TABLE,
                self.ALL_TYPES_COLUMNS,
                self.ALL_TYPES_ROWDATA)

        snapshot = session.snapshot(
            read_timestamp=batch.committed, multi_use=True)

        # Find -inf
        self._check_sql_results(
            snapshot,
            sql='SELECT eye_d FROM all_types WHERE approx_value = @neg_inf',
            params={'neg_inf': float('-inf')},
            param_types={'neg_inf': Type(code=FLOAT64)},
            expected=[(207,)],
        )

        # Find +inf
        self._check_sql_results(
            snapshot,
            sql='SELECT eye_d FROM all_types WHERE approx_value = @pos_inf',
            params={'pos_inf': float('+inf')},
            param_types={'pos_inf': Type(code=FLOAT64)},
            expected=[(107,)],
        )

        rows = list(snapshot.execute_sql(
            'SELECT'
            ' [CAST("-inf" AS FLOAT64),'
            ' CAST("+inf" AS FLOAT64),'
            ' CAST("NaN" AS FLOAT64)]'))
        self.assertEqual(len(rows), 1)
        float_array, = rows[0]
        self.assertEqual(float_array[0], float('-inf'))
        self.assertEqual(float_array[1], float('+inf'))
        # NaNs cannot be searched for by equality.
        self.assertTrue(math.isnan(float_array[2]))


class TestStreamingChunking(unittest.TestCase, _TestData):

    @classmethod
    def setUpClass(cls):
        from tests.system.utils.streaming_utils import INSTANCE_NAME
        from tests.system.utils.streaming_utils import DATABASE_NAME

        instance = Config.CLIENT.instance(INSTANCE_NAME)
        if not instance.exists():
            raise unittest.SkipTest(
                "Run 'tests/system/utils/populate_streaming.py' to enable.")

        database = instance.database(DATABASE_NAME)
        if not instance.exists():
            raise unittest.SkipTest(
                "Run 'tests/system/utils/populate_streaming.py' to enable.")

        cls._db = database

    def _verify_one_column(self, table_desc):
        sql = 'SELECT chunk_me FROM {}'.format(table_desc.table)
        with self._db.snapshot() as snapshot:
            rows = list(snapshot.execute_sql(sql))
        self.assertEqual(len(rows), table_desc.row_count)
        expected = table_desc.value()
        for row in rows:
            self.assertEqual(row[0], expected)

    def _verify_two_columns(self, table_desc):
        sql = 'SELECT chunk_me, chunk_me_2 FROM {}'.format(table_desc.table)
        with self._db.snapshot() as snapshot:
            rows = list(snapshot.execute_sql(sql))
        self.assertEqual(len(rows), table_desc.row_count)
        expected = table_desc.value()
        for row in rows:
            self.assertEqual(row[0], expected)
            self.assertEqual(row[1], expected)

    def test_four_kay(self):
        from tests.system.utils.streaming_utils import FOUR_KAY
        self._verify_one_column(FOUR_KAY)

    def test_forty_kay(self):
        from tests.system.utils.streaming_utils import FOUR_KAY
        self._verify_one_column(FOUR_KAY)

    def test_four_hundred_kay(self):
        from tests.system.utils.streaming_utils import FOUR_HUNDRED_KAY
        self._verify_one_column(FOUR_HUNDRED_KAY)

    def test_four_meg(self):
        from tests.system.utils.streaming_utils import FOUR_MEG
        self._verify_two_columns(FOUR_MEG)


class CustomException(Exception):
    """Placeholder for any user-defined exception."""


class _DatabaseDropper(object):
    """Helper for cleaning up databases created on-the-fly."""

    def __init__(self, db):
        self._db = db

    def delete(self):
        self._db.drop()


class _ReadAbortTrigger(object):
    """Helper for tests provoking abort-during-read."""

    KEY1 = 'key1'
    KEY2 = 'key2'

    def __init__(self):
        self.provoker_started = threading.Event()
        self.provoker_done = threading.Event()
        self.handler_running = threading.Event()
        self.handler_done = threading.Event()

    def _provoke_abort_unit_of_work(self, transaction):
        keyset = KeySet(keys=[(self.KEY1,)])
        rows = list(
            transaction.read(COUNTERS_TABLE, COUNTERS_COLUMNS, keyset))

        assert len(rows) == 1
        row = rows[0]
        value = row[1]

        self.provoker_started.set()

        self.handler_running.wait()

        transaction.update(
            COUNTERS_TABLE, COUNTERS_COLUMNS, [[self.KEY1, value + 1]])

    def provoke_abort(self, database):
        database.run_in_transaction(self._provoke_abort_unit_of_work)
        self.provoker_done.set()

    def _handle_abort_unit_of_work(self, transaction):
        keyset_1 = KeySet(keys=[(self.KEY1,)])
        rows_1 = list(
            transaction.read(COUNTERS_TABLE, COUNTERS_COLUMNS, keyset_1))

        assert len(rows_1) == 1
        row_1 = rows_1[0]
        value_1 = row_1[1]

        self.handler_running.set()

        self.provoker_done.wait()

        keyset_2 = KeySet(keys=[(self.KEY2,)])
        rows_2 = list(
            transaction.read(COUNTERS_TABLE, COUNTERS_COLUMNS, keyset_2))

        assert len(rows_2) == 1
        row_2 = rows_2[0]
        value_2 = row_2[1]

        transaction.update(
            COUNTERS_TABLE, COUNTERS_COLUMNS, [[self.KEY2, value_1 + value_2]])

    def handle_abort(self, database):
        database.run_in_transaction(self._handle_abort_unit_of_work)
        self.handler_done.set()
