import logging
from dataclasses import dataclass, field

import pandas as pd
import polars as pl

from aligned.data_source.batch_data_source import BatchDataSource
from aligned.data_source.stream_data_source import StreamDataSource
from aligned.retrival_job import RequestResult
from aligned.schemas.codable import Codable
from aligned.schemas.derivied_feature import DerivedFeature
from aligned.schemas.feature import EventTimestamp, Feature, FeatureReferance
from aligned.schemas.literal_value import LiteralValue

logger = logging.getLogger(__name__)


@dataclass
class EventTrigger(Codable):
    condition: DerivedFeature
    event: StreamDataSource
    payload: set[Feature]

    async def check_pandas(self, df: pd.DataFrame, result: RequestResult) -> None:
        from aligned.data_source.stream_data_source import SinkableDataSource
        from aligned.local.job import LiteralRetrivalJob

        if not isinstance(self.event, SinkableDataSource):
            logger.info(f'Event: {self.event.topic_name} is not sinkable will return')
            return

        logger.info(f'Checking for event: {self.event.topic_name}')

        mask = await self.condition.transformation.transform_pandas(df)

        if mask.any():
            trigger_result = RequestResult(result.entities, self.payload, None)
            features = {entity.name for entity in result.entities}.union(
                {feature.name for feature in self.payload}
            )
            events = df[list(features)].loc[mask]
            logger.info(f'Sending {events.shape[0]} events: {self.event.topic_name}')
            await self.event.write_to_stream(LiteralRetrivalJob(events, trigger_result))

    async def check_polars(self, df: pl.LazyFrame, result: RequestResult) -> None:
        from aligned.data_source.stream_data_source import SinkableDataSource
        from aligned.local.job import LiteralRetrivalJob

        if not isinstance(self.event, SinkableDataSource):
            logger.info(f'Event: {self.event.topic_name} is not sinkable will return')
            return

        logger.info(f'Checking for event: {self.event.topic_name}')

        mask: pl.LazyFrame = await self.condition.transformation.transform_polars(df, self.condition.name)
        mask = mask.filter(pl.col(self.condition.name))

        triggers = mask.collect()

        if triggers.shape[0] > 0:
            trigger_result = RequestResult(result.entities, self.payload, None)
            features = {entity.name for entity in result.entities}.union(
                {feature.name for feature in self.payload}
            )
            events = mask.lazy().select(features)
            logger.info(f'Sending {triggers.shape[0]} events: {self.event.topic_name}')
            await self.event.write_to_stream(LiteralRetrivalJob(events, trigger_result))

    def __hash__(self) -> int:
        return self.event.topic_name.__hash__()


@dataclass
class Target(Codable):
    estimating: FeatureReferance
    feature: Feature
    event_trigger: EventTrigger | None = field(default=None)

    def __hash__(self) -> int:
        return self.feature.name.__hash__()


@dataclass
class TargetProbability(Codable):
    outcome: LiteralValue
    feature: Feature


@dataclass
class InferenceView(Codable):
    target: set[Target]
    entities: set[Feature]
    features: set[Feature]
    derived_features: set[DerivedFeature]
    event_timestamp: EventTimestamp | None = field(default=None)
    source: BatchDataSource | None = field(default=None)
    probabilities: set[TargetProbability] | None = field(default=None)


@dataclass
class Model(Codable):
    name: str
    features: set[FeatureReferance]
    inference_view: InferenceView

    def __hash__(self) -> int:
        return self.name.__hash__()
