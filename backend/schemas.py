from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, model_validator

FLOAT32_LIMIT = 3.402823466e38
Coordinate = Annotated[float, Field(ge=-FLOAT32_LIMIT, le=FLOAT32_LIMIT, allow_inf_nan=False)]
Altitude = Annotated[float, Field(gt=0.1, le=FLOAT32_LIMIT, allow_inf_nan=False)]
MissionName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class Target(BaseModel):
    north: Coordinate
    east: Coordinate
    altitude: Altitude


class MissionCreate(Target):
    name: MissionName


class MissionUpdate(BaseModel):
    name: MissionName | None = None
    north: Coordinate | None = None
    east: Coordinate | None = None
    altitude: Altitude | None = None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("At least one non-null field is required.")
        return self


class Mission(MissionCreate):
    id: int
