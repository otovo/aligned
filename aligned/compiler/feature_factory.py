from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable, TypeVar

import pandas as pd
import polars as pl

from aligned.compiler.constraint_factory import ConstraintFactory, LiteralFactory
from aligned.data_source.stream_data_source import StreamDataSource
from aligned.schemas.constraints import (
    Constraint,
    InDomain,
    LowerBound,
    LowerBoundInclusive,
    MaxLength,
    MinLength,
    UpperBound,
    UpperBoundInclusive,
)
from aligned.schemas.derivied_feature import DerivedFeature
from aligned.schemas.feature import EventTimestamp as EventTimestampFeature
from aligned.schemas.feature import Feature, FeatureLocation, FeatureReferance, FeatureType
from aligned.schemas.transformation import TextVectoriserModel, Transformation

if TYPE_CHECKING:
    from aligned.compiler.transformation_factory import FillNaStrategy


class TransformationFactory:
    """
    A class that can compute the transformation logic.

    For most classes will there be no need for a factory,
    However for more advanced transformations will this be needed.

    e.g:

    StandaredScalerFactory(
        time_window=timedelta(days=30)
    )

    The batch data source will be provided when the compile method is run.
    leading to fetching a small sample, and compute the metrics needed in order to generate a
    """

    def compile(self) -> Transformation:
        pass

    @property
    def using_features(self) -> list[FeatureFactory]:
        pass


class AggregationTransformationFactory:
    @property
    def time_window(self) -> timedelta | None:
        pass

    def with_group_by(self, entities: list[FeatureReferance]) -> TransformationFactory:
        pass


T = TypeVar('T')


@dataclass
class EventTrigger:
    condition: FeatureFactory
    event: StreamDataSource


@dataclass
class TargetProbability:
    of_value: Any
    target: Target
    _name: str | None = None

    def __hash__(self) -> int:
        return self._name.__hash__()

    def __set_name__(self, owner, name):
        self._name = name


class FeatureReferencable:
    def feature_referance(self) -> FeatureReferance:
        pass


@dataclass
class Target(FeatureReferencable):
    feature: FeatureFactory
    event_trigger: EventTrigger | None = field(default=None)
    ground_truth_event: StreamDataSource | None = field(default=None)
    _name: str | None = field(default=None)
    _location: FeatureLocation | None = field(default=None)

    def __set_name__(self, owner, name):
        self._name = name

    def feature_referance(self) -> FeatureReferance:
        if not self._name:
            raise ValueError('Missing name, can not create reference')
        if not self._location:
            raise ValueError('Missing location, can not create reference')
        return FeatureReferance(self._name, self._location, self.feature.dtype)

    def listen_to_ground_truth_event(self, stream: StreamDataSource) -> Target:
        return Target(
            feature=self.feature,
            event_trigger=self.event_trigger,
            ground_truth_event=stream,
        )

    def send_ground_truth_event(self, when: Bool, sink_to: StreamDataSource) -> Target:
        assert when.dtype == FeatureType('').bool, 'A trigger needs a boolean condition'

        return Target(self.feature, EventTrigger(when, sink_to))

    def probability_of(self, value: Any) -> TargetProbability:

        if not isinstance(value, self.feature.dtype.python_type):
            raise ValueError(
                (
                    'Probability of target is of incorrect data type. ',
                    f'Target is {self.feature.dtype}, but value is {type(value)}.',
                )
            )

        return TargetProbability(value, self)


class FeatureFactory(FeatureReferencable):
    """
    Represents the information needed to generate a feature definition

    This may contain lazely loaded information, such as then name.
    Which will be added when the feature view is compiled, as we can get the attribute name at runtime.

    The feature can still have no name, but this means that it is an unstored feature.
    It will threfore need a transformation field.

    The feature_dependencies is the features graph for the given feature.

    aka
                            x <- standard scaler <- age: Float
    x_and_y_is_equal <-
                            y: Float
    """

    _name: str | None = None
    _location: FeatureLocation | None = None
    _description: str | None = None

    transformation: TransformationFactory | None = None
    constraints: set[ConstraintFactory] | None = None

    def __set_name__(self, owner, name):
        self._name = name

    @property
    def dtype(self) -> FeatureType:
        raise NotImplementedError()

    @property
    def name(self) -> str:
        if not self._name:
            raise ValueError('Have not been given a name yet')
        return self._name

    @property
    def depending_on_names(self) -> list[str]:
        if not self.transformation:
            return []
        return [feat._name for feat in self.transformation.using_features if feat._name]

    def feature_referance(self) -> FeatureReferance:
        return FeatureReferance(self.name, self._location, self.dtype)

    def feature(self) -> Feature:
        return Feature(
            name=self.name,
            dtype=self.dtype,
            description=self._description,
            tags=None,
            constraints=self.constraints,
        )

    def as_target(self) -> Target:
        return Target(self)

    def compile(self) -> DerivedFeature:

        if not self.transformation:
            raise ValueError('Trying to create a derived feature with no transformation')

        return DerivedFeature(
            name=self.name,
            dtype=self.dtype,
            depending_on=[feat.feature_referance() for feat in self.transformation.using_features],
            transformation=self.transformation.compile(),
            depth=self.depth(),
            description=self._description,
            tags=None,
            constraints=None,
        )

    def depth(self) -> int:
        value = 0
        if not self.transformation:
            return value
        for feature in self.transformation.using_features:
            value = max(feature.depth(), value)
        return value + 1

    def description(self: T, description: str) -> T:
        self._description = description  # type: ignore [attr-defined]
        return self

    def feature_dependencies(self) -> list[FeatureFactory]:
        values = []

        if not self.transformation:
            return []

        def add_values(feature: FeatureFactory) -> None:
            values.append(feature)
            if not feature.transformation:
                return
            for sub_feature in feature.transformation.using_features:
                add_values(sub_feature)

        for sub_feature in self.transformation.using_features:
            add_values(sub_feature)

        return values

    def copy_type(self: T) -> T:
        raise NotImplementedError()

    def fill_na(self: T, value: FillNaStrategy | Any) -> T:

        from aligned.compiler.transformation_factory import (
            ConstantFillNaStrategy,
            FillMissingFactory,
            FillNaStrategy,
        )

        instance: FeatureFactory = self.copy_type()  # type: ignore [attr-defined]
        if isinstance(value, FillNaStrategy):
            instance.transformation = FillMissingFactory(self, value)
        else:
            instance.transformation = FillMissingFactory(self, ConstantFillNaStrategy(value))
        return instance  # type: ignore [return-value]

    def transformed_using_features_pandas(
        self: T, using_features: list[FeatureFactory], transformation: Callable[[pd.DataFrame, pd.Series]]
    ) -> T:
        from aligned.compiler.transformation_factory import PandasTransformationFactory

        dtype: FeatureFactory = self.copy_type()  # type: ignore [assignment]

        dtype.transformation = PandasTransformationFactory(dtype, transformation, using_features or [self])
        return dtype  # type: ignore [return-value]

    def transform_pandas(self, transformation: Callable[[pd.DataFrame], pd.Series], as_dtype: T) -> T:
        from aligned.compiler.transformation_factory import PandasTransformationFactory

        dtype: FeatureFactory = as_dtype  # type: ignore [assignment]

        dtype.transformation = PandasTransformationFactory(dtype, transformation, [self])
        return dtype  # type: ignore [return-value]

    def transformed_using_features_polars(
        self: T,
        using_features: list[FeatureFactory],
        transformation: Callable[[pl.LazyFrame, str], pl.LazyFrame],
    ) -> T:
        from aligned.compiler.transformation_factory import PolarsTransformationFactory

        dtype: FeatureFactory = self.copy_type()  # type: ignore [assignment]
        dtype.transformation = PolarsTransformationFactory(dtype, transformation, using_features or [self])
        return dtype  # type: ignore [return-value]

    def transform_polars(
        self,
        expression: pl.Expr,
        using_features: list[FeatureFactory] | None = None,
        as_dtype: T | None = None,
    ) -> T:
        from aligned.compiler.transformation_factory import PolarsTransformationFactory

        dtype: FeatureFactory = as_dtype or self.copy_type()  # type: ignore [assignment]
        dtype.transformation = PolarsTransformationFactory(dtype, expression, using_features or [self])
        return dtype  # type: ignore [return-value]

    def is_required(self: T) -> T:
        from aligned.schemas.constraints import Required

        self._add_constraint(Required())  # type: ignore[attr-defined]
        return self

    def _add_constraint(self, constraint: ConstraintFactory | Constraint) -> None:
        # The constraint should be a lazy evaluated constraint
        # Aka, a factory, as with the features.
        # Therefore making it possible to add distribution checks
        if not self.constraints:
            self.constraints = set()
        if isinstance(constraint, Constraint):
            self.constraints.add(constraint)
        else:
            self.constraints.add(LiteralFactory(constraint))

    def is_not_null(self) -> Bool:
        from aligned.compiler.transformation_factory import NotNullFactory

        instance = Bool()
        instance.transformation = NotNullFactory(self)
        return instance


class CouldBeEntityFeature:
    def as_entity(self) -> Entity:
        if isinstance(self, FeatureFactory):
            return Entity(self)

        raise ValueError(f'{self} is not a feature factory, and can therefore not be an entity')


class EquatableFeature(FeatureFactory):

    # Comparable operators
    def __eq__(self, right: FeatureFactory | Any) -> Bool:  # type: ignore[override]
        from aligned.compiler.transformation_factory import EqualsFactory

        instance = Bool()
        instance.transformation = EqualsFactory(self, right)
        return instance

    def equals(self, right: object) -> Bool:
        return self == right

    def __ne__(self, right: FeatureFactory | Any) -> Bool:  # type: ignore[override]
        from aligned.compiler.transformation_factory import NotEqualsFactory

        instance = Bool()
        instance.transformation = NotEqualsFactory(right, self)
        return instance

    def not_equals(self, right: object) -> Bool:
        return self != right

    def is_in(self, values: list[Any]) -> Bool:
        from aligned.compiler.transformation_factory import IsInFactory

        instance = Bool()
        instance.transformation = IsInFactory(self, values)
        return instance


class ComparableFeature(EquatableFeature):
    def __lt__(self, right: object) -> Bool:
        from aligned.compiler.transformation_factory import LowerThenFactory

        instance = Bool()
        instance.transformation = LowerThenFactory(right, self)
        return instance

    def __le__(self, right: float) -> Bool:
        from aligned.compiler.transformation_factory import LowerThenOrEqualFactory

        instance = Bool()
        instance.transformation = LowerThenOrEqualFactory(right, self)
        return instance

    def __gt__(self, right: object) -> Bool:
        from aligned.compiler.transformation_factory import GreaterThenFactory

        instance = Bool()
        instance.transformation = GreaterThenFactory(self, right)
        return instance

    def __ge__(self, right: object) -> Bool:
        from aligned.compiler.transformation_factory import GreaterThenOrEqualFactory

        instance = Bool()
        instance.transformation = GreaterThenOrEqualFactory(right, self)
        return instance

    def lower_bound(self: T, value: float, is_inclusive: bool | None = None) -> T:

        if is_inclusive:
            self._add_constraint(LowerBoundInclusive(value))  # type: ignore[attr-defined]
        else:
            self._add_constraint(LowerBound(value))  # type: ignore[attr-defined]
        return self

    def upper_bound(self: T, value: float, is_inclusive: bool | None = None) -> T:

        if is_inclusive:
            self._add_constraint(UpperBoundInclusive(value))  # type: ignore[attr-defined]
        else:
            self._add_constraint(UpperBound(value))  # type: ignore[attr-defined]
        return self


class ArithmeticFeature(ComparableFeature):
    def __sub__(self, other: FeatureFactory) -> Float:
        from aligned.compiler.transformation_factory import DifferanceBetweenFactory, TimeDifferanceFactory

        feature = Float()
        if self.dtype == FeatureType('').datetime:
            feature.transformation = TimeDifferanceFactory(self, other)
        else:
            feature.transformation = DifferanceBetweenFactory(self, other)
        return feature

    def __add__(self, other: FeatureFactory) -> Float:
        from aligned.compiler.transformation_factory import AdditionBetweenFactory

        feature = Float()
        feature.transformation = AdditionBetweenFactory(self, other)
        return feature

    def __truediv__(self, other: FeatureFactory) -> Float:
        from aligned.compiler.transformation_factory import RatioFactory

        feature = Float()
        feature.transformation = RatioFactory(self, other)
        return feature

    def __floordiv__(self, other: FeatureFactory) -> Float:
        from aligned.compiler.transformation_factory import RatioFactory

        feature = Float()
        feature.transformation = RatioFactory(self, other)
        return feature

    def __abs__(self) -> Float:
        from aligned.compiler.transformation_factory import AbsoluteFactory

        feature = Float()
        feature.transformation = AbsoluteFactory(self)
        return feature

    def __pow__(self, other: FeatureFactory | Any) -> Float:
        from aligned.compiler.transformation_factory import PowerFactory

        feature = Float()
        feature.transformation = PowerFactory(self, other)
        return feature

    def log1p(self) -> Float:
        from aligned.compiler.transformation_factory import LogTransformFactory

        feature = Float()
        feature.transformation = LogTransformFactory(self)
        return feature


class DecimalOperations(FeatureFactory):
    def __round__(self) -> Int64:
        from aligned.compiler.transformation_factory import RoundFactory

        feature = Int64()
        feature.transformation = RoundFactory(self)
        return feature

    def __ceil__(self) -> Int64:
        from aligned.compiler.transformation_factory import CeilFactory

        feature = Int64()
        feature.transformation = CeilFactory(self)
        return feature

    def __floor__(self) -> Int64:
        from aligned.compiler.transformation_factory import FloorFactory

        feature = Int64()
        feature.transformation = FloorFactory(self)
        return feature


class TruncatableFeature(FeatureFactory):
    def __trunc__(self: T) -> T:
        raise NotImplementedError()


class NumberConvertableFeature(FeatureFactory):
    def as_float(self) -> Float:
        from aligned.compiler.transformation_factory import ToNumericalFactory

        feature = Float()
        feature.transformation = ToNumericalFactory(self)
        return feature

    def __int__(self) -> Int64:
        raise NotImplementedError()

    def __float__(self) -> Float:
        raise NotImplementedError()


class InvertableFeature(FeatureFactory):
    def __invert__(self) -> Bool:
        from aligned.compiler.transformation_factory import InverseFactory

        feature = Bool()
        feature.transformation = InverseFactory(self)
        return feature


class LogicalOperatableFeature(InvertableFeature):
    def __and__(self, other: Bool) -> Bool:
        from aligned.compiler.transformation_factory import AndFactory

        feature = Bool()
        feature.transformation = AndFactory(self, other)
        return feature

    def logical_and(self, other: Bool) -> Bool:
        return self & other

    def __or__(self, other: Bool) -> Bool:
        from aligned.compiler.transformation_factory import OrFactory

        feature = Bool()
        feature.transformation = OrFactory(self, other)
        return feature

    def logical_or(self, other: Bool) -> Bool:
        return self | other


class CategoricalEncodableFeature(EquatableFeature):
    def one_hot_encode(self, labels: list[str]) -> list[Bool]:
        return [self == label for label in labels]

    def ordinal_categories(self, orders: list[str]) -> Int32:
        from aligned.compiler.transformation_factory import OrdinalFactory

        feature = Int32()
        feature.transformation = OrdinalFactory(orders, self)
        return feature

    def accepted_values(self: T, values: list[str]) -> T:
        self._add_constraint(InDomain(values))  # type: ignore[attr-defined]
        return self


class DateFeature(FeatureFactory):
    def date_component(self, component: str) -> Int32:
        from aligned.compiler.transformation_factory import DateComponentFactory

        feature = Int32()
        feature.transformation = DateComponentFactory(component, self)
        return feature


class Bool(EquatableFeature, LogicalOperatableFeature):
    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').bool

    def copy_type(self) -> Bool:
        return Bool()


class Float(ArithmeticFeature, DecimalOperations):
    def copy_type(self) -> Float:
        return Float()

    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').float

    def aggregate(self) -> ArithmeticAggregation:
        return ArithmeticAggregation(self)


class Int32(ArithmeticFeature, CouldBeEntityFeature):
    def copy_type(self) -> Int32:
        return Int32()

    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').int32

    def aggregate(self) -> ArithmeticAggregation:
        return ArithmeticAggregation(self)


class Int64(ArithmeticFeature, CouldBeEntityFeature):
    def copy_type(self) -> Int64:
        return Int64()

    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').int64

    def aggregate(self) -> ArithmeticAggregation:
        return ArithmeticAggregation(self)


class UUID(FeatureFactory, CouldBeEntityFeature):
    def copy_type(self) -> UUID:
        return UUID()

    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').uuid


class LengthValidatable(FeatureFactory):
    def min_length(self: T, length: int) -> T:
        self._add_constraint(MinLength(length))
        return self

    def max_length(self: T, length: int) -> T:
        self._add_constraint(MaxLength(length))
        return self


class String(CategoricalEncodableFeature, NumberConvertableFeature, CouldBeEntityFeature, LengthValidatable):
    def copy_type(self) -> String:
        return String()

    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').string

    def aggregate(self) -> StringAggregation:
        return StringAggregation(self)

    def split(self, pattern: str, max_splits: int | None = None) -> String:
        raise NotImplementedError()

    def replace(self, values: dict[str, str]) -> String:
        from aligned.compiler.transformation_factory import ReplaceFactory

        feature = String()
        feature.transformation = ReplaceFactory(values, self)
        return feature

    def contains(self, value: str) -> Bool:
        from aligned.compiler.transformation_factory import ContainsFactory

        feature = Bool()
        feature.transformation = ContainsFactory(value, self)
        return feature

    def sentence_vector(self, model: TextVectoriserModel) -> Embedding:
        from aligned.compiler.transformation_factory import WordVectoriserFactory

        feature = Embedding()
        feature.transformation = WordVectoriserFactory(self, model)
        return feature

    def append(self, feature: FeatureFactory | str) -> String:
        from aligned.compiler.transformation_factory import AppendStrings

        feature = String()
        feature.transformation = AppendStrings(self, feature)
        return feature


class Entity(FeatureFactory):

    _dtype: FeatureFactory

    @property
    def dtype(self) -> FeatureType:
        return self._dtype.dtype

    def __init__(self, dtype: FeatureFactory):
        self._dtype = dtype

    def aggregate(self) -> CategoricalAggregation:
        return CategoricalAggregation(self)


class Timestamp(DateFeature, ArithmeticFeature):
    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').datetime


class EventTimestamp(DateFeature, ArithmeticFeature):

    ttl: timedelta | None

    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').datetime

    def __init__(self, ttl: timedelta | None = None):
        self.ttl = ttl

    def event_timestamp(self) -> EventTimestampFeature:
        return EventTimestampFeature(
            name=self.name, ttl=self.ttl.total_seconds() if self.ttl else None, description=self._description
        )


class Embedding(FeatureFactory):

    sub_type: FeatureFactory

    def copy_type(self) -> Embedding:
        return Embedding()

    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').embedding


class ImageUrl(FeatureFactory):
    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').string

    def copy_type(self) -> ImageUrl:
        return ImageUrl()

    def load_image(self) -> Image:
        from aligned.compiler.transformation_factory import LoadImageFactory

        image = Image()
        image.transformation = LoadImageFactory(self)
        return image


class Image(FeatureFactory):
    @property
    def dtype(self) -> FeatureType:
        return FeatureType('').array

    def copy_type(self) -> Image:
        return Image()

    def to_grayscale(self) -> Image:
        from aligned.compiler.transformation_factory import GrayscaleImageFactory

        image = Image()
        image.transformation = GrayscaleImageFactory(self)
        return image


@dataclass
class Coordinate:

    x: ArithmeticFeature
    y: ArithmeticFeature

    def eucledian_distance(self, to: Coordinate) -> Float:
        sub = self.x - to.x
        sub.hidden = True
        return (sub**2 + (self.y - to.y) ** 2) ** 0.5


@dataclass
class StringAggregation:

    feature: String
    time_window: timedelta | None = None

    def over(self, time_window: timedelta) -> StringAggregation:
        self.time_window = time_window
        return self

    def concat(self, separator: str | None = None) -> String:
        from aligned.compiler.aggregation_factory import ConcatStringsAggrigationFactory

        feature = String()
        feature.transformation = ConcatStringsAggrigationFactory(
            self.feature, group_by=[], separator=separator, time_window=self.time_window
        )
        return feature


@dataclass
class CategoricalAggregation:

    feature: FeatureFactory
    time_window: timedelta | None = None

    def over(self, time_window: timedelta) -> CategoricalAggregation:
        self.time_window = time_window
        return self

    def count(self) -> Int64:
        from aligned.compiler.aggregation_factory import CountAggregationFactory

        feat = Float()
        feat.transformation = CountAggregationFactory(self.feature, group_by=[], time_window=self.time_window)
        return feat


@dataclass
class ArithmeticAggregation:

    feature: ArithmeticFeature
    time_window: timedelta | None = None

    def over(
        self,
        weeks: float | None = None,
        days: float | None = None,
        hours: float | None = None,
        minutes: float | None = None,
        seconds: float | None = None,
    ) -> ArithmeticAggregation:
        self.time_window = timedelta(
            weeks=weeks or 0, days=days or 0, hours=hours or 0, minutes=minutes or 0, seconds=seconds or 0
        )
        return self

    def sum(self) -> Float:
        from aligned.compiler.aggregation_factory import SumAggregationFactory

        feat = Float()
        feat.transformation = SumAggregationFactory(self.feature, group_by=[], time_window=self.time_window)
        return feat

    def mean(self) -> Float:
        from aligned.compiler.aggregation_factory import MeanAggregationFactory

        feat = Float()
        feat.transformation = MeanAggregationFactory(self.feature, group_by=[], time_window=self.time_window)
        return feat

    def min(self) -> Float:
        from aligned.compiler.aggregation_factory import MinAggregationFactory

        feat = Float()
        feat.transformation = MinAggregationFactory(self.feature, group_by=[], time_window=self.time_window)
        return feat

    def max(self) -> Float:
        from aligned.compiler.aggregation_factory import MaxAggregationFactory

        feat = Float()
        feat.transformation = MaxAggregationFactory(self.feature, group_by=[], time_window=self.time_window)
        return feat

    def count(self) -> Int64:
        from aligned.compiler.aggregation_factory import CountAggregationFactory

        feat = Int64()
        feat.transformation = CountAggregationFactory(self.feature, group_by=[], time_window=self.time_window)
        return feat

    def count_distinct(self) -> Int64:
        from aligned.compiler.aggregation_factory import CountDistinctAggregationFactory

        feat = Int64()
        feat.transformation = CountDistinctAggregationFactory(
            self.feature, group_by=[], time_window=self.time_window
        )
        return feat

    def std(self) -> Float:
        from aligned.compiler.aggregation_factory import StdAggregationFactory

        feat = Float()
        feat.transformation = StdAggregationFactory(self.feature, group_by=[], time_window=self.time_window)
        return feat

    def variance(self) -> Float:
        from aligned.compiler.aggregation_factory import VarianceAggregationFactory

        feat = Float()
        feat.transformation = VarianceAggregationFactory(
            self.feature, group_by=[], time_window=self.time_window
        )
        return feat

    def median(self) -> Float:
        from aligned.compiler.aggregation_factory import MedianAggregationFactory

        feat = Float()
        feat.transformation = MedianAggregationFactory(
            self.feature, group_by=[], time_window=self.time_window
        )
        return feat

    def percentile(self, percentile: float) -> Float:
        from aligned.compiler.aggregation_factory import PercentileAggregationFactory

        feat = Float()
        feat.transformation = PercentileAggregationFactory(
            self.feature, percentile=percentile, group_by=[], time_window=self.time_window
        )
        return feat
