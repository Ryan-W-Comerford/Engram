from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    kafka: str
    db: str
