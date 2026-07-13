"""
新架构的主应用入口
整合所有路由和服务
"""
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api.routes import (
    auth,
    dashboard,
    tasks,
    logs,
    settings,
    prompts,
    results,
    login_state,
    websocket,
    accounts,
)
from src.api.auth import initialize_session_security, require_authenticated_session
from src.api.dependencies import (
    set_process_service,
    set_scheduler_service,
    set_task_generation_service,
)
from src.services.task_service import TaskService
from src.services.process_service import ProcessService
from src.services.scheduler_service import SchedulerService
from src.services.task_log_cleanup_service import cleanup_task_logs
from src.services.task_generation_service import TaskGenerationService
from src.infrastructure.persistence.sqlite_bootstrap import (
    bootstrap_sqlite_storage,
    migrate_task_prompts,
)
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.infrastructure.config.settings import settings as app_settings


# 全局服务实例
process_service = ProcessService()
scheduler_service = SchedulerService(process_service)
task_generation_service = TaskGenerationService()


async def _sync_task_runtime_status(task_id: int, is_running: bool) -> None:
    task_service = TaskService(SqliteTaskRepository())
    task = await task_service.get_task(task_id)
    if not task or task.is_running == is_running:
        return
    await task_service.update_task_status(task_id, is_running)
    await websocket.broadcast_message(
        "task_status_changed",
        {"id": task_id, "is_running": is_running},
    )


process_service.set_lifecycle_hooks(
    on_started=lambda task_id: _sync_task_runtime_status(task_id, True),
    on_stopped=lambda task_id: _sync_task_runtime_status(task_id, False),
)

# 设置全局 ProcessService 实例供依赖注入使用
set_process_service(process_service)
set_scheduler_service(scheduler_service)
set_task_generation_service(task_generation_service)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    print("正在启动应用...")
    initialize_session_security()
    bootstrap_sqlite_storage()
    migrate_task_prompts()
    cleanup_task_logs(keep_days=app_settings.task_log_retention_days)

    # 重置所有任务状态为停止
    task_repo = SqliteTaskRepository()
    task_service = TaskService(task_repo)
    tasks_list = await task_service.get_all_tasks()
    process_service.failure_guard.migrate_legacy_task_keys(tasks_list)

    for task in tasks_list:
        if task.is_running:
            await task_service.update_task_status(task.id, False)

    # 加载定时任务
    await scheduler_service.reload_jobs(tasks_list)
    scheduler_service.start()

    print("应用启动完成")

    yield

    # 关闭时
    print("正在关闭应用...")
    scheduler_service.stop()
    await process_service.stop_all()
    print("应用已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="闲鱼智能监控机器人",
    description="基于AI的闲鱼商品监控系统",
    version="2.0.0",
    lifespan=lifespan
)

# 注册路由
app.include_router(auth.router)

protected_api_dependencies = [Depends(require_authenticated_session)]
app.include_router(tasks.router, dependencies=protected_api_dependencies)
app.include_router(dashboard.router, dependencies=protected_api_dependencies)
app.include_router(logs.router, dependencies=protected_api_dependencies)
app.include_router(settings.router, dependencies=protected_api_dependencies)
app.include_router(prompts.router, dependencies=protected_api_dependencies)
app.include_router(results.router, dependencies=protected_api_dependencies)
app.include_router(login_state.router, dependencies=protected_api_dependencies)
app.include_router(accounts.router, dependencies=protected_api_dependencies)
app.include_router(websocket.router)

# 挂载静态文件
# 旧的静态文件目录（用于截图等）
app.mount("/static", StaticFiles(directory="static"), name="static")

# 挂载 Vue 3 前端构建产物
# 注意：需要在所有 API 路由之后挂载，以避免覆盖 API 路由
import os
if os.path.exists("dist"):
    app.mount("/assets", StaticFiles(directory="dist/assets"), name="assets")


# 健康检查端点
@app.get("/health")
async def health_check():
    """健康检查（无需认证）"""
    return {"status": "healthy", "message": "服务正常运行"}


# 主页路由 - 服务 Vue 3 SPA
@app.get("/")
async def read_root(request: Request):
    """提供 Vue 3 SPA 的主页面"""
    if os.path.exists("dist/index.html"):
        return FileResponse("dist/index.html")
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "前端构建产物不存在，请先运行 cd web-ui && npm run build"}
        )


# Catch-all 路由 - 处理所有前端路由（必须放在最后）
@app.get("/{full_path:path}")
async def serve_spa(request: Request, full_path: str):
    """
    Catch-all 路由，将所有非 API 请求重定向到 index.html
    这样可以支持 Vue Router 的 HTML5 History 模式
    """
    # 如果请求的是静态资源（如 favicon.ico），返回 404
    if full_path.endswith(('.ico', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.css', '.js', '.json')):
        return JSONResponse(status_code=404, content={"error": "资源未找到"})

    # 其他所有路径都返回 index.html，让前端路由处理
    if os.path.exists("dist/index.html"):
        return FileResponse("dist/index.html")
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "前端构建产物不存在，请先运行 cd web-ui && npm run build"}
        )


if __name__ == "__main__":
    import uvicorn
    from src.infrastructure.config.settings import settings

    print(f"启动新架构应用，端口: {app_settings.server_port}")
    uvicorn.run(app, host="0.0.0.0", port=app_settings.server_port)
