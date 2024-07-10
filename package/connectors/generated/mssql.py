# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
#
# This is a generated code. Do not modify directly.
# Run `make generate_connector_package` to update.

from connectors.source import DataSourceConfiguration
from connectors.sources.mssql import MSSQLDataSource
from package.connectors.connector_base import ConnectorBase


class MSSQLConnector(ConnectorBase):
    """
    MSSQLConnector class generated for connecting to the data source.

    Args:

        host (str): Host

        port (int): Port

        username (str): Username

        password (str): Password

        database (str): Database

        tables (list): Comma-separated list of tables
            - This configurable field is ignored when Advanced Sync Rules are used.

        fetch_size (int): Rows fetched per request

        retry_count (int): Retries per request

        schema (str): Schema

        ssl_enabled (bool): Enable SSL verification

        ssl_ca (str): SSL certificate

        validate_host (bool): Validate host

    """

    def __init__(
        self,
        host=None,
        port=None,
        username=None,
        password=None,
        database=None,
        tables="*",
        fetch_size=50,
        retry_count=3,
        schema=None,
        ssl_enabled=False,
        ssl_ca=None,
        validate_host=False,
        **kwargs
    ):

        configuration = MSSQLDataSource.get_default_configuration()

        # Apply the user provided configuration in the class constructor
        args = locals()
        for key in configuration.keys():
            if args.get(key) is not None:
                configuration[key]["value"] = args[key]

        connector_configuration = DataSourceConfiguration(configuration)

        super().__init__(
            data_provider=MSSQLDataSource(connector_configuration), **kwargs
        )

        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.database = database
        self.tables = tables
        self.fetch_size = fetch_size
        self.retry_count = retry_count
        self.schema = schema
        self.ssl_enabled = ssl_enabled
        self.ssl_ca = ssl_ca
        self.validate_host = validate_host
