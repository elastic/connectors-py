#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiomysql
import pytest

from connectors.byoc import Filter
from connectors.filtering.validation import SyncRuleValidationResult
from connectors.source import ConfigurableFieldValueError, DataSourceConfiguration
from connectors.sources.mysql import (
    MySQLAdvancedRulesValidator,
    MySQLClient,
    MySqlDataSource,
    parse_tables_string_to_list_of_tables,
)
from connectors.sources.tests.support import create_source
from connectors.tests.commons import AsyncIterator


def immutable_doc(**kwargs):
    return frozenset(kwargs.items())


ADVANCED_SNIPPET = "advanced_snippet"

DATABASE = "database"

TABLE_ONE = "table1"
TABLE_TWO = "table2"
TABLE_THREE = "table3"

DOC_ONE = immutable_doc(id=1, text="some text 1")
DOC_TWO = immutable_doc(id=2, text="some text 2")
DOC_THREE = immutable_doc(id=3, text="some text 3")
DOC_FOUR = immutable_doc(id=4, text="some text 4")
DOC_FIVE = immutable_doc(id=5, text="some text 5")
DOC_SIX = immutable_doc(id=6, text="some text 6")
DOC_SEVEN = immutable_doc(id=7, text="some text 7")
DOC_EIGHT = immutable_doc(id=8, text="some text 8")

TABLE_ONE_QUERY_ALL = "query all db one table one"
TABLE_ONE_QUERY_DOC_ONE = "query doc one"
TABLE_TWO_QUERY_ALL = "query all db one table two"

DB_TWO_TABLE_ONE_QUERY_ALL = "query all db two table one"
DB_TWO_TABLE_TWO_QUERY_ALL = "query all db two table two"

ALL_DOCS = "all_docs"
ONLY_DOC_ONE = "only_doc_one"

ACCESSIBLE = "accessible"
INACCESSIBLE = "inaccessible"

MYSQL = {
    TABLE_ONE: {
        TABLE_ONE_QUERY_ALL: [DOC_ONE, DOC_TWO],
        TABLE_ONE_QUERY_DOC_ONE: [DOC_ONE],
    },
    TABLE_TWO: {TABLE_TWO_QUERY_ALL: [DOC_THREE, DOC_FOUR]},
}


def future_with_result(result):
    future = asyncio.Future()
    future.set_result(result)

    return future


@pytest.fixture
def patch_fetch_tables():
    with patch.object(
        MySqlDataSource, "fetch_all_tables", side_effect=([])
    ) as fetch_tables:
        yield fetch_tables


@pytest.fixture
def patch_ping():
    with patch.object(MySqlDataSource, "ping", return_value=AsyncMock()) as ping:
        yield ping


@pytest.fixture
def patch_fetch_rows_for_table():
    with patch.object(MySqlDataSource, "fetch_rows_for_table") as mock_to_patch:
        yield mock_to_patch


@pytest.fixture
def patch_default_wait_multiplier():
    with patch("connectors.sources.mysql.RETRY_INTERVAL", 0):
        yield


@pytest.fixture
def patch_connection_pool():
    connection_pool = Mock()
    connection_pool.close = Mock()
    connection_pool.wait_closed = AsyncMock()
    connection_pool.acquire = AsyncMock(return_value=Connection())

    with patch(
        "aiomysql.create_pool",
        return_value=future_with_result(connection_pool),
    ):
        yield connection_pool


def test_get_configuration():
    """Test get_configuration method of MySQL"""
    klass = MySqlDataSource

    config = DataSourceConfiguration(klass.get_default_configuration())

    assert config["host"] == "127.0.0.1"
    assert config["port"] == 3306


class Result:
    """This class contains method which returns dummy response"""

    def result(self):
        """Result method which returns dummy result"""
        return [["table1"], ["table2"]]


class Cursor:
    """This class contains methods which returns dummy response"""

    async def __aenter__(self):
        """Make a dummy database connection and return it"""
        return self

    def __init__(self, *args, **kw):
        self.first_call = True
        self.description = [["Database"]]

    def fetchall(self):
        """This method returns object of Return class"""
        futures_object = asyncio.Future()
        futures_object.set_result([["table1"], ["table2"]])
        return futures_object

    async def fetchmany(self, size=1):
        """This method returns response of fetchmany"""
        if self.first_call:
            self.first_call = False
            return [["table1"], ["table2"]]
        if self.is_connection_lost:
            raise Exception("Incomplete Read Error")
        return []

    async def scroll(self, *args, **kw):
        raise Exception("Incomplete Read Error")

    def execute(self, query):
        """This method returns future object"""
        futures_object = asyncio.Future()
        futures_object.set_result(MagicMock())
        return futures_object

    async def __aexit__(self, exception_type, exception_value, exception_traceback):
        """Make sure the dummy database connection gets closed"""
        pass


class Connection:
    """This class contains methods which returns dummy connection response"""

    async def __aenter__(self):
        """Make a dummy database connection and return it"""
        return self

    async def ping(self):
        """This method returns object of Result class"""
        return True

    async def cursor(self):
        """This method returns object of Result class"""
        return Cursor

    async def __aexit__(self, exception_type, exception_value, exception_traceback):
        """Make sure the dummy database connection gets closed"""
        pass


class MockSsl:
    """This class contains methods which returns dummy ssl context"""

    def load_verify_locations(self, cadata):
        """This method verify locations"""
        pass


async def mock_mysql_response():
    """Creates mock response

    Returns:
        Mock Object: Mock response
    """
    mock_response = asyncio.Future()
    mock_response.set_result(MagicMock())

    return mock_response


@pytest.mark.asyncio
async def test_close_when_source_setup_correctly_does_not_raise_errors():
    source = await setup_mysql_source()

    await source.close()


@pytest.mark.asyncio
async def test_client_get_tables(patch_connection_pool):
    table_1 = "table_1"
    table_2 = "table_2"

    fetchall_tables_response = [
        (table_1,),
        (table_2,),
    ]

    mock_cursor = MagicMock(spec=aiomysql.Cursor)
    mock_cursor.fetchall = AsyncMock(return_value=fetchall_tables_response)
    mock_cursor.__aenter__.return_value = mock_cursor

    mock_connection = MagicMock(spec=aiomysql.Connection)
    mock_connection.cursor.return_value = mock_cursor
    mock_connection.__aenter__.return_value = mock_connection

    patch_connection_pool.acquire.return_value = mock_connection

    client = await setup_mysql_client()

    async with client:
        result = await client.get_all_table_names()
        expected_result = [table_1, table_2]

        assert result == expected_result


@pytest.mark.asyncio
async def test_client_get_column_names(patch_connection_pool):
    table = "table"
    column_1 = "column_1"
    column_2 = "column_2"

    description_response = [
        (column_1,),
        (column_2,),
    ]

    mock_cursor = MagicMock(spec=aiomysql.Cursor)
    mock_cursor.description = description_response
    mock_cursor.__aenter__.return_value = mock_cursor

    mock_connection = MagicMock(spec=aiomysql.Connection)
    mock_connection.cursor.return_value = mock_cursor
    mock_connection.__aenter__.return_value = mock_connection

    patch_connection_pool.acquire.return_value = mock_connection

    client = await setup_mysql_client()

    async with client:
        result = await client.get_column_names(table)
        expected_result = [f"{table}_{column_1}", f"{table}_{column_2}"]

        assert result == expected_result


@pytest.mark.asyncio
async def test_client_ping(patch_logger, patch_connection_pool):
    client = await setup_mysql_client()

    async with client:
        await client.ping()


@pytest.mark.asyncio
async def test_client_ping_negative(patch_logger):
    client = await setup_mysql_client()

    mock_response = asyncio.Future()
    mock_response.set_result(Mock())

    client.connection_pool = await mock_response

    with patch.object(aiomysql, "create_pool", return_value=mock_response):
        with pytest.raises(Exception):
            await client.ping()


@pytest.mark.asyncio
async def test_connect_with_retry(
    patch_logger, patch_connection_pool, patch_default_wait_multiplier
):
    source = await setup_mysql_source()

    streamer = source._connect(query="select * from database.table", fetch_many=True)

    with pytest.raises(Exception):
        async for _ in streamer:
            pass


@pytest.mark.asyncio
async def test_fetch_documents(patch_connection_pool):
    last_update_time = "2023-01-18 17:18:56"
    primary_key_col = "pk"
    column = "column"

    document = {
        "Table": "table_name",
        "_id": "table_name_",
        "_timestamp": last_update_time,
        f"table_name_{column}": "table1",
    }

    source = await setup_mysql_source(DATABASE)
    source._get_primary_key_columns = AsyncMock(return_value=[primary_key_col])
    source._connect = AsyncIterator([[[last_update_time]], [column], document])

    query = "select * from table"

    document_list = []
    async for document in source.fetch_documents(table="table_name", query=query):
        document_list.append(document)

    assert document in document_list


@pytest.mark.asyncio
async def test_fetch_rows_from_tables(patch_connection_pool):
    document = {"_id": 1}

    source = await setup_mysql_source()
    source.fetch_rows_for_table = AsyncIterator([document])

    async for row in source.fetch_rows_from_tables("table"):
        assert "_id" in row


@pytest.mark.asyncio
async def test_get_docs(patch_connection_pool):
    source = await setup_mysql_source(DATABASE)

    source.get_tables_to_fetch = AsyncMock(return_value=["table"])
    source.fetch_rows_from_tables = AsyncIterator([{"a": 1, "b": 2}])

    async for doc, _ in source.get_docs():
        assert doc == {"a": 1, "b": 2}


async def setup_mysql_client():
    client = MySQLClient(
        host="host",
        port=123,
        user="user",
        password="password",
        ssl_enabled=False,
        ssl_certificate="",
    )

    return client


async def setup_mysql_source(database=""):
    source = create_source(MySqlDataSource)
    source.configuration.set_field(
        name="database", label="Database", value=database, type="str"
    )

    source.database = database
    source._mysql_client = MagicMock()

    return source


def setup_available_docs(advanced_snippet):
    available_docs = []

    for table in advanced_snippet:
        query = advanced_snippet[table]
        available_docs += MYSQL[table][query]

    return available_docs


@pytest.mark.parametrize(
    "filtering, expected_docs",
    [
        (
            # single table, multiple docs
            Filter(
                {
                    ADVANCED_SNIPPET: {
                        "value": {
                            TABLE_ONE: TABLE_ONE_QUERY_ALL,
                        }
                    }
                }
            ),
            {DOC_ONE, DOC_TWO},
        ),
        (
            # single table, single doc
            Filter({ADVANCED_SNIPPET: {"value": {TABLE_ONE: TABLE_ONE_QUERY_DOC_ONE}}}),
            {DOC_ONE},
        ),
        (
            # multiple tables, multiple docs
            Filter(
                {
                    ADVANCED_SNIPPET: {
                        "value": {
                            TABLE_ONE: TABLE_ONE_QUERY_DOC_ONE,
                            TABLE_TWO: TABLE_TWO_QUERY_ALL,
                        }
                    }
                }
            ),
            {DOC_ONE, DOC_THREE, DOC_FOUR},
        ),
    ],
)
@pytest.mark.asyncio
async def test_get_docs_with_advanced_rules(
    filtering, expected_docs, patch_fetch_rows_for_table
):
    source = await setup_mysql_source(DATABASE)
    docs_in_db = setup_available_docs(filtering.get_advanced_rules())
    patch_fetch_rows_for_table.return_value = AsyncIterator(docs_in_db)

    yielded_docs = set()
    async for doc, _ in source.get_docs(filtering):
        yielded_docs.add(doc)

    assert yielded_docs == expected_docs


@pytest.mark.asyncio
async def test_validate_config_when_host_empty_then_raise_error():
    source = create_source(MySqlDataSource, host="")

    with pytest.raises(ConfigurableFieldValueError):
        await source.validate_config()


@pytest.mark.asyncio
async def test_validate_config_when_port_has_wrong_type_then_raise_error():
    source = create_source(MySqlDataSource)
    source.configuration.set_field(name="port", value="port")

    with pytest.raises(ConfigurableFieldValueError):
        await source.validate_config()


@pytest.mark.parametrize(
    "datasource, advanced_rules, expected_validation_result",
    [
        (
            {},
            {},
            SyncRuleValidationResult.valid_result(
                SyncRuleValidationResult.ADVANCED_RULES
            ),
        ),
        (
            {TABLE_ONE: {}},
            {TABLE_ONE: {}},
            SyncRuleValidationResult.valid_result(
                SyncRuleValidationResult.ADVANCED_RULES
            ),
        ),
        (
            {TABLE_ONE: {}, TABLE_TWO: {}},
            {TABLE_ONE: {}, TABLE_TWO: {}},
            SyncRuleValidationResult.valid_result(
                SyncRuleValidationResult.ADVANCED_RULES
            ),
        ),
        (
            {},
            {TABLE_ONE: {}},
            SyncRuleValidationResult(
                rule_id=SyncRuleValidationResult.ADVANCED_RULES,
                is_valid=False,
                validation_message=f"Tables not found or inaccessible: {TABLE_ONE}.",
            ),
        ),
        (
            {},
            {TABLE_ONE: {}, TABLE_TWO: {}},
            SyncRuleValidationResult(
                rule_id=SyncRuleValidationResult.ADVANCED_RULES,
                is_valid=False,
                validation_message=f"Tables not found or inaccessible: {TABLE_ONE}, {TABLE_TWO}.",
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_advanced_rules_tables_validation(
    datasource,
    advanced_rules,
    expected_validation_result,
    patch_fetch_tables,
    patch_ping,
):
    patch_fetch_tables.side_effect = [
        map(lambda table: (table, None), datasource.keys())
    ]

    source = create_source(MySqlDataSource)
    validation_result = await MySQLAdvancedRulesValidator(source).validate(
        advanced_rules
    )

    assert validation_result == expected_validation_result


@pytest.mark.parametrize("tables", ["*", ["*"]])
@pytest.mark.asyncio
async def test_get_tables_when_wildcard_configured_then_fetch_all_tables(tables):
    source = create_source(MySqlDataSource)
    source.fetch_all_tables = AsyncMock(return_value="table")

    await source.get_tables_to_fetch()

    assert source.fetch_all_tables.call_count == 1


@pytest.mark.asyncio
async def test_validate_database_accessible_when_accessible_then_no_error_raised():
    source = create_source(MySqlDataSource)
    source.database = "test_database"

    cursor = AsyncMock()
    cursor.execute.return_value = None

    await source._validate_database_accessible(cursor)
    cursor.execute.assert_called_with(f"USE {source.database};")


@pytest.mark.asyncio
async def test_validate_database_accessible_when_not_accessible_then_error_raised():
    source = create_source(MySqlDataSource)

    cursor = AsyncMock()
    cursor.execute.side_effect = aiomysql.Error("Error")

    with pytest.raises(ConfigurableFieldValueError):
        await source._validate_database_accessible(cursor)


@pytest.mark.asyncio
async def test_validate_tables_accessible_when_accessible_then_no_error_raised():
    source = create_source(MySqlDataSource)
    source.tables = ["table_1", "table_2", "table_3"]
    source.fetch_all_tables = AsyncMock(return_value=["table_1", "table_2", "table_3"])

    cursor = AsyncMock()
    cursor.execute.return_value = None

    await source._validate_tables_accessible(cursor)


@pytest.mark.parametrize("tables", ["*", ["*"]])
@pytest.mark.asyncio
async def test_validate_tables_accessible_when_accessible_and_wildcard_then_no_error_raised(
    tables,
):
    source = create_source(MySqlDataSource)
    source.tables = tables
    source.fetch_all_tables = AsyncMock(return_value=["table_1", "table_2", "table_3"])

    cursor = AsyncMock()
    cursor.execute.return_value = None

    await source._validate_tables_accessible(cursor)

    assert source.fetch_all_tables.call_count == 1


@pytest.mark.asyncio
async def test_validate_tables_accessible_when_not_accessible_then_error_raised():
    source = create_source(MySqlDataSource)
    source.tables = ["table1"]
    source.fetch_all_tables = AsyncMock(return_value=["table1"])

    cursor = AsyncMock()
    cursor.execute.side_effect = aiomysql.Error("Error")

    with pytest.raises(ConfigurableFieldValueError):
        await source._validate_tables_accessible(cursor)


@pytest.mark.parametrize(
    "tables_string, expected_tables_list",
    [
        (None, []),
        ("", []),
        ("table_1", ["table_1"]),
        ("table_1, ", ["table_1"]),
        ("`table_1,`,", ["`table_1,`"]),
        ("table_1, table_2", ["table_1", "table_2"]),
        ("`table_1,abc`", ["`table_1,abc`"]),
        ("`table_1,abc`, table_2", ["`table_1,abc`", "table_2"]),
        ("`table_1,abc`, `table_2,def`", ["`table_1,abc`", "`table_2,def`"]),
    ],
)
def test_parse_tables_string_to_list(tables_string, expected_tables_list):
    assert parse_tables_string_to_list_of_tables(tables_string) == expected_tables_list


@pytest.mark.parametrize(
    "primary_key_tuples, expected_primary_key_columns",
    [
        ([], []),
        ([("id",)], [f"{TABLE_ONE}_id"]),
        (
            [("group",), ("class",), ("name",)],
            [
                f"{TABLE_ONE}_group",
                f"{TABLE_ONE}_class",
                f"{TABLE_ONE}_name",
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_get_primary_key_columns(
    primary_key_tuples, expected_primary_key_columns
):
    source = create_source(MySqlDataSource)
    source._connect = AsyncIterator([primary_key_tuples])

    primary_key_columns = await source._get_primary_key_columns(TABLE_ONE)

    assert primary_key_columns == expected_primary_key_columns


@pytest.mark.parametrize(
    "row, primary_key_columns, expected_id",
    [
        ({"key_1": 1, "key_2": 2}, ["key_1"], f"{TABLE_ONE}_1_"),
        ({"key_1": 1, "key_2": 2}, ["key_1", "key_2"], f"{TABLE_ONE}_1_2_"),
        ({"key_1": 1, "key_2": 2}, ["key_1", "key_3"], f"{TABLE_ONE}_1_"),
    ],
)
def test_generate_id(row, primary_key_columns, expected_id):
    source = create_source(MySqlDataSource)

    row_id = source._generate_id(TABLE_ONE, row, primary_key_columns)

    assert row_id == expected_id
