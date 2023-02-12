import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd
import polars as pl

from aligned.psql.data_source import PostgreSQLConfig, PostgreSQLDataSource
from aligned.request.retrival_request import RequestResult, RetrivalRequest
from aligned.retrival_job import DateRangeJob, FactualRetrivalJob, FullExtractJob, RetrivalJob
from aligned.schemas.feature import FeatureLocation, FeatureType

logger = logging.getLogger(__name__)


@dataclass
class SQLQuery:
    sql: str


@dataclass
class PostgreSqlJob(RetrivalJob):

    config: PostgreSQLConfig
    query: str
    retrival_requests: list[RetrivalRequest] = field(default_factory=list)

    def request_result(self) -> RequestResult:
        return RequestResult.from_request_list(self.retrival_requests)

    def retrival_requests(self) -> list[RetrivalRequest]:
        return self.retrival_requests

    async def to_pandas(self) -> pd.DataFrame:
        df = await self.to_polars()
        return df.collect().to_pandas()

    async def to_polars(self) -> pl.LazyFrame:
        return pl.read_sql(self.query, self.config.url).lazy()


@dataclass
class FullExtractPsqlJob(FullExtractJob):

    source: PostgreSQLDataSource
    request: RetrivalRequest
    limit: int | None = None

    @property
    def request_result(self) -> RequestResult:
        return RequestResult.from_request(self.request)

    @property
    def retrival_requests(self) -> list[RetrivalRequest]:
        return [self.request]

    @property
    def config(self) -> PostgreSQLConfig:
        return self.source.config

    async def to_pandas(self) -> pd.DataFrame:
        return await self.psql_job().to_pandas()

    async def to_polars(self) -> pl.LazyFrame:
        return await self.psql_job().to_polars()

    def psql_job(self) -> PostgreSqlJob:
        return PostgreSqlJob(self.config, self.build_request())

    def build_request(self) -> str:

        all_features = [
            feature.name for feature in list(self.request.all_required_features.union(self.request.entities))
        ]
        sql_columns = self.source.feature_identifier_for(all_features)
        columns = [
            f'"{sql_col}" AS {alias}' if sql_col != alias else sql_col
            for sql_col, alias in zip(sql_columns, all_features)
        ]
        column_select = ', '.join(columns)
        schema = f'{self.config.schema}.' if self.config.schema else ''

        limit_query = ''
        if self.limit:
            limit_query = f'LIMIT {int(self.limit)}'

        f'SELECT {column_select} FROM {schema}"{self.source.table}" {limit_query}',


@dataclass
class DateRangePsqlJob(DateRangeJob):

    source: PostgreSQLDataSource
    start_date: datetime
    end_date: datetime
    request: RetrivalRequest

    @property
    def request_result(self) -> RequestResult:
        return RequestResult.from_request(self.request)

    @property
    def retrival_requests(self) -> list[RetrivalRequest]:
        return [self.request]

    @property
    def config(self) -> PostgreSQLConfig:
        return self.source.config

    async def to_pandas(self) -> pd.DataFrame:
        return await self.psql_job().to_pandas()

    async def to_polars(self) -> pl.LazyFrame:
        return await self.psql_job().to_polars()

    def psql_job(self) -> PostgreSqlJob:
        return PostgreSqlJob(self.config, self.build_request())

    def build_request(self) -> str:

        if not self.request.event_timestamp:
            raise ValueError('Event timestamp is needed in order to run a data range job')

        event_timestamp_column = self.source.feature_identifier_for([self.request.event_timestamp.name])[0]
        all_features = [
            feature.name for feature in list(self.request.all_required_features.union(self.request.entities))
        ]
        sql_columns = self.source.feature_identifier_for(all_features)
        columns = [
            f'"{sql_col}" AS {alias}' if sql_col != alias else sql_col
            for sql_col, alias in zip(sql_columns, all_features)
        ]
        column_select = ', '.join(columns)
        schema = f'{self.config.schema}.' if self.config.schema else ''
        start_date = self.start_date.strftime('%Y-%m-%d %H:%M:%S')
        end_date = self.end_date.strftime('%Y-%m-%d %H:%M:%S')

        return (
            f'SELECT {column_select} FROM {schema}"{self.source.table}" WHERE'
            f' {event_timestamp_column} BETWEEN \'{start_date}\' AND \'{end_date}\''
        )


@dataclass
class FactPsqlJob(FactualRetrivalJob):
    """Fetches features for defined facts within a postgres DB

    It is supported to fetch from different tables, in one request
    This is hy the `source` property is a dict with sources

    NB: It is expected that the data sources are for the same psql instance
    """

    sources: dict[FeatureLocation, PostgreSQLDataSource]
    requests: list[RetrivalRequest]
    facts: RetrivalJob

    @property
    def request_result(self) -> RequestResult:
        return RequestResult.from_request_list(self.requests)

    @property
    def retrival_requests(self) -> list[RetrivalRequest]:
        return self.requests

    @property
    def config(self) -> PostgreSQLConfig:
        return list(self.sources.values())[0].config

    async def to_pandas(self) -> pd.DataFrame:
        job = await self.psql_job()
        return await job.to_pandas()

    async def to_polars(self) -> pl.LazyFrame:
        job = await self.psql_job()
        return await job.to_polars()

    async def psql_job(self) -> PostgreSqlJob:
        if isinstance(self.facts, PostgreSqlJob):
            return PostgreSqlJob(self.config, self.build_sql_entity_query(self.facts))
        return PostgreSqlJob(self.config, await self.build_request())

    def dtype_to_sql_type(self, dtype: object) -> str:
        if isinstance(dtype, str):
            return dtype
        if dtype == FeatureType('').string:
            return 'text'
        if dtype == FeatureType('').uuid:
            return 'uuid'
        if dtype == FeatureType('').int32 or dtype == FeatureType('').int64:
            return 'integer'
        if dtype == FeatureType('').datetime:
            return 'TIMESTAMP WITH TIME ZONE'
        return 'uuid'

    async def build_request(self) -> str:
        import numpy as np
        from jinja2 import BaseLoader, Environment

        template = Environment(loader=BaseLoader()).from_string(self.__sql_template())
        template_context: dict[str, Any] = {}

        final_select_names: set[str] = set()
        entity_types: dict[str, FeatureType] = {}
        has_event_timestamp = False

        for request in self.requests:
            final_select_names = final_select_names.union(
                {f'{request.location.name}_cte.{feature}' for feature in request.all_required_feature_names}
            )
            final_select_names = final_select_names.union(
                {f'entities.{entity}' for entity in request.entity_names}
            )
            for entity in request.entities:
                entity_types[entity.name] = entity.dtype
            if request.event_timestamp:
                has_event_timestamp = True

        if has_event_timestamp:
            final_select_names.add('event_timestamp')
            entity_types['event_timestamp'] = FeatureType('').datetime

        # Need to replace nan as it will not be encoded
        fact_df = await self.facts.to_pandas()
        fact_df = pd.DataFrame(self.facts).replace(np.nan, None)

        number_of_values = max(len(values) for values in self.facts.values())
        # + 1 is needed as 0 is evaluated for null
        fact_df['row_id'] = list(range(1, number_of_values + 1))

        entity_type_list = [
            self.dtype_to_sql_type(entity_types.get(entity, FeatureType('').int32))
            for entity in fact_df.columns
        ]

        query_values = []
        all_entities = []
        for values in fact_df.values:
            row_placeholders = []
            for column_index, value in enumerate(values):
                row_placeholders.append(
                    {
                        'value': value,  # Could in theory lead to SQL injection (?)
                        'dtype': entity_type_list[column_index],
                    }
                )
                if fact_df.columns[column_index] not in all_entities:
                    all_entities.append(fact_df.columns[column_index])
            query_values.append(row_placeholders)

        feature_view_names: list[str] = [location.name for location in self.sources.keys()]
        # Add the joins to the fact

        tables = []
        for request in self.requests:
            source = self.sources[request.location]
            field_selects = request.all_required_feature_names.union(
                {f'entities.{entity}' for entity in request.entity_names}
            ).union({'entities.row_id'})
            field_identifiers = source.feature_identifier_for(field_selects)
            selects = {
                feature if feature == db_field_name else f'{db_field_name} AS {feature}'
                for feature, db_field_name in zip(field_selects, field_identifiers)
            }

            entities = list(request.entity_names)
            entity_db_name = source.feature_identifier_for(entities)
            sort_query = 'entities.row_id'

            event_timestamp_clause = ''
            if request.event_timestamp:
                event_timestamp_column = source.feature_identifier_for([request.event_timestamp.name])[0]
                event_timestamp_clause = f'AND entities.event_timestamp >= ta.{event_timestamp_column}'
                sort_query += f', {event_timestamp_column} DESC'

            join_conditions = [
                f'ta.{entity_db_name} = entities.{entity} {event_timestamp_clause}'
                for entity, entity_db_name in zip(entities, entity_db_name)
            ]
            tables.append(
                {
                    'name': source.table,
                    'joins': join_conditions,
                    'features': selects,
                    'sort_query': sort_query,
                    'fv': request.location.name,
                }
            )

        template_context['selects'] = list(final_select_names)
        template_context['tables'] = tables
        template_context['joins'] = [
            f'INNER JOIN {feature_view}_cte ON {feature_view}_cte.row_id = entities.row_id'
            for feature_view in feature_view_names
        ]
        template_context['values'] = query_values
        template_context['entities'] = list(all_entities)

        # should insert the values as a value variable
        # As this can lead to sql injection
        return template.render(template_context)

    def build_sql_entity_query(self, sql_facts: PostgreSqlJob) -> str:
        from jinja2 import BaseLoader, Environment

        template = Environment(loader=BaseLoader()).from_string(self.__sql_entities_template())
        template_context: dict[str, Any] = {}

        final_select_names: set[str] = set()
        entity_types: dict[str, FeatureType] = {}
        has_event_timestamp = False

        for request in self.requests:
            final_select_names = final_select_names.union(
                {f'{request.location.name}_cte.{feature}' for feature in request.all_required_feature_names}
            )
            final_select_names = final_select_names.union(
                {f'entities.{entity}' for entity in request.entity_names}
            )
            for entity in request.entities:
                entity_types[entity.name] = entity.dtype
            if request.event_timestamp:
                has_event_timestamp = True

        if has_event_timestamp:
            final_select_names.add('event_timestamp')
            entity_types['event_timestamp'] = FeatureType('').datetime

        # Need to replace nan as it will not be encoded

        feature_view_names: list[str] = [location.name for location in self.sources.keys()]
        # Add the joins to the fact

        tables = []
        all_entities = set()
        for request in self.requests:
            source = self.sources[request.location]
            field_selects = request.all_required_feature_names.union(
                {f'entities.{entity}' for entity in request.entity_names}
            ).union({'entities.row_id'})
            field_identifiers = source.feature_identifier_for(field_selects)
            selects = {
                feature if feature == db_field_name else f'{db_field_name} AS {feature}'
                for feature, db_field_name in zip(field_selects, field_identifiers)
            }

            entities = list(request.entity_names)
            all_entities.update(request.entity_names)
            entity_db_name = source.feature_identifier_for(entities)
            sort_query = 'entities.row_id'

            event_timestamp_clause = ''
            if request.event_timestamp:
                all_entities.add('event_timestamp')
                event_timestamp_column = source.feature_identifier_for([request.event_timestamp.name])[0]
                event_timestamp_clause = f'AND entities.event_timestamp >= ta.{event_timestamp_column}'
                sort_query += f', {event_timestamp_column} DESC'

            join_conditions = [
                f'ta.{entity_db_name} = entities.{entity} {event_timestamp_clause}'
                for entity, entity_db_name in zip(entities, entity_db_name)
            ]
            tables.append(
                {
                    'name': source.table,
                    'joins': join_conditions,
                    'features': selects,
                    'sort_query': sort_query,
                    'fv': request.location.name,
                }
            )

        all_entities_list = list(all_entities)
        all_entities_str = ', '.join(all_entities_list)
        all_entities_list.append('row_id')
        entity_query = (
            f'SELECT {all_entities_str}, ROW_NUMBER() OVER (ORDER BY '
            f'{list(request.entity_names)[0]}) AS row_id FROM ({sql_facts.query}) AS entities'
        )

        template_context['selects'] = list(final_select_names)
        template_context['tables'] = tables
        template_context['joins'] = [
            f'INNER JOIN {feature_view}_cte ON {feature_view}_cte.row_id = entities.row_id'
            for feature_view in feature_view_names
        ]
        template_context['entities_sql'] = entity_query
        template_context['entities'] = all_entities_list

        # should insert the values as a value variable
        # As this can lead to sql injection
        return template.render(template_context)

    def __sql_entities_template(self) -> str:
        return """
WITH entities (
    {{ entities | join(', ') }}
) AS (
    {{ entities_sql }}
),

{% for table in tables %}
    {{table.fv}}_cte AS (
        SELECT DISTINCT ON (entities.row_id) {{ table.features | join(', ') }}
        FROM entities
        LEFT JOIN {{table.name}} ta on {{ table.joins | join(' AND ') }}
        ORDER BY {{table.sort_query}}
    ){% if loop.last %}{% else %},{% endif %}
{% endfor %}

SELECT {{ selects | join(', ') }}
FROM entities
{{ joins | join('\n    ') }}

"""

    def __sql_template(self) -> str:
        return """
WITH entities (
    {{ entities | join(', ') }}
) AS (
VALUES {% for row in values %}
    ({% for value in row %}
        {% if value.value %}'{{value.value}}'::{{value.dtype}}{% else %}null::{{value.dtype}}{% endif %}
        {% if loop.last %}{% else %},{% endif %}{% endfor %}){% if loop.last %}{% else %},{% endif %}
{% endfor %}
),

{% for table in tables %}
    {{table.fv}}_cte AS (
        SELECT DISTINCT ON (entities.row_id) {{ table.features | join(', ') }}
        FROM entities
        LEFT JOIN {{table.name}} ta on {{ table.joins | join(' AND ') }}
        ORDER BY {{table.sort_query}}
    ){% if loop.last %}{% else %},{% endif %}
{% endfor %}

SELECT {{ selects | join(', ') }}
FROM entities
{{ joins | join('\n    ') }}

"""
