from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class DeviceFilter(BaseModel):
    currentVersion: str = Field(min_length=1, max_length=128)


class CreateCampaign(BaseModel):
    targetVersion: str = Field(min_length=1, max_length=128)
    imageUrl: HttpUrl
    deviceFilter: DeviceFilter
    maxConcurrent: int = Field(gt=0, le=10000)
    failureThresholdPercent: float = Field(ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def different_version(self):
        if self.targetVersion == self.deviceFilter.currentVersion:
            raise ValueError("targetVersion must differ from currentVersion")
        return self


class Completion(BaseModel):
    commandKey: str = Field(min_length=1, max_length=200)
    faultCode: int = Field(ge=0)
    startTime: datetime
    completeTime: datetime

    @model_validator(mode="after")
    def times(self):
        # The assignment does not specify a timezone format. Treat naive simulator
        # timestamps as UTC; scheduler deadlines always use database receipt time.
        if self.startTime.tzinfo is None:
            self.startTime = self.startTime.replace(tzinfo=UTC)
        if self.completeTime.tzinfo is None:
            self.completeTime = self.completeTime.replace(tzinfo=UTC)
        if self.completeTime < self.startTime:
            raise ValueError("completeTime must not precede startTime")
        return self


class Inform(BaseModel):
    deviceId: str = Field(min_length=1, max_length=200)
    firmwareVersion: str = Field(min_length=1, max_length=128)
    event: Literal["BOOT", "CONNECTION_REQUEST", "PERIODIC", "TRANSFER_COMPLETE"]
    transferComplete: Completion | None = None

    @model_validator(mode="after")
    def require_completion(self):
        if self.event == "TRANSFER_COMPLETE" and self.transferComplete is None:
            raise ValueError("TRANSFER_COMPLETE requires transferComplete")
        return self


class CommandResponse(BaseModel):
    commandKey: str = Field(min_length=1, max_length=200)
    status: Literal["ACCEPTED", "REJECTED"]
    reason: str | None = Field(default=None, max_length=2000)


class FleetDevice(BaseModel):
    deviceId: str = Field(min_length=1, max_length=200)
    firmwareVersion: str = Field(min_length=1, max_length=128)
