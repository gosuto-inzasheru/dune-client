""""
Basic Dune Client Class responsible for refreshing Dune Queries
Framework built on Dune's API Documentation
https://duneanalytics.notion.site/API-Documentation-1b93d16e0fa941398e15047f643e003a
"""
from __future__ import annotations

import time
from io import BytesIO
from typing import Any, Optional, Union

import requests
from requests import Response, JSONDecodeError

from dune_client.base_client import BaseDuneClient
from dune_client.interface import DuneInterface
from dune_client.models import (
    ExecutionResponse,
    ExecutionResultCSV,
    DuneError,
    QueryFailed,
    ExecutionStatusResponse,
    ResultsResponse,
    ExecutionState,
)

from dune_client.query import Query


class DuneClient(DuneInterface, BaseDuneClient):
    """
    An interface for Dune API with a few convenience methods
    combining the use of endpoints (e.g. refresh)
    """

    def _handle_response(
        self,
        response: Response,
    ) -> Any:
        try:
            # Some responses can be decoded and converted to DuneErrors
            response_json = response.json()
            self.logger.debug(f"received response {response_json}")
            return response_json
        except JSONDecodeError as err:
            # Others can't. Only raise HTTP error for not decodable errors
            response.raise_for_status()
            raise ValueError("Unreachable since previous line raises") from err

    def _route_url(self, route: str) -> str:
        return f"{self.BASE_URL}{self.API_PATH}/{route}"

    def _get(self, route: str, params: Optional[Any] = None) -> Any:
        url = self._route_url(route)
        self.logger.debug(f"GET received input url={url}")
        response = requests.get(
            url,
            headers={"x-dune-api-key": self.token},
            timeout=self.DEFAULT_TIMEOUT,
            params=params,
        )
        return self._handle_response(response)

    def _post(self, route: str, params: Any) -> Any:
        url = self._route_url(route)
        self.logger.debug(f"POST received input url={url}, params={params}")
        response = requests.post(
            url=url,
            json=params,
            headers={"x-dune-api-key": self.token},
            timeout=self.DEFAULT_TIMEOUT,
        )
        return self._handle_response(response)

    def execute(
        self, query: Query, performance: Optional[str] = None
    ) -> ExecutionResponse:
        """Post's to Dune API for execute `query`"""
        self.logger.info(
            f"executing {query.query_id} on {performance or self.performance} cluster"
        )
        response_json = self._post(
            route=f"query/{query.query_id}/execute",
            params={
                "query_parameters": {
                    p.key: p.to_dict()["value"] for p in query.parameters()
                },
                "performance": performance or self.performance,
            },
        )
        try:
            return ExecutionResponse.from_dict(response_json)
        except KeyError as err:
            raise DuneError(response_json, "ExecutionResponse", err) from err

    def get_status(self, job_id: str) -> ExecutionStatusResponse:
        """GET status from Dune API for `job_id` (aka `execution_id`)"""
        response_json = self._get(
            route=f"execution/{job_id}/status",
        )
        try:
            return ExecutionStatusResponse.from_dict(response_json)
        except KeyError as err:
            raise DuneError(response_json, "ExecutionStatusResponse", err) from err

    def get_result(self, job_id: str) -> ResultsResponse:
        """GET results from Dune API for `job_id` (aka `execution_id`)"""
        response_json = self._get(route=f"execution/{job_id}/results")
        try:
            return ResultsResponse.from_dict(response_json)
        except KeyError as err:
            raise DuneError(response_json, "ResultsResponse", err) from err

    def get_result_csv(self, job_id: str) -> ExecutionResultCSV:
        """
        GET results in CSV format from Dune API for `job_id` (aka `execution_id`)

        this API only returns the raw data in CSV format, it is faster & lighterweight
        use this method for large results where you want lower CPU and memory overhead
        if you need metadata information use get_results() or get_status()
        """
        url = self._route_url(f"execution/{job_id}/results/csv")
        self.logger.debug(f"GET CSV received input url={url}")
        response = requests.get(
            url,
            headers={"x-dune-api-key": self.token},
            timeout=self.DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        return ExecutionResultCSV(data=BytesIO(response.content))

    def get_latest_result(self, query: Union[Query, str, int]) -> ResultsResponse:
        """
        GET the latest results for a query_id without having to execute the query again.

        :param query: :class:`Query` object OR query id as string | int

        https://dune.com/docs/api/api-reference/latest_results/
        """
        if isinstance(query, Query):
            params = {
                f"params.{p.key}": p.to_dict()["value"] for p in query.parameters()
            }
            query_id = query.query_id
        else:
            params = None
            query_id = int(query)

        response_json = self._get(
            route=f"query/{query_id}/results",
            params=params,
        )
        try:
            return ResultsResponse.from_dict(response_json)
        except KeyError as err:
            raise DuneError(response_json, "ResultsResponse", err) from err

    def cancel_execution(self, job_id: str) -> bool:
        """POST Execution Cancellation to Dune API for `job_id` (aka `execution_id`)"""
        response_json = self._post(route=f"execution/{job_id}/cancel", params=None)
        try:
            # No need to make a dataclass for this since it's just a boolean.
            success: bool = response_json["success"]
            return success
        except KeyError as err:
            raise DuneError(response_json, "CancellationResponse", err) from err

    def _refresh(
        self,
        query: Query,
        ping_frequency: int = 5,
        performance: Optional[str] = None,
    ) -> str:
        job_id = self.execute(query=query, performance=performance).execution_id
        status = self.get_status(job_id)
        while status.state not in ExecutionState.terminal_states():
            self.logger.info(
                f"waiting for query execution {job_id} to complete: {status}"
            )
            time.sleep(ping_frequency)
            status = self.get_status(job_id)
        if status.state == ExecutionState.FAILED:
            self.logger.error(status)
            raise QueryFailed(f"{status}. Perhaps your query took too long to run!")

        return job_id

    def refresh(
        self,
        query: Query,
        ping_frequency: int = 5,
        performance: Optional[str] = None,
    ) -> ResultsResponse:
        """
        Executes a Dune `query`, waits until execution completes,
        fetches and returns the results.
        Sleeps `ping_frequency` seconds between each status request.
        """
        job_id = self._refresh(
            query,
            ping_frequency=ping_frequency,
            performance=performance,
        )
        return self.get_result(job_id)

    def refresh_csv(
        self,
        query: Query,
        ping_frequency: int = 5,
        performance: Optional[str] = None,
    ) -> ExecutionResultCSV:
        """
        Executes a Dune query, waits till execution completes,
        fetches and the results in CSV format
        (use it load the data directly in pandas.from_csv() or similar frameworks)
        """
        job_id = self._refresh(
            query,
            ping_frequency=ping_frequency,
            performance=performance,
        )
        return self.get_result_csv(job_id)

    def refresh_into_dataframe(
        self, query: Query, performance: Optional[str] = None
    ) -> Any:
        """
        Execute a Dune Query, waits till execution completes,
        fetched and returns the result as a Pandas DataFrame

        This is a convenience method that uses refresh_csv underneath
        """
        try:
            import pandas  # type: ignore # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            raise ImportError(
                "dependency failure, pandas is required but missing"
            ) from exc
        data = self.refresh_csv(query, performance=performance).data
        return pandas.read_csv(data)
