"""
任务管理路由
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from typing import List
import os
import asyncio
from src.api.dependencies import (
    get_process_service,
    get_scheduler_service,
    get_task_generation_service,
    get_task_service,
    get_monitoring_preflight_service,
)
from src.services.task_service import TaskPromptIntegrityError, TaskService
from src.services.process_service import ProcessService
from src.services.scheduler_service import SchedulerService
from src.services.task_generation_service import TaskGenerationService
from src.services.monitoring_preflight import MonitoringPreflightService
from src.services.task_generation_runner import (
    build_task_create,
    run_ai_generation_job,
)
from src.services.task_payloads import serialize_task, serialize_tasks
from src.domain.models.task import TaskCreate, TaskUpdate, TaskGenerateRequest
from src.prompt_utils import generate_criteria
from src.utils import resolve_task_log_path
from src.services.account_strategy_service import normalize_account_strategy
from src.services.price_history_service import delete_task_price_snapshots
from src.services.result_storage_service import (
    delete_task_result_blacklist_rules,
    delete_task_result_records,
)
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _serialize_preflight(report) -> dict | None:
    if report is None:
        return None
    if isinstance(report, dict):
        return report
    to_dict = getattr(report, "to_dict", None)
    return to_dict() if callable(to_dict) else None


def _log_delete_failure(task_id: int, resource: str, exc: BaseException) -> None:
    print(
        f"[TaskDeletion] task_id={task_id} resource={resource} "
        f"error={type(exc).__name__}"
    )


async def _delete_task_record_cancellation_safe(
    service: TaskService,
    task_id: int,
) -> bool:
    """Keep the lifecycle lock until the durable delete has settled."""
    operation = asyncio.create_task(service.delete_task_record(task_id))
    cancellation: asyncio.CancelledError | None = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            if cancellation is not None:
                raise cancellation from None
            raise

    if cancellation is not None:
        try:
            operation.result()
        except BaseException:
            pass
        raise cancellation
    return bool(operation.result())


async def _delete_task_log(task_id: int, task_name: str) -> None:
    log_file_path = resolve_task_log_path(task_id, task_name)
    if os.path.exists(log_file_path):
        await asyncio.to_thread(os.remove, log_file_path)

async def _reload_scheduler_if_needed(
    task_service: TaskService,
    scheduler_service: SchedulerService,
):
    tasks = await task_service.get_all_tasks()
    await scheduler_service.reload_jobs(tasks)


def _has_keyword_rules(rules) -> bool:
    return bool(rules and len(rules) > 0)


def _validate_final_account_strategy(existing_task, task_update: TaskUpdate) -> None:
    account_state_file = (
        task_update.account_state_file
        if task_update.account_state_file is not None
        else existing_task.account_state_file
    )
    account_strategy = normalize_account_strategy(
        task_update.account_strategy,
        account_state_file,
    )
    task_update.account_strategy = account_strategy
    if account_strategy == "fixed" and not account_state_file:
        raise HTTPException(status_code=400, detail="固定账号模式下必须选择账号。")
@router.get("", response_model=List[dict])
async def get_tasks(
    service: TaskService = Depends(get_task_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
):
    """获取所有任务"""
    tasks = await service.get_all_tasks()
    return serialize_tasks(tasks, scheduler_service)
@router.get("/{task_id}", response_model=dict)
async def get_task(
    task_id: int,
    service: TaskService = Depends(get_task_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
):
    """获取单个任务"""
    task = await service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务未找到")
    return serialize_task(task, scheduler_service)
@router.post("/", response_model=dict)
async def create_task(
    task_create: TaskCreate,
    service: TaskService = Depends(get_task_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
):
    """创建新任务"""
    try:
        task = await service.create_task(task_create)
    except TaskPromptIntegrityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _reload_scheduler_if_needed(service, scheduler_service)
    return {"message": "任务创建成功", "task": serialize_task(task, scheduler_service)}
@router.post("/generate", response_model=dict)
async def generate_task(
    req: TaskGenerateRequest,
    service: TaskService = Depends(get_task_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
    generation_service: TaskGenerationService = Depends(get_task_generation_service),
):
    """创建任务。AI模式会生成分析标准，关键词模式直接保存规则。"""
    print(f"收到任务生成请求: {req.task_name}，模式: {req.decision_mode}")

    try:
        mode = req.decision_mode or "ai"
        if mode == "ai":
            job = await generation_service.create_job(req.task_name)
            generation_service.track(
                run_ai_generation_job(
                    job_id=job.job_id,
                    req=req,
                    task_service=service,
                    scheduler_service=scheduler_service,
                    generation_service=generation_service,
                )
            )
            return JSONResponse(
                status_code=202,
                content={
                    "message": "AI 任务生成已开始。",
                    "job": job.model_dump(mode="json"),
                },
            )

        task = await service.create_task(build_task_create(req, ""))
        await _reload_scheduler_if_needed(service, scheduler_service)
        return {"message": "任务创建成功。", "task": serialize_task(task, scheduler_service)}

    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"AI任务生成API发生未知错误: {str(e)}"
        print(error_msg)
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=error_msg)
@router.get("/generate-jobs/{job_id}", response_model=dict)
async def get_task_generation_job(
    job_id: str,
    generation_service: TaskGenerationService = Depends(get_task_generation_service),
):
    """获取任务生成作业状态"""
    job = await generation_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务生成作业未找到")
    return {"job": job.model_dump(mode="json")}
@router.patch("/{task_id}", response_model=dict)
async def update_task(
    task_id: int,
    task_update: TaskUpdate,
    service: TaskService = Depends(get_task_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
):
    """更新任务"""
    try:
        existing_task = await service.get_task(task_id)
        if not existing_task:
            raise HTTPException(status_code=404, detail="任务未找到")
        _validate_final_account_strategy(existing_task, task_update)

        current_mode = getattr(existing_task, "decision_mode", "ai") or "ai"
        target_mode = task_update.decision_mode or current_mode
        description_changed = (
            task_update.description is not None
            and task_update.description != existing_task.description
        )
        switched_to_ai = current_mode != "ai" and target_mode == "ai"
        task_already_updated = False

        if target_mode == "keyword":
            final_rules = (
                task_update.keyword_rules
                if task_update.keyword_rules is not None
                else getattr(existing_task, "keyword_rules", [])
            )
            if not _has_keyword_rules(final_rules):
                raise HTTPException(status_code=400, detail="关键词模式下至少需要一个关键词。")
        if target_mode == "ai" and (description_changed or switched_to_ai):
            print(f"检测到任务 {task_id} 需要刷新 AI 标准文件，开始重新生成...")
            try:
                description_for_ai = (
                    task_update.description
                    if task_update.description is not None
                    else existing_task.description
                )
                if not str(description_for_ai or "").strip():
                    raise HTTPException(status_code=400, detail="AI 模式下详细需求不能为空。")
                print("开始调用 AI 生成新的分析标准...")
                generated_criteria = await generate_criteria(
                    user_description=description_for_ai,
                    reference_file_path="prompts/macbook_criteria.txt"
                )
                if not generated_criteria or len(generated_criteria.strip()) == 0:
                    print("AI 返回的内容为空")
                    raise HTTPException(status_code=500, detail="AI 未能生成分析标准，返回内容为空。")
                task = await service.update_task_with_generated_criteria(
                    task_id,
                    task_update,
                    generated_criteria,
                )
                task_already_updated = True
                print("新的任务级分析标准已保存")
            except HTTPException:
                raise
            except Exception as e:
                error_msg = f"重新生成 criteria 文件时出错: {str(e)}"
                print(error_msg)
                import traceback
                print(traceback.format_exc())
                raise HTTPException(status_code=500, detail=error_msg)
        if not task_already_updated:
            task = await service.update_task(task_id, task_update)
        await _reload_scheduler_if_needed(service, scheduler_service)
        return {"message": "任务更新成功", "task": serialize_task(task, scheduler_service)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
@router.delete("/{task_id}", response_model=dict)
async def delete_task(
    task_id: int,
    service: TaskService = Depends(get_task_service),
    process_service: ProcessService = Depends(get_process_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
):
    """删除任务"""
    async with process_service.task_lifecycle_guard(task_id):
        task = await service.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务未找到")

        try:
            await process_service._stop_task_locked(task_id)
        except Exception as exc:
            _log_delete_failure(task_id, "process_stop", exc)
        try:
            process_is_running = process_service.is_running(task_id)
        except Exception as exc:
            _log_delete_failure(task_id, "process_status", exc)
            raise HTTPException(
                status_code=500,
                detail="无法确认任务进程已停止，删除已取消",
            ) from None
        if process_is_running:
            print(
                f"[TaskDeletion] task_id={task_id} resource=process "
                "error=StillRunning"
            )
            raise HTTPException(status_code=409, detail="任务进程仍在运行，删除已取消")

        try:
            success = await _delete_task_record_cancellation_safe(service, task_id)
        except Exception as exc:
            _log_delete_failure(task_id, "task_record", exc)
            raise HTTPException(status_code=500, detail="任务记录删除失败") from None
        if not success:
            print(
                f"[TaskDeletion] task_id={task_id} resource=task_record "
                "error=DeleteRejected"
            )
            raise HTTPException(status_code=500, detail="任务记录删除失败")

    cleanup_steps = (
        ("prompt", lambda: service.delete_task_prompt(task_id)),
        ("result_items", lambda: delete_task_result_records(task_id)),
        (
            "price_snapshots",
            lambda: asyncio.to_thread(delete_task_price_snapshots, task_id),
        ),
        (
            "blacklist_rules",
            lambda: asyncio.to_thread(delete_task_result_blacklist_rules, task_id),
        ),
        ("log", lambda: _delete_task_log(task_id, task.task_name)),
        (
            "scheduler",
            lambda: _reload_scheduler_if_needed(service, scheduler_service),
        ),
    )
    for resource, cleanup in cleanup_steps:
        try:
            await cleanup()
        except Exception as exc:
            _log_delete_failure(task_id, resource, exc)
    return {"message": "任务删除成功"}


@router.post("/preflight/{task_id}", response_model=dict)
async def preflight_task(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
    preflight_service: MonitoringPreflightService = Depends(
        get_monitoring_preflight_service
    ),
):
    task = await task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务未找到")
    report = await preflight_service.run(task)
    return {"preflight": report.to_dict()}


@router.post("/start/{task_id}", response_model=dict)
async def start_task(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
    process_service: ProcessService = Depends(get_process_service),
):
    """启动单个任务"""
    task = await task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务未找到")
    if not task.enabled:
        raise HTTPException(status_code=400, detail="任务已被禁用，无法启动")
    if task.is_running:
        raise HTTPException(status_code=400, detail="任务已在运行中")
    success = await process_service.start_task(task_id, task.task_name)
    if not success:
        get_report = getattr(process_service, "get_last_preflight_report", None)
        report = get_report(task_id) if callable(get_report) else None
        serialized_report = _serialize_preflight(report)
        if serialized_report and not serialized_report.get("success"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "preflight_failed",
                    "message": "运行环境预检未通过",
                    "preflight": serialized_report,
                },
            )
        raise HTTPException(status_code=500, detail="启动任务失败")
    get_report = getattr(process_service, "get_last_preflight_report", None)
    report = get_report(task_id) if callable(get_report) else None
    return {
        "message": f"任务 '{task.task_name}' 已启动",
        "preflight": _serialize_preflight(report),
    }


@router.post("/stop/{task_id}", response_model=dict)
async def stop_task(
    task_id: int,
    task_service: TaskService = Depends(get_task_service),
    process_service: ProcessService = Depends(get_process_service),
):
    """停止单个任务"""
    task = await task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务未找到")
    await process_service.stop_task(task_id)
    return {"message": f"任务ID {task_id} 已发送停止信号"}
