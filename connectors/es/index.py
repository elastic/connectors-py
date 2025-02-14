#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
from functools import partial

from elasticsearch import ApiError

from connectors.es import ESClient
from connectors.logger import logger

DEFAULT_PAGE_SIZE = 100


class DocumentNotFoundError(Exception):
    pass


class TemporaryConnectorApiWrapper(ESClient):
    """Temporary class to wrap calls to Connectors API.

    When connectors API becomes part of official client
    this class will be removed.
    """

    def __init__(self, elastic_config):
        super().__init__(elastic_config)

    async def connector_get(self, connector_id, include_deleted):
        return await self.client.perform_request(
            "GET",
            f"/_connector/{connector_id}",
            headers={"accept": "application/json"},
            params={"include_deleted": include_deleted},
        )


class ESApi(ESClient):
    def __init__(self, elastic_config):
        super().__init__(elastic_config)
        self._api_wrapper = TemporaryConnectorApiWrapper(elastic_config)

    async def connector_check_in(self, connector_id):
        return await self._retrier.execute_with_retry(
            partial(self.client.connector.check_in, connector_id=connector_id)
        )

    async def connector_get(self, connector_id, include_deleted=False):
        return await self._retrier.execute_with_retry(
            partial(self._api_wrapper.connector_get, connector_id, include_deleted)
        )

    async def connector_put(
        self, connector_id, service_type, connector_name, index_name, is_native
    ):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.connector.put,
                connector_id=connector_id,
                service_type=service_type,
                name=connector_name,
                index_name=index_name,
                is_native=is_native,
            )
        )

    async def connector_update_scheduling(self, connector_id, scheduling):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.connector.update_scheduling,
                connector_id=connector_id,
                scheduling=scheduling,
            )
        )

    async def connector_update_configuration(self, connector_id, configuration, values):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.connector.update_configuration,
                connector_id=connector_id,
                configuration=configuration,
                values=values,
            )
        )

    async def connector_update_filtering_draft_validation(
        self, connector_id, validation_result
    ):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.connector.update_filtering_validation,
                connector_id=connector_id,
                validation=validation_result,
            )
        )

    async def connector_activate_filtering_draft(self, connector_id):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.connector.update_active_filtering, connector_id=connector_id
            )
        )

    async def connector_sync_job_claim(self, sync_job_id, worker_hostname, sync_cursor):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.connector.sync_job_claim,
                connector_sync_job_id=sync_job_id,
                worker_hostname=worker_hostname,
                sync_cursor=sync_cursor,
            )
        )

    async def connector_sync_job_create(self, connector_id, job_type, trigger_method):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.connector.sync_job_post,
                id=connector_id,
                job_type=job_type,
                trigger_method=trigger_method,
            )
        )

    async def connector_sync_job_update_stats(
        self, sync_job_id, ingestion_stats, metadata
    ):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.connector.sync_job_update_stats,
                connector_sync_job_id=sync_job_id,
                body=ingestion_stats,
                metadata=metadata if metadata else {},
            )
        )


class ESIndex(ESClient):
    """
    Encapsulates the work with Elasticsearch index.

    All classes that are extended by ESIndex should implement _create_object
    method to represent documents

    Args:
        index_name (str): index_name: Name of an Elasticsearch index
        elastic_config (dict): Elasticsearch configuration and credentials
    """

    def __init__(self, index_name, elastic_config):
        # initialize elasticsearch client
        super().__init__(elastic_config)
        self.api = ESApi(elastic_config)
        self.feature_use_connectors_api = elastic_config.get(
            "feature_use_connectors_api"
        )
        self.index_name = index_name
        self.elastic_config = elastic_config

    def _create_object(self, doc):
        """
        The method must be implemented in all successor classes

        Args:
            doc (dict): Represents an Elasticsearch document
        Raises:
            NotImplementedError: if not implemented in a successor class
        """
        raise NotImplementedError

    async def fetch_by_id(self, doc_id):
        resp_body = await self.fetch_response_by_id(doc_id)
        return self._create_object(resp_body)

    async def fetch_response_by_id(self, doc_id):
        try:
            if not self.serverless:
                await self._retrier.execute_with_retry(
                    partial(self.client.indices.refresh, index=self.index_name)
                )

            resp = await self._retrier.execute_with_retry(
                partial(self.client.get, index=self.index_name, id=doc_id)
            )
        except ApiError as e:
            logger.error(f"The server returned {e.status_code}")
            logger.error(e.body, exc_info=True)
            if e.status_code == 404:
                msg = f"Couldn't find document in {self.index_name} by id {doc_id}"
                raise DocumentNotFoundError(msg) from e
            raise

        return resp.body

    async def index(self, doc):
        return await self._retrier.execute_with_retry(
            partial(self.client.index, index=self.index_name, document=doc)
        )

    async def clean_index(self):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.delete_by_query,
                index=self.index_name,
                body={"query": {"match_all": {}}},
                ignore_unavailable=True,
                conflicts="proceed",
            )
        )

    async def update(self, doc_id, doc, if_seq_no=None, if_primary_term=None):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.update,
                index=self.index_name,
                id=doc_id,
                doc=doc,
                if_seq_no=if_seq_no,
                if_primary_term=if_primary_term,
            )
        )

    async def update_by_script(self, doc_id, script):
        return await self._retrier.execute_with_retry(
            partial(
                self.client.update,
                index=self.index_name,
                id=doc_id,
                script=script,
            )
        )

    async def get_all_docs(self, query=None, sort=None, page_size=DEFAULT_PAGE_SIZE):
        """
        Lookup for elasticsearch documents using {query}

        Args:
            query (dict): Represents an Elasticsearch query
            sort (list): A list of fields to sort the result
            page_size (int): Number of documents per query
        Returns:
            Iterator
        """
        if not self.serverless:
            await self._retrier.execute_with_retry(
                partial(self.client.indices.refresh, index=self.index_name)
            )

        if query is None:
            query = {"match_all": {}}

        count = 0
        offset = 0

        while True:
            try:
                resp = await self._retrier.execute_with_retry(
                    partial(
                        self.client.search,
                        index=self.index_name,
                        query=query,
                        sort=sort,
                        from_=offset,
                        size=page_size,
                        expand_wildcards="hidden",
                        seq_no_primary_term=True,
                    )
                )
            except ApiError as e:
                logger.error(
                    f"Elasticsearch returned {e.status_code} for 'GET {self.index_name}/_search' with body:"
                )
                logger.error(e.body, exc_info=True)
                raise

            hits = resp["hits"]["hits"]
            total = resp["hits"]["total"]["value"]
            count += len(hits)
            for hit in hits:
                yield self._create_object(hit)
            if count >= total:
                break
            offset += len(hits)
