"""LED 混光配方台 —— FastAPI 入口。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .schemas import (
    CalibrateRequest,
    CalibrateResponse,
    CalibChannelResult,
    ChannelSolution,
    PathStepChannel,
    PathStepResult,
    PlanPathRequest,
    PlanPathResponse,
    SolveRequest,
    SolveResponse,
)
from .calibration import CalibSample, calibrate
from .path_planner import plan_path
from .solver import Channel, solve, SolverError

# static/ 与 app/ 同级；用绝对路径，保证从任意工作目录启动都能找到页面
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="LED 混光配方台 API",
    description=(
        "基于 OR-Tools CP-SAT 的多通道 LED 混光最小功率配方求解、"
        "起始驱动级到目标色的最小功率调光路径规划与满档读数标定"
    ),
    version="1.1.0",
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/solve", response_model=SolveResponse)
def api_solve(req: SolveRequest) -> SolveResponse:
    channels = [
        Channel(id=c.id, rgb=(c.r, c.g, c.b), power_mw=c.power_mw)
        for c in req.channels
    ]
    target = (req.target_r, req.target_g, req.target_b)
    try:
        sol = solve(channels, target, req.tolerance)
    except SolverError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if sol is None:
        # 明确判定不可行：不返回任何近似配方
        return SolveResponse(
            feasible=False,
            target=[target[0], target[1], target[2]],
            tolerance=req.tolerance,
            message=(
                "无可行配方：在给定容差下，3～6 路 LED 的任意驱动级组合都无法"
                "使三色分量同时落入要求区间（且不裁剪）。可尝试放宽容差、"
                "调整目标色或增加/更换通道。"
            ),
        )

    lo = [t - req.tolerance for t in target]
    hi = [t + req.tolerance for t in target]
    return SolveResponse(
        feasible=True,
        target=list(target),
        tolerance=req.tolerance,
        sorted_ids=sol.sorted_ids,
        channels=[
            ChannelSolution(
                id=req.channels[i].id,
                drive=sol.drives[i],
                r_full=req.channels[i].r,
                g_full=req.channels[i].g,
                b_full=req.channels[i].b,
                power_full_mw=req.channels[i].power_mw,
            )
            for i in range(len(req.channels))
        ],
        actual_rgb=sol.actual_rgb,
        errors=sol.errors,
        in_range=[
            lo[c] <= sol.actual_rgb[c] <= hi[c]
            and 0 <= sol.actual_rgb[c] <= 1000
            for c in range(3)
        ],
        total_power_mw=sol.power_mw,
        power_num=sol.power_num,
        message="求解成功：总功率已最小，相同功率下驱动级序列按 ID 字典序最小。",
    )


@app.post("/api/plan_path", response_model=PlanPathResponse)
def api_plan_path(req: PlanPathRequest) -> PlanPathResponse:
    """从起始驱动级到目标色规划最小化各步功率之和的完整调光路径。"""
    channels = [
        Channel(id=c.id, rgb=(c.r, c.g, c.b), power_mw=c.power_mw)
        for c in req.channels
    ]
    target = (req.target_r, req.target_g, req.target_b)
    status, sol = plan_path(
        channels,
        list(req.start_drives),
        target,
        req.steps,
        req.max_delta,
        req.tolerance,
    )

    base = PlanPathResponse(
        status="unknown",
        target=list(target),
        tolerance=req.tolerance,
        steps=req.steps,
        max_delta=req.max_delta,
        start_drives=list(req.start_drives),
    )

    if status == "infeasible":
        base.status = "infeasible"
        base.message = (
            "无完整调光路径：在给定的步骤数、每步驱动变化上限与容差下，"
            "不存在一条从起始驱动级出发、每步实际 RGB 都落入对应参考色"
            "±容差（且 0～1000）的完整路径。未返回任何局部步骤。"
            "可尝试增加步骤数、放大每步驱动变化上限或放宽容差。"
        )
        return base

    if status == "unknown":
        base.status = "unknown"
        base.message = (
            "调光路径搜索未完成：CP-SAT 未能在求解时限内证明最优性，"
            "按规定不返回任何（可能非最优的）路径步骤。"
            "请调整参数或稍后重试。"
        )
        return base

    assert sol is not None
    steps_out: list[PathStepResult] = []
    for k, step in enumerate(sol.steps):
        prev = req.start_drives if k == 0 else sol.steps[k - 1].drives
        steps_out.append(
            PathStepResult(
                index=step.index,
                channels=[
                    PathStepChannel(
                        id=req.channels[i].id,
                        drive=step.drives[i],
                        delta=step.drives[i] - prev[i],
                    )
                    for i in range(len(req.channels))
                ],
                actual_rgb=step.actual_rgb,
                reference_rgb=step.reference_rgb,
                errors=step.errors,
                in_range=step.in_range,
                power_mw=step.power_mw,
                power_num=step.power_num,
            )
        )

    base.status = "optimal"
    base.start_actual_rgb = sol.start_actual_rgb
    base.steps_result = steps_out
    base.sorted_ids = sol.sorted_ids
    base.tiebreak_complete = sol.tiebreak_complete
    base.total_power_mw = sol.power_mw
    base.power_num = sol.power_num
    if sol.tiebreak_complete:
        base.message = (
            "路径规划成功：各步功率之和已证明最小；相同总功率下驱动级序列"
            "（先按时间步、步内按 ID 字典序）取字典序最小。"
        )
    else:
        base.message = (
            "路径规划成功：各步功率之和已证明最小（字典序 tie-break 未在"
            "时限内跑完，不影响功率最优性）。"
        )
    return base


@app.post("/api/calibrate", response_model=CalibrateResponse)
def api_calibrate(req: CalibrateRequest) -> CalibrateResponse:
    """由 3～12 组实测样本反求各路 0～1000 满档 RGB（整数约束，唯一性可证明）。"""
    ids = [c.id for c in req.channels]
    samples = [
        CalibSample(
            drives=tuple(s.drives),
            measured=(s.measured[0], s.measured[1], s.measured[2]),
        )
        for s in req.samples
    ]
    result = calibrate(ids, samples)
    return CalibrateResponse(
        status=result.status,
        channels=[
            CalibChannelResult(id=cid, r=r, g=g, b=b)
            for (cid, r, g, b) in result.channels
        ],
        message=result.message,
    )


# 静态页面（放最后注册，避免吞掉 /api 路由）
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
