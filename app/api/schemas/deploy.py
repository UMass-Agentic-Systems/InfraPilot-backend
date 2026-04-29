from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DeployRequest(BaseModel):
    app_name: str = Field(min_length=1, max_length=255)
    requirements: str = Field(min_length=1)


class DeployResponse(BaseModel):
    deployment_id: int | None = None
    status: str
    error: str | None = None


class DeploymentDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    app_name: str
    status: str
    desired_state_yaml: str
    created_at: datetime
    updated_at: datetime
