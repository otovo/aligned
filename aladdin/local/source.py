import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pandas as pd

from aladdin.codable import Codable
from aladdin.data_source.batch_data_source import BatchDataSource, ColumnFeatureMappable
from aladdin.enricher import CsvFileEnricher, Enricher, LoadedStatEnricher, StatisticEricher, TimespanSelector
from aladdin.feature_store import FeatureStore
from aladdin.repo_definition import RepoDefinition
from aladdin.s3.storage import FileStorage, HttpStorage
from aladdin.storage import Storage

logger = logging.getLogger(__name__)


class DataFileReference:
    """
    A reference to a data file.

    It can therefore be loaded in and writen to.
    Either as a pandas data frame, or a dask data frame.
    """

    async def read_pandas(self) -> pd.DataFrame:
        raise NotImplementedError()

    async def read_dask(self) -> pd.DataFrame:
        raise NotImplementedError()

    async def write_pandas(self) -> None:
        raise NotImplementedError()

    async def write_dask(self) -> None:
        raise NotImplementedError()


class StorageFileReference:
    """
    A reference to a file that can be loaded as bytes.

    The bytes can contain anything, potentially a FeatureStore definition
    """

    async def read(self) -> bytes:
        raise NotImplementedError()

    async def write(self, content: bytes) -> None:
        raise NotImplementedError()

    async def feature_store(self) -> FeatureStore:
        file = await self.read()
        return FeatureStore.from_definition(RepoDefinition.from_json(file))


@dataclass
class CsvConfig(Codable):
    """
    A config for how a CSV file should be loaded
    """

    seperator: str = field(default=',')
    compression: Literal['infer', 'gzip', 'bz2', 'zip', 'xz', 'zstd'] = field(default='infer')


@dataclass
class CsvFileSource(BatchDataSource, ColumnFeatureMappable, StatisticEricher):
    """
    A source pointing to a CSV file
    """

    path: str
    mapping_keys: dict[str, str] = field(default_factory=dict)
    csv_config: CsvConfig = field(default_factory=CsvConfig())

    type_name: str = 'csv'

    def job_group_key(self) -> str:
        return f'{self.type_name}/{self.path}'

    def __hash__(self) -> int:
        return hash(self.job_group_key())

    async def read_pandas(self) -> pd.DataFrame:
        return pd.read_csv(self.path, sep=self.csv_config.seperator, compression=self.csv_config.compression)

    def std(
        self, columns: set[str], time: TimespanSelector | None = None, limit: int | None = None
    ) -> Enricher:
        return LoadedStatEnricher(
            stat='std',
            columns=list(columns),
            enricher=self.enricher().selector(time, limit),
            mapping_keys=self.mapping_keys,
        )

    def mean(
        self, columns: set[str], time: TimespanSelector | None = None, limit: int | None = None
    ) -> Enricher:
        return LoadedStatEnricher(
            stat='mean',
            columns=list(columns),
            enricher=self.enricher().selector(time, limit),
            mapping_keys=self.mapping_keys,
        )

    def enricher(self) -> CsvFileEnricher:
        return CsvFileEnricher(file=Path(self.path))


@dataclass
class StorageFileSource(StorageFileReference):

    path: str

    @property
    def storage(self) -> Storage:
        if self.path.startswith('http'):
            return HttpStorage()
        else:
            return FileStorage()

    def __hash__(self) -> int:
        return hash(self.path)

    async def read(self) -> bytes:
        return await self.storage.read(self.path)

    async def write(self, content: bytes) -> None:
        return await self.storage.write(self.path, content)


class FileSource:
    """
    A factory class, creating references to files.

    This therefore abstracts away the concrete classes the users wants.
    Therefore making them easier to discover.
    """

    @staticmethod
    def from_path(path: str) -> 'StorageFileSource':
        return StorageFileSource(path=path)

    @staticmethod
    def csv_at(
        path: str, mapping_keys: dict[str, str] | None = None, csv_config: CsvConfig | None = None
    ) -> 'CsvFileSource':
        return CsvFileSource(path, mapping_keys=mapping_keys or {}, csv_config=csv_config or CsvConfig())


@dataclass
class LiteralReference(DataFileReference):
    """
    A class containing a in mem pandas frame.

    This makes it easier standardise the interface when writing data.
    """

    file: pd.DataFrame

    def job_group_key(self) -> str:
        return str(uuid4())

    async def read_pandas(self) -> pd.DataFrame:
        return self.file
