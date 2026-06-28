from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field



class HealthResponse(BaseModel):
    status: str
    kafka: str
    db: str

class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, examples=["my-api"])


class CreateProjectResponse(BaseModel):
    project_id: str
    name: str
    api_key: str  # shown exactly once — store it, you can't retrieve it again
    created_at: datetime
