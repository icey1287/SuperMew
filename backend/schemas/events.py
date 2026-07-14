from pydantic import BaseModel, ConfigDict, Field

from backend.events.generated.run_event_v1 import RunEventV1


class EventSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunEventsResponse(EventSchema):
    events: list[RunEventV1] = Field(default_factory=list)
    next_after: int
