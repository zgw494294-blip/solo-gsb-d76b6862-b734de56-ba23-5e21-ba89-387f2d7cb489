"""FastAPI 的请求/响应数据模型（含录入校验）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ChannelInput(BaseModel):
    id: str = Field(..., description="通道 ID，全局唯一")
    r: int = Field(..., ge=0, le=1000, description="满档线性 R")
    g: int = Field(..., ge=0, le=1000, description="满档线性 G")
    b: int = Field(..., ge=0, le=1000, description="满档线性 B")
    power_mw: int = Field(..., ge=1, le=10000, description="满档功率 (mW)")

    @field_validator("id")
    @classmethod
    def _strip_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("通道 ID 不能为空")
        return v

    @model_validator(mode="after")
    def _not_all_zero(self) -> "ChannelInput":
        if self.r == 0 and self.g == 0 and self.b == 0:
            raise ValueError("满档 RGB 不能全为零")
        return self


class SolveRequest(BaseModel):
    channels: list[ChannelInput] = Field(..., description="3～6 路 LED，ID 唯一")
    target_r: int = Field(..., ge=0, le=1000)
    target_g: int = Field(..., ge=0, le=1000)
    target_b: int = Field(..., ge=0, le=1000)
    tolerance: int = Field(..., ge=0, le=20, description="共用整数容差")

    @model_validator(mode="after")
    def _check_channels(self) -> "SolveRequest":
        if not 3 <= len(self.channels) <= 6:
            raise ValueError("LED 通道数必须为 3～6 路")
        ids = [c.id for c in self.channels]
        if len(set(ids)) != len(ids):
            dup = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"通道 ID 必须唯一，重复 ID：{', '.join(dup)}")
        return self


class ChannelSolution(BaseModel):
    id: str
    drive: int = Field(..., ge=0, le=255)
    r_full: int
    g_full: int
    b_full: int
    power_full_mw: int


class SolveResponse(BaseModel):
    feasible: bool
    target: list[int]
    tolerance: int
    # 字典序 tie-break 实际使用的 ID 顺序
    sorted_ids: list[str] = []
    channels: list[ChannelSolution] = []
    actual_rgb: list[float] = []
    errors: list[float] = []
    in_range: list[bool] = []
    total_power_mw: float | None = None
    power_num: int | None = Field(
        default=None, description="总功率的整数分子，实际功率 = 该值 / 255"
    )
    message: str = ""


# ---------------- 满档读数标定 ----------------


class CalibChannelRef(BaseModel):
    """标定只需要通道 ID；满档 RGB 是被反求的对象。"""

    id: str = Field(..., description="通道 ID，与样本驱动级按顺序一一对应")

    @field_validator("id")
    @classmethod
    def _strip_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("通道 ID 不能为空")
        return v


class CalibSampleInput(BaseModel):
    drives: list[int] = Field(..., description="各路驱动级，顺序与 channels 一致")
    measured: list[int] = Field(..., description="实测线性 RGB（四舍五入后的整数）")

    @field_validator("drives")
    @classmethod
    def _check_drives(cls, v: list[int]) -> list[int]:
        if any(not isinstance(d, int) or not 0 <= d <= 255 for d in v):
            raise ValueError("每个驱动级必须是 0～255 的整数")
        return v

    @field_validator("measured")
    @classmethod
    def _check_measured(cls, v: list[int]) -> list[int]:
        if len(v) != 3:
            raise ValueError("实测 RGB 必须恰好包含 R、G、B 三个整数")
        if any(not isinstance(m, int) or not 0 <= m <= 6000 for m in v):
            raise ValueError("每个实测分量必须是 0～6000 的整数")
        return v


class CalibrateRequest(BaseModel):
    channels: list[CalibChannelRef] = Field(..., description="3～6 路通道，ID 唯一")
    samples: list[CalibSampleInput] = Field(..., description="3～12 组实测样本")

    @model_validator(mode="after")
    def _check(self) -> "CalibrateRequest":
        if not 3 <= len(self.channels) <= 6:
            raise ValueError("LED 通道数必须为 3～6 路")
        ids = [c.id for c in self.channels]
        if len(set(ids)) != len(ids):
            dup = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"通道 ID 必须唯一，重复 ID：{', '.join(dup)}")
        if not 3 <= len(self.samples) <= 12:
            raise ValueError("标定样本数必须为 3～12 组")
        n = len(self.channels)
        bad = [
            str(i + 1)
            for i, s in enumerate(self.samples)
            if len(s.drives) != n
        ]
        if bad:
            raise ValueError(
                f"以下样本的驱动级数量与通道数（{n} 路）不一致：第 {', '.join(bad)} 组"
            )
        return self


class CalibChannelResult(BaseModel):
    id: str
    r: int
    g: int
    b: int


class CalibrateResponse(BaseModel):
    # unique：唯一（channels 有值）；contradictory：样本矛盾；
    # non_unique：存在多组合法参数；unknown：搜索未完成（不得据此下结论）
    status: Literal["unique", "contradictory", "non_unique", "unknown"]
    channels: list[CalibChannelResult] = []
    message: str = ""
