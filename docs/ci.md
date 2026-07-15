# CI 与必需质量门禁

本仓库在 Pull Request 指向 `master`、推送到 `master` 以及手动触发时运行两项稳定检查：

- `Quality`
- `Public repository safety`

这两个名称是后续 branch protection 使用的 required check 名称，不应随意修改。CI 和受支持的运行时 Python 固定为 **3.11**；Python 3.9 不受支持。本阶段不声明 Python 3.12 或更高版本兼容。前端 CI 固定使用 **Node 22**，与 Dockerfile 保持一致。

## 本地复现 Quality

在 Python 3.11 环境安装带 hash 的完整 CI lock：

```bash
python -m pip install --require-hashes -r requirements-ci.txt
python -m compileall -q src spider_v2.py
```

先运行 CI helper 测试：

```bash
python -m pytest tests/ci \
  -p scripts.ci.pytest_network_guard \
  --disable-socket \
  --allow-hosts=localhost,127.0.0.1,::1 \
  --allow-unix-socket
```

完整 required pytest 明确排除真实流量测试，并通过 JUnit 检查严格 baseline：

```bash
mkdir -p artifacts
set +e
python -m pytest \
  -m "not live and not live_slow" \
  -o xfail_strict=true \
  -p scripts.ci.pytest_network_guard \
  --disable-socket \
  --allow-hosts=localhost,127.0.0.1,::1 \
  --allow-unix-socket \
  --junitxml=artifacts/pytest-results.xml
pytest_exit_code=$?
set -e
python scripts/ci/check_pytest_baseline.py \
  --junit artifacts/pytest-results.xml \
  --allowlist ci/known-test-failures.txt \
  --pytest-exit-code "$pytest_exit_code" \
  --summary artifacts/pytest-summary.txt
```

网络门禁允许 `localhost`、`127.0.0.1`、`::1` 和 Unix domain socket，外部 IP、DNS 与 HTTP 连接会立即失败。required CI 不安装或启动浏览器。

前端使用已有 lock，不更新 `package-lock.json`：

```bash
cd web-ui
npm ci --ignore-scripts
npm run build
```

如果未来确有依赖需要 lifecycle script，必须先记录具体依赖和原因，再审查是否可以移除 `--ignore-scripts`。

## 更新 Python CI lock

`requirements-ci.in` 引用项目现有 `requirements.txt`，并只增加 CI 专用工具。必须在 Linux + Python 3.11 中更新 lock；不要用 Python 3.9 或其他平台直接覆盖。

```bash
python -m pip install "pip-tools==7.5.3"
python -m piptools compile \
  --generate-hashes \
  --no-emit-index-url \
  --output-file=requirements-ci.txt \
  --strip-extras \
  requirements-ci.in
python -m pip install --require-hashes -r requirements-ci.txt
```

提交前应在全新的 Python 3.11 Linux 环境再次执行最后一条安装命令。CI lock 不改变 Docker 的运行时依赖安装方式。

## 已知失败 contract

`ci/known-test-failures.txt` 只能包含完整 pytest node ID。当前 baseline 是：

```text
tests/test_frontend_build_paths.py::test_frontend_build_output_path_is_consistent_across_configs
tests/unit/test_utils.py::test_save_to_jsonl
```

`scripts/ci/check_pytest_baseline.py` 解析 JUnit XML，不依赖控制台语言或固定 passed 数量。Required contract 必须精确为两项已知失败、0 error、0 skip/xfail；`xfail_strict=true` 使非严格 XPASS 也不能混入成功结果。新增失败、collection/fixture/internal error、任何 skip/xfail、损坏 JUnit、重复 allowlist 条目都会使 CI 失败。如果 allowlist 中的测试已经通过，CI 也会失败；修复该测试的 PR 必须同步删除对应条目。不要使用 `continue-on-error`、skip 或 xfail 隐藏失败。

失败时 Actions 只上传 `artifacts/pytest-summary.txt`，其中只有计数、node ID 和脱敏 contract 问题。原始 JUnit XML 不作为 artifact 上传，因为 failure body 可能包含敏感上下文。

## Public repository safety

本地运行：

```bash
python scripts/ci/check_public_repo_safety.py --repo-root .
BASE_SHA=<reviewed-base-sha> HEAD_SHA=<candidate-head-sha> \
  python scripts/ci/check_git_range_safety.py --repo-root .
python scripts/ci/check_detect_secrets.py \
  --repo-root . \
  --baseline .secrets.baseline \
  --secret-allowlist ci/public-secret-allowlist.yml
```

当前树检查范围来自 `git ls-files`。PR 使用 base/head SHA，master push 使用 `before`/`github.sha`，额外遍历范围内每个 commit 新增或修改的 blob；因此中间 commit 里添加后又删除的 secret、数据库、Cookie、私钥或大型二进制仍会失败。范围 secret 判定不读取当前 `.secrets.baseline` 或 allowlist，修改两者无法掩盖中间 commit。`workflow_dispatch` 没有事件范围时只运行当前树检查。

门禁拒绝环境文件、数据库、Cookie、浏览器状态、日志、运行 JSONL、价格历史、私钥、本地代理配置、备份、大型文件和未审阅二进制。已有项目图片只通过 `ci/public-file-allowlist.txt` 中的逐文件例外保留。

`detect-secrets` 使用 `--no-verify`，仓库内容不会上传到外部服务。`.secrets.baseline` 中每个误报还必须匹配 `ci/public-secret-allowlist.yml` 的精确路径和窄正则；禁止目录级或全局放行。

所有 `.github/workflows/*.yml` 中的外部 action 必须固定到完整 40 位 commit SHA，并在同行注释人类可读版本。Docker action 必须固定 digest。安全检查同时拒绝 `pull_request_target`、默认 write 权限、未验证的远程 shell 管道，以及向 PR shell 命令传入 repository secret。任何由公开评论/讨论触发且拥有 secret 或写权限的 job，必须在 job 级 `if` 中按事件字段明确限定 `OWNER`、`MEMBER` 或 `COLLABORATOR`。Claude Router 使用 `ci/claude-router/package-lock.json` 和 `npm ci --ignore-scripts`，只执行本地 lock 的 `ccr` 二进制，不授予 OIDC。

## Live 测试

`live` 和 `live_slow` 会访问真实外部服务并需要独立测试凭据，因此不属于 required CI。需要人工执行时，应在隔离环境按 `tests/README.md` 准备配置，然后显式运行：

```bash
RUN_LIVE_TESTS=1 python -m pytest tests/live -m "live or live_slow"
```

不要把真实凭据写入命令、文档、测试 fixture 或仓库文件。

## Action pin 更新

1. 只选择官方或成熟维护方发布的 action。
2. 从官方 release/tag 解析其不可变 40 位 commit SHA。
3. 更新 `uses:` 的 SHA，并保留同行版本注释。
4. 运行 CI helper、公开仓库安全检查和 workflow YAML 校验。
5. 在 PR 中记录旧版本、新版本和上游 release 链接。

required workflows 的默认权限只有 `contents: read`，不使用 repository secrets，也不授予 write、package 或 OIDC 权限。缓存只依据 lock 文件加速安装，不能成为测试通过的必要条件。

## 合并后的 branch protection

只有在本 PR 合并、两个 check 名称已经在 GitHub 注册后，才配置 `master` branch protection：

```text
Required checks:
- Quality
- Public repository safety
```

同时建议：require pull request、strict status checks / require up-to-date branch、enforce admins、禁止 force push、禁止删除分支。`dismiss stale approvals` 可按项目需要决定。不要求 deployment check，也不要求 live tests。不得在 required checks 首次出现前提前启用，以免锁死 `master`。
