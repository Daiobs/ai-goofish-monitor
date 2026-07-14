import asyncio
import sys
import os
import argparse
import json
import signal
import contextlib
import re

from src.config import STATE_FILE
from src.failure_guard import FailureGuard
from src.infrastructure.persistence.sqlite_bootstrap import migrate_task_prompts
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.scraper import ScrapeTaskFailed, sanitize_failure_reason, scrape_xianyu


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="闲鱼商品监控脚本，支持多任务配置和实时AI分析。",
        epilog="""
使用示例:
  # 运行 config.json 中定义的所有任务
  python spider_v2.py

  # 按稳定任务 ID 运行单个任务（调度器使用）
  python spider_v2.py --task-id 42

  # 兼容旧入口：任务名必须唯一
  python spider_v2.py --task-name "Sony A7M4"

  # 调试模式: 运行所有任务，但每个任务只处理前3个新发现的商品
  python spider_v2.py --debug-limit 3
""",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--debug-limit", type=int, default=0, help="调试模式：每个任务仅处理前 N 个新商品（0 表示无限制）")
    parser.add_argument("--config", type=str, help="指定任务配置文件路径（传入时优先读取 JSON）")
    selector_group = parser.add_mutually_exclusive_group()
    selector_group.add_argument("--task-id", type=int, help="按稳定 ID 从 SQLite 精确运行单个任务")
    selector_group.add_argument(
        "--task-name",
        type=str,
        help="[deprecated] 仅在任务名唯一时运行",
    )
    args = parser.parse_args()

    loaded_tasks = []
    if args.task_id is not None:
        if args.config:
            print("错误：--task-id 必须从 SQLite 加载，不能与 --config 同时使用。")
            return 1
        migrate_task_prompts()
        repository = SqliteTaskRepository()
        selected_task = await repository.find_by_id(args.task_id)
        if selected_task is None:
            print(f"错误：未找到任务 ID {args.task_id}。")
            return 1
        loaded_tasks = await repository.find_all()
        FailureGuard().migrate_legacy_task_keys(loaded_tasks)
        tasks_config = [selected_task.model_dump()]
    elif args.config:
        if not os.path.exists(args.config):
            sys.exit(f"错误: 配置文件 '{args.config}' 不存在。")
        try:
            with open(args.config, 'r', encoding='utf-8') as f:
                tasks_config = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            sys.exit(
                f"错误: 读取或解析配置文件 '{args.config}' 失败: "
                f"{sanitize_failure_reason(e)}"
            )
    else:
        migrate_task_prompts()
        repository = SqliteTaskRepository()
        loaded_tasks = await repository.find_all()
        FailureGuard().migrate_legacy_task_keys(loaded_tasks)
        tasks_config = [task.model_dump() for task in loaded_tasks]

    def normalize_keywords(value):
        if value is None:
            return []
        if isinstance(value, str):
            raw_values = re.split(r"[\n,]+", value)
        elif isinstance(value, (list, tuple, set)):
            raw_values = list(value)
        else:
            raw_values = [value]

        normalized = []
        seen = set()
        for item in raw_values:
            text = str(item).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(text)
        return normalized

    def flatten_legacy_groups(groups):
        merged = []
        for group in groups or []:
            if isinstance(group, dict):
                merged.extend(normalize_keywords(group.get("include_keywords")))
        return normalize_keywords(merged)

    def has_bound_account(tasks: list) -> bool:
        for task in tasks:
            account = task.get("account_state_file")
            if isinstance(account, str) and account.strip():
                return True
        return False

    def has_any_state_file() -> bool:
        state_dir = os.getenv("ACCOUNT_STATE_DIR", "state").strip().strip('"').strip("'")
        if os.path.isdir(state_dir):
            for name in os.listdir(state_dir):
                if name.endswith(".json"):
                    return True
        return False

    active_task_configs = []
    if args.task_id is not None:
        task_found = tasks_config[0]
        if task_found.get("enabled", False):
            active_task_configs.append(task_found)
        else:
            print(f"任务 ID {args.task_id} 已被禁用，跳过执行。")
    elif args.task_name:
        matching_tasks = [
            task for task in tasks_config
            if task.get("task_name") == args.task_name
        ]
        if len(matching_tasks) > 1:
            print(
                f"错误：任务名 '{args.task_name}' 匹配到 {len(matching_tasks)} 个任务，"
                "请改用 --task-id。"
            )
            return 1
        if not matching_tasks:
            print(f"错误：未找到名为 '{args.task_name}' 的任务。")
            return 1
        task_found = matching_tasks[0]
        if task_found.get("enabled", False):
            active_task_configs.append(task_found)
        else:
            print(f"任务 '{args.task_name}' 已被禁用，跳过执行。")
    else:
        active_task_configs = [
            task for task in tasks_config if task.get("enabled", False)
        ]

    if not active_task_configs:
        print("没有需要执行的任务，程序退出。")
        return 0

    if (
        not os.path.exists(STATE_FILE)
        and not has_bound_account(active_task_configs)
        and not has_any_state_file()
    ):
        print("错误: 未找到登录状态文件。请在 state/ 中添加账号或配置 account_state_file。")
        return 1

    # 读取所有prompt文件内容（关键词模式不需要加载prompt）
    for task in active_task_configs:
        decision_mode = str(task.get("decision_mode", "ai")).strip().lower()
        if decision_mode not in {"ai", "keyword"}:
            decision_mode = "ai"
        task["decision_mode"] = decision_mode
        keyword_rules = task.get("keyword_rules")
        if keyword_rules is None and task.get("keyword_rule_groups") is not None:
            task["keyword_rules"] = flatten_legacy_groups(task.get("keyword_rule_groups") or [])
        else:
            task["keyword_rules"] = normalize_keywords(keyword_rules)

        if decision_mode == "keyword":
            task["ai_prompt_text"] = ""
            continue

        if task.get("enabled", False) and task.get("ai_prompt_base_file") and task.get("ai_prompt_criteria_file"):
            try:
                with open(task["ai_prompt_base_file"], 'r', encoding='utf-8') as f_base:
                    base_prompt = f_base.read()
                with open(task["ai_prompt_criteria_file"], 'r', encoding='utf-8') as f_criteria:
                    criteria_text = f_criteria.read()
                
                # 动态组合成最终的Prompt
                task['ai_prompt_text'] = base_prompt.replace("{{CRITERIA_SECTION}}", criteria_text)
                
                # 验证生成的prompt是否有效
                if len(task['ai_prompt_text']) < 100:
                    print(f"警告: 任务 '{task['task_name']}' 生成的prompt过短 ({len(task['ai_prompt_text'])} 字符)，可能存在问题。")
                elif "{{CRITERIA_SECTION}}" in task['ai_prompt_text']:
                    print(f"警告: 任务 '{task['task_name']}' 的prompt中仍包含占位符，替换可能失败。")
                else:
                    print(f"✅ 任务 '{task['task_name']}' 的prompt生成成功，长度: {len(task['ai_prompt_text'])} 字符")

            except FileNotFoundError as e:
                print(
                    f"警告: 任务 '{task['task_name']}' 的prompt文件缺失: "
                    f"{sanitize_failure_reason(e)}，该任务的AI分析将被跳过。"
                )
                task['ai_prompt_text'] = ""
            except Exception as e:
                print(
                    f"错误: 任务 '{task['task_name']}' 处理prompt文件时发生异常: "
                    f"{sanitize_failure_reason(e)}，该任务的AI分析将被跳过。"
                )
                task['ai_prompt_text'] = ""
        elif task.get("enabled", False) and task.get("ai_prompt_file"):
            try:
                with open(task["ai_prompt_file"], 'r', encoding='utf-8') as f:
                    task['ai_prompt_text'] = f.read()
                print(f"✅ 任务 '{task['task_name']}' 的prompt文件读取成功，长度: {len(task['ai_prompt_text'])} 字符")
            except FileNotFoundError:
                print(f"警告: 任务 '{task['task_name']}' 的prompt文件 '{task['ai_prompt_file']}' 未找到，该任务的AI分析将被跳过。")
                task['ai_prompt_text'] = ""
            except Exception as e:
                print(
                    f"错误: 任务 '{task['task_name']}' 读取prompt文件时发生异常: "
                    f"{sanitize_failure_reason(e)}，该任务的AI分析将被跳过。"
                )
                task['ai_prompt_text'] = ""

    print("\n--- 开始执行监控任务 ---")
    if args.debug_limit > 0:
        print(f"** 调试模式已激活，每个任务最多处理 {args.debug_limit} 个新商品 **")
    
    if args.task_id is not None:
        print(f"** 定时任务模式：只执行任务 ID {args.task_id} **")
    elif args.task_name:
        print(f"** 定时任务模式：只执行任务 '{args.task_name}' **")

    print("--------------------")

    # 为每个启用的任务创建一个异步执行协程
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    tasks = []
    for task_conf in active_task_configs:
        print(f"-> 任务 '{task_conf['task_name']}' 已加入执行队列。")
        tasks.append(asyncio.create_task(scrape_xianyu(task_config=task_conf, debug_limit=args.debug_limit)))

    async def _shutdown_watcher():
        await stop_event.wait()
        print("\n收到终止信号，正在优雅退出，取消所有爬虫任务...")
        for t in tasks:
            if not t.done():
                t.cancel()

    shutdown_task = asyncio.create_task(_shutdown_watcher())

    try:
        # 并发执行所有任务
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        shutdown_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown_task

    print("\n--- 所有任务执行完毕 ---")
    has_failure = False
    for i, result in enumerate(results):
        task_name = active_task_configs[i]['task_name']
        if isinstance(result, asyncio.CancelledError):
            print(f"任务 '{task_name}' 已由用户取消。")
            continue
        if isinstance(result, ScrapeTaskFailed):
            has_failure = True
            if result.failure_kind == "risk_control":
                status = "风控终止"
            elif result.failure_kind == "login_required":
                status = "登录失效"
            else:
                status = "运行异常"
            print(
                f"任务 '{task_name}' {status}: {result.reason} "
                f"(已处理 {result.processed_item_count} 个新商品)"
            )
            continue
        if isinstance(result, BaseException):
            has_failure = True
            print(
                f"任务 '{task_name}' 运行异常: "
                f"{sanitize_failure_reason(result)}"
            )
            continue
        print(f"任务 '{task_name}' 正常结束，本次运行共处理了 {result} 个新商品。")

    return 1 if has_failure else 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
