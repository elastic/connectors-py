#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
"""Salesforce source module responsible to fetch documents from Salesforce."""
from contextlib import contextmanager
from functools import cached_property
from threading import Lock

import aiohttp
from aiohttp.client_exceptions import ClientResponseError

from connectors.logger import logger
from connectors.source import BaseDataSource
from connectors.utils import CancellableSleeps, retryable

RETRIES = 3
RETRY_INTERVAL = 1

BASE_URL = "https://<domain>.my.salesforce.com"
API_VERSION = "v58.0"
TOKEN_ENDPOINT = "/services/oauth2/token"
QUERY_ENDPOINT = f"/services/data/{API_VERSION}/query"
DESCRIBE_ENDPOINT = f"/services/data/{API_VERSION}/sobjects"
DESCRIBE_SOBJECT_ENDPOINT = f"/services/data/{API_VERSION}/sobjects/<sobject>/describe"

RELEVANT_SOBJECTS = ["Account", "Contact", "Lead", "Opportunity", "User"]
RELEVANT_SOBJECT_FIELDS = [
    "AccountId",
    "BillingAddress",
    "Company",
    "ConvertedAccountId",
    "ConvertedContactId",
    "ConvertedDate",
    "ConvertedOpportunityId",
    "Department",
    "Description",
    "Email",
    "LeadSource",
    "Name",
    "OwnerId",
    "Phone",
    "PhotoUrl",
    "Rating",
    "StageName",
    "Status",
    "Title",
    "Type",
    "Website",
]


class RateLimitedException(Exception):
    """Notifies that Salesforce has begun rate limiting the current account"""

    pass


class RequestRefusedException(Exception):
    """Notifies that a request to Saleforce was rejected"""

    pass


class InvalidQueryException(Exception):
    """Notifies that a query was malformed or otherwise incorrect"""

    pass


class InvalidCredentialsException(Exception):
    """Notifies that credentials are invalid for fetching a Salesforce token"""

    pass


class TokenFetchException(Exception):
    """Notifies that an unexpected error occurred when fetching a Salesforce token"""

    pass


class RequestError(Exception):
    """Notifies that a general uncaught 400 error occurred during a request"""

    pass


class LockedException(Exception):
    """Notifies that the current process is locked, only for token generation"""

    pass


class SalesforceClient:
    def __init__(self, configuration):
        self._logger = logger
        self._sleeps = CancellableSleeps()

        self._queryable_sobjects = None
        self._queryable_sobject_fields = None
        self._sobjects_cache_by_type = None

        self.base_url = BASE_URL.replace("<domain>", configuration["domain"])
        self.api_token = SalesforceAPIToken(
            self.session,
            self.base_url,
            configuration["client_id"],
            configuration["client_secret"],
        )
        self.doc_mapper = SalesforceDocMapper(self.base_url)

    def set_logger(self, logger_):
        self._logger = logger_

    @cached_property
    def session(self):
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None),
        )

    @retryable(
        retries=RETRIES,
        interval=RETRY_INTERVAL,
        skipped_exceptions=[InvalidCredentialsException],
    )
    async def get_token(self):
        while True:
            try:
                await self.api_token.generate()
                break
            except LockedException:
                self._logger.debug("Token generation is already in process.")
                break
            except Exception as e:
                raise e

    async def ping(self):
        # TODO ping something of value (this could be config check instead)
        await self.session.head(self.base_url)

    async def close(self):
        self.api_token.clear()
        if self.session is not None:
            await self.session.close()
            del self.session

    async def get_docs(self):
        async for account in self.get_accounts():
            yield account, None

        async for opportunity in self.get_opportunities():
            yield opportunity, None

        async for contact in self.get_contacts():
            yield contact, None

        async for lead in self.get_leads():
            yield lead, None

    async def get_accounts(self):
        if not await self._is_queryable("Account"):
            self._logger.info(
                "Object Account is not queryable, so they won't be ingested."
            )
            return

        query = await self._accounts_query()
        async for records in self._yield_non_bulk_query_pages(query):
            for record in records:
                yield self.doc_mapper.map_account(record)

    async def get_opportunities(self):
        if not await self._is_queryable("Opportunity"):
            self._logger.info(
                "ObjectOpportunity is not queryable, so they won't be ingested."
            )
            return

        query = await self._opportunities_query()
        async for records in self._yield_non_bulk_query_pages(query):
            for record in records:
                yield self.doc_mapper.map_opportunity(record)

    async def get_contacts(self):
        if not await self._is_queryable("Contact"):
            self._logger.info(
                "ObjectContact is not queryable, so they won't be ingested."
            )
            return

        query = await self._contacts_query()
        async for records in self._yield_non_bulk_query_pages(query):
            for record in records:
                sobjects_by_id = await self.sobjects_cache_by_type()
                record["Account"] = sobjects_by_id["Account"].get(
                    record.get("AccountId"), {}
                )
                record["Owner"] = sobjects_by_id["User"].get(record.get("OwnerId"), {})
                yield self.doc_mapper.map_contact(record)

    async def get_leads(self):
        if not await self._is_queryable("Lead"):
            self._logger.info(
                "Object Lead is not queryable, so they won't be ingested."
            )
            return

        query = await self._leads_query()
        async for records in self._yield_non_bulk_query_pages(query):
            for record in records:
                sobjects_by_id = await self.sobjects_cache_by_type()
                record["Owner"] = sobjects_by_id["User"].get(record.get("OwnerId"), {})
                record["ConvertedAccount"] = sobjects_by_id["Account"].get(
                    record.get("ConvertedAccountId"), {}
                )
                record["ConvertedContact"] = sobjects_by_id["Contact"].get(
                    record.get("ConvertedContactId"), {}
                )
                record["ConvertedOpportunity"] = sobjects_by_id["Opportunity"].get(
                    record.get("ConvertedOpportunityId"), {}
                )
                yield self.doc_mapper.map_lead(record)

    async def queryable_sobjects(self):
        """Cached async property"""
        if self._queryable_sobjects is not None:
            return self._queryable_sobjects

        response = await self._get_json(f"{self.base_url}{DESCRIBE_ENDPOINT}")
        self._queryable_sobjects = []

        for sobject in response.get("sobjects", []):
            if sobject["queryable"] is True and sobject["name"] in RELEVANT_SOBJECTS:
                self._queryable_sobjects.append(sobject["name"].lower())

        return self._queryable_sobjects

    async def queryable_sobject_fields(self):
        """Cached async property"""
        if self._queryable_sobject_fields is not None:
            return self._queryable_sobject_fields

        self._queryable_sobject_fields = {}

        for sobject in RELEVANT_SOBJECTS:
            endpoint = DESCRIBE_SOBJECT_ENDPOINT.replace("<sobject>", sobject)
            response = await self._get_json(f"{self.base_url}{endpoint}")

            queryable_fields = [
                f["name"].lower()
                for f in response.get("fields", [])
                if f["name"] in RELEVANT_SOBJECT_FIELDS
            ]
            self._queryable_sobject_fields[sobject] = queryable_fields

        return self._queryable_sobject_fields

    async def sobjects_cache_by_type(self):
        """Cached async property

        Many sobjects require extra data that is taxing on the rate limiter
        to repeatedly fetch each request.
        Instead we cache them on the first request for re-use later.
        """
        if self._sobjects_cache_by_type is not None:
            return self._sobjects_cache_by_type

        self._sobjects_cache_by_type = {}
        self._sobjects_cache_by_type["Account"] = await self._prepare_sobject_cache(
            "Account"
        )
        self._sobjects_cache_by_type["Contact"] = await self._prepare_sobject_cache(
            "Contact"
        )
        self._sobjects_cache_by_type["Opportunity"] = await self._prepare_sobject_cache(
            "Opportunity"
        )
        self._sobjects_cache_by_type["User"] = await self._prepare_sobject_cache("User")
        return self._sobjects_cache_by_type

    async def _prepare_sobject_cache(self, sobject):
        if not await self._is_queryable(sobject):
            self._logger.info(f"{sobject} is not queryable, so they won't be cached.")
            return {}

        queryable_fields = ["Name"]
        if sobject in ["User", "Contact", "Lead"]:
            queryable_fields.append("Email")

        sobjects = {}
        query_builder = SalesforceSoqlBuilder(sobject)
        query_builder.with_id()
        query_builder.with_fields(queryable_fields)
        query = query_builder.build()

        async for records in self._yield_non_bulk_query_pages(query):
            for record in records:
                sobjects[record["Id"]] = record

        return sobjects

    async def _is_queryable(self, sobject):
        """User settings can cause sobjects to be non-queryable
        Querying these causes errors, so we try to filter those out in advance
        """
        return sobject.lower() in await self.queryable_sobjects()

    async def _select_queryable_fields(self, sobject, fields):
        """User settings can cause fields to be non-queryable
        Querying these causes errors, so we try to filter those out in advance
        """
        sobject_fields = await self.queryable_sobject_fields()
        queryable_fields = sobject_fields.get(sobject, [])
        return [f for f in fields if f.lower() in queryable_fields]

    async def _yield_non_bulk_query_pages(self, soql_query):
        """loops through query response pages and yields lists of records"""
        url = f"{self.base_url}{QUERY_ENDPOINT}"
        params = {"q": soql_query}

        while True:
            response = await self._get_json(
                url,
                params=params,
            )
            yield response.get("records")
            if response.get("done", True) is True:
                break

            url = response.get("nextRecordsUrl")
            params = None

    def _auth_headers(self):
        return {"authorization": f"Bearer {self.api_token.token()}"}

    @retryable(
        retries=RETRIES,
        interval=RETRY_INTERVAL,
        skipped_exceptions=[RateLimitedException, InvalidQueryException],
    )
    async def _get_json(self, url, params=None):
        response_body = None

        while True:
            try:
                response = await self._get(url, params=params)
                response_body = await response.json()
                # We get the response body before raising for status as it contains vital error information
                response.raise_for_status()
                return response_body
            except ClientResponseError as e:
                await self._handle_client_response_error(response_body, e)
            except Exception as e:
                raise e

    async def _get(self, url, params=None):
        self._logger.debug(f"Sending request. Url: {url}, params: {params}")
        return await self.session.get(
            url,
            headers=self._auth_headers(),
            params=params,
        )

    async def _handle_client_response_error(self, response_body, e):
        exception_details = f"status: {e.status}, message: {e.message}"

        if e.status == 401:
            self._logger.warning(
                f"Token expired, attemping to fetch new token. Status: {e.status}, message: {e.message}"
            )
            # The user can alter the lifetime of issued tokens, so we don't know when they expire
            # Therefore we fetch the token when we encounter an error rather than when it expires
            self.api_token.clear()
            await self.get_token()
            # raise to continue with retry strategy
            raise e
        elif 400 <= e.status < 500:
            errors = self._handle_response_body_error(response_body)
            # response format is an array for some reason so we check all of the error codes
            # errorCode and message are generally identical, except if the query is invalid
            error_codes = [x["errorCode"] for x in errors]

            if "REQUEST_LIMIT_EXCEEDED" in error_codes:
                raise RateLimitedException(
                    f"Salesforce is rate limiting this account. {exception_details}, details: {', '.join(error_codes)}"
                ) from e
            elif (
                any(
                    error in error_codes
                    for error in [
                        "INVALID_FIELD",
                        "INVALID_TERM",
                        "MALFORMED_QUERY",
                    ]
                )
                in error_codes
            ):
                raise InvalidQueryException(
                    f"The query was rejected by Salesforce. {exception_details}, details: {', '.join(error_codes)}, query: {', '.join([x['message'] for x in errors])}"
                ) from e
            else:
                raise RequestError(
                    f"The request to Salesforce failed. {exception_details}, details: {', '.join(error_codes)}"
                ) from e
        else:
            raise e

    def _handle_response_body_error(self, error_list):
        if error_list is None or len(error_list) < 1:
            return [{"errorCode": "unknown"}]

        return error_list

    async def _accounts_query(self):
        queryable_fields = await self._select_queryable_fields(
            "Account",
            [
                "Name",
                "Description",
                "BillingAddress",
                "Type",
                "Website",
                "Rating",
                "Department",
            ],
        )
        query_builder = SalesforceSoqlBuilder("Account")
        query_builder.with_id()
        query_builder.with_default_metafields()
        query_builder.with_fields(queryable_fields)
        # TODO add uncommon_object_remote_fields
        query_builder.with_fields(["Owner.Id", "Owner.Name", "Owner.Email"])
        query_builder.with_fields(["Parent.Id", "Parent.Name"])

        if await self._is_queryable("Opportunity"):
            queryable_join_fields = await self._select_queryable_fields(
                "Opportunity",
                [
                    "Name",
                    "StageName",
                ],
            )
            join_builder = SalesforceSoqlBuilder("Opportunities")
            join_builder.with_id()
            join_builder.with_fields(queryable_join_fields)
            join_builder.with_order_by("CreatedDate DESC")
            join_builder.with_limit(1)
            query_builder.with_join(join_builder.build())

        return query_builder.build()

    async def _opportunities_query(self):
        queryable_fields = await self._select_queryable_fields(
            "Opportunity",
            [
                "Name",
                "Description",
                "StageName",
            ],
        )
        query_builder = SalesforceSoqlBuilder("Opportunity")
        query_builder.with_id()
        query_builder.with_default_metafields()
        query_builder.with_fields(queryable_fields)
        # TODO add uncommon_object_remote_fields
        query_builder.with_fields(["Owner.Id", "Owner.Name", "Owner.Email"])

        return query_builder.build()

    async def _contacts_query(self):
        queryable_fields = await self._select_queryable_fields(
            "Contact",
            [
                "Name",
                "Description",
                "Email",
                "Phone",
                "Title",
                "PhotoUrl",
                "LeadSource",
                "AccountId",
                "OwnerId",
            ],
        )
        query_builder = SalesforceSoqlBuilder("Contact")
        query_builder.with_id()
        query_builder.with_default_metafields()
        query_builder.with_fields(queryable_fields)
        # TODO add uncommon_object_remote_fields
        return query_builder.build()

    async def _leads_query(self):
        queryable_fields = await self._select_queryable_fields(
            "Lead",
            [
                "Company",
                "ConvertedAccountId",
                "ConvertedContactId",
                "ConvertedDate",
                "ConvertedOpportunityId",
                "Description",
                "Email",
                "LeadSource",
                "Name",
                "OwnerId",
                "Phone",
                "PhotoUrl",
                "Rating",
                "Status",
                "Title",
            ],
        )
        query_builder = SalesforceSoqlBuilder("Lead")
        query_builder.with_id()
        query_builder.with_default_metafields()
        query_builder.with_fields(queryable_fields)
        # TODO add uncommon_object_remote_fields
        return query_builder.build()


class SalesforceAPIToken:
    def __init__(self, session, base_url, client_id, client_secret):
        self.lock = Lock()
        self._token = None
        self.session = session
        self.url = f"{base_url}{TOKEN_ENDPOINT}"
        self.token_payload = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }

    def token(self):
        return self._token

    async def generate(self):
        with self._non_blocking_lock():
            response_body = {}
            try:
                response = await self.session.post(self.url, data=self.token_payload)
                response_body = await response.json()
                response.raise_for_status()
                self._token = response_body["access_token"]
            except ClientResponseError as e:
                if 400 <= e.status < 500:
                    # 400s have detailed error messages in body
                    error_message = response_body.get(
                        "error", "No error dscription found."
                    )
                    if error_message == "invalid_client":
                        raise InvalidCredentialsException(
                            f"The `client_id` and `client_secret` provided could not be used to generate a token. Status: {e.status}, message: {e.message}, details: {error_message}"
                        ) from e
                    else:
                        raise TokenFetchException(
                            f"Could not fetch token from Salesforce: Status: {e.status}, message: {e.message}, details: {error_message}"
                        ) from e
                else:
                    raise TokenFetchException(
                        f"Unexpected error while fetching Salesforce token. Status: {e.status}, message: {e.message}"
                    ) from e

    def clear(self):
        self._token = None

    @contextmanager
    def _non_blocking_lock(self):
        if not self.lock.acquire(blocking=False):
            raise LockedException("Token generation is already running.")
        try:
            yield self.lock
        finally:
            self.lock.release()


class SalesforceSoqlBuilder:
    def __init__(self, table):
        self.table_name = table
        self.fields = []
        self.where = ""
        self.order_by = ""
        self.limit = ""

    def with_id(self):
        self.fields.append("Id")

    def with_default_metafields(self):
        self.fields.extend(["CreatedDate", "LastModifiedDate"])

    def with_fields(self, fields):
        self.fields.extend(fields)

    def with_where(self, where_string):
        self.where = f"WHERE {where_string}"

    def with_order_by(self, order_by_string):
        self.order_by = f"ORDER BY {order_by_string}"

    def with_limit(self, limit):
        self.limit = f"LIMIT {limit}"

    def with_join(self, join):
        self.fields.append(f"(\n{join})\n")

    def build(self):
        select_columns = ",\n".join(set(self.fields))

        query_lines = []
        query_lines.append(f"SELECT {select_columns}")
        query_lines.append(f"FROM {self.table_name}")
        query_lines.append(self.where)
        query_lines.append(self.order_by)
        query_lines.append(self.limit)

        return "\n".join([line for line in query_lines if line != ""])


class SalesforceDocMapper:
    def __init__(self, base_url):
        self.base_url = base_url

    def map_account(self, account):
        owner = account.get("Owner", {})

        opportunities = account.get("Opportunities")
        opportunity_records = opportunities.get("records", []) if opportunities else []
        opportunity = opportunity_records[0] if len(opportunity_records) > 0 else {}
        opportunity_url = (
            f"{self.base_url}/{opportunity.get('Id')}" if opportunity else ""
        )
        opportunity_status = opportunity.get("StageName", "")

        return {
            "_id": account.get("Id"),
            "account_type": account.get("Type"),
            "address": self._format_address(account.get("BillingAddress")),
            "body": account.get("Description"),
            "content_source_id": account.get("Id"),
            "created_at": account.get("CreatedDate"),
            "last_updated": account.get("LastModifiedDate"),
            "open_activities": "",  # TODO
            "open_activities_urls": "",  # TODO
            "opportunity_name": opportunity.get("Name"),
            "opportunity_status": opportunity_status,
            "opportunity_url": opportunity_url,
            "owner": owner.get("Name"),
            "owner_email": owner.get("Email"),
            "rating": account.get("Rating"),
            "source": "salesforce",
            "tags": [account.get("Type")],
            "title": account.get("Name"),
            "type": "account",
            "url": f"{self.base_url}/{account.get('Id')}",
            "website_url": account.get("Website"),
        }

    def map_opportunity(self, opportunity):
        owner = opportunity.get("Owner", {})

        return {
            "_id": opportunity.get("Id"),
            "body": opportunity.get("Description"),
            "content_source_id": opportunity.get("Id"),
            "created_at": opportunity.get("CreatedDate"),
            "last_updated": opportunity.get("LastModifiedDate"),
            "next_step": opportunity.get("NextStep"),
            "owner": owner.get("Name"),
            "owner_email": owner.get("Email"),
            "source": "salesforce",
            "status": opportunity.get("StageName", ""),
            "title": opportunity.get("Name"),
            "type": "opportunity",
            "url": f"{self.base_url}/{opportunity.get('Id')}",
        }

    def map_contact(self, contact):
        account = contact.get("Account", {})
        account_id = contact.get("AccountId", "")
        account_url = f"{self.base_url}/{account_id}" if account_id else ""

        owner = contact.get("Owner", {})
        owner_id = contact.get("OwnerId", "")
        owner_url = f"{self.base_url}/{owner_id}" if owner_id else ""

        photo_url = contact.get("PhotoUrl")
        thumbnail = f"{self.base_url}{photo_url}" if photo_url else ""

        return {
            "_id": contact.get("Id"),
            "account": account.get("Name"),
            "account_url": account_url,
            "body": contact.get("Description"),
            "email": contact.get("Email"),
            "job_title": contact.get("Title"),
            "last_updated": contact.get("LastModifiedDate"),
            "lead_source": contact.get("LeadSource"),
            "owner": owner.get("Name"),
            "owner_url": owner_url,
            "phone": contact.get("Phone"),
            "source": "salesforce",
            "thumbnail": thumbnail,
            "title": contact.get("Name"),
            "type": "contact",
            "url": f"{self.base_url}/{contact.get('Id')}",
        }

    def map_lead(self, lead):
        owner = lead.get("Owner", {})
        owner_id = lead.get("OwnerId", "")
        owner_url = f"{self.base_url}/{owner_id}" if owner_id else ""

        converted_account = lead.get("ConvertedAccount", {})
        converted_account_id = converted_account.get("Id")
        converted_account_url = (
            f"{self.base_url}/{converted_account_id}" if converted_account_id else None
        )

        converted_contact = lead.get("ConvertedContact", {})
        converted_contact_id = converted_account.get("Id")
        converted_contact_url = (
            f"{self.base_url}/{converted_contact_id}" if converted_contact_id else None
        )

        converted_opportunity = lead.get("ConvertedOpportunity", {})
        converted_opportunity_id = converted_opportunity.get("Id")
        converted_opportunity_url = (
            f"{self.base_url}/{converted_opportunity_id}"
            if converted_opportunity_id
            else None
        )

        photo_url = lead.get("PhotoUrl")
        thumbnail = f"{self.base_url}{photo_url}" if photo_url else None

        return {
            "_id": lead.get("Id"),
            "body": lead.get("Description"),
            "company": lead.get("Company"),
            "converted_account": converted_account.get("Name"),
            "converted_account_url": converted_account_url,
            "converted_at": lead.get("ConvertedDate"),  # TODO convert
            "converted_contact": converted_contact.get("Name"),
            "converted_contact_url": converted_contact_url,
            "converted_opportunity": converted_opportunity.get("Name"),
            "converted_opportunity_url": converted_opportunity_url,
            "email": lead.get("Email"),
            "job_title": lead.get("Title"),
            "last_updated": lead.get("LastModifiedDate"),
            "lead_source": lead.get("LeadSource"),
            "owner": owner.get("Name"),
            "owner_url": owner_url,
            "phone": lead.get("Phone"),
            "rating": lead.get("Rating"),
            "source": "salesforce",
            "status": lead.get("Status"),
            "title": lead.get("Name"),
            "thumbnail": thumbnail,
            "type": "lead",
            "url": f"{self.base_url}/{lead.get('Id')}",
        }

    def _format_address(self, address):
        if not address:
            return ""

        address_fields = [
            address.get("street"),
            address.get("city"),
            address.get("state"),
            str(address.get("postalCode", "")),
            address.get("country"),
        ]
        return ", ".join([a for a in address_fields if a])


class SalesforceDataSource(BaseDataSource):
    """Salesforce"""

    name = "Salesforce"
    service_type = "salesforce"

    def __init__(self, configuration):
        super().__init__(configuration=configuration)
        self.salesforce_client = SalesforceClient(configuration=configuration)

    def _set_internal_logger(self):
        self.salesforce_client.set_logger(self._logger)

    @classmethod
    def get_default_configuration(cls):
        return {
            "client_id": {
                "label": "Client ID",
                "type": "str",
                "value": "",
            },
            "client_secret": {
                "label": "Client Secret",
                "type": "str",
                "value": "",
            },
            "domain": {
                "label": "Domain",
                "type": "str",
                "value": "",
            },
        }

    async def validate_config(self):
        self.configuration.check_valid()

    async def close(self):
        await self.salesforce_client.close()

    async def ping(self):
        try:
            await self.salesforce_client.ping()
            self._logger.debug("Successfully connected to Salesforce.")
        except Exception as e:
            self._logger.exception(f"Error while connecting to Salesforce: {e}")
            raise

    async def get_content(self, attachment, timestamp=None, doit=False):
        # TODO implement
        return

    async def get_docs(self, filtering=None):
        await self.salesforce_client.get_token()

        # TODO filtering
        async for doc in self.salesforce_client.get_docs():
            yield doc
