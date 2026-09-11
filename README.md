# model-router

本地 OpenAI 兼容网关：把多个上游服务/模型的请求，路由到**当时最快**的可用模型。

Codex / OpenAI SDK 只看到一个模型名（默认 `route-fastest`），剩下的选模型由本工具完成。

## 特性
- 支持 `responses` 与 `chat/completions` 两种协议；协议不同时自动转换请求、响应和流式事件
- 自适应测速：先用小探针快速筛选，再对可用模型精测吞吐与首 token 延迟；结果缓存并持续复测
- 能力过滤：上下文窗口 / 工具调用 / 视觉 / 推理，防止「最快但最弱」被打挂
- 失败自动降级：模型挂了自动避开；启动即测一轮，随时可用 `bench` 手动刷新
- 图片请求自动降级：明确的视觉能力错误才切换模型，输入图片错误不会污染视觉能力缓存
- 请求级故障降级：未开始向客户端输出前，遇到 5xx、超时或连接失败会在 3 次/30 秒预算内切换候选模型
- 配置即文档：YAML 一个文件搞定，同事复制改改就能用

## 快速开始
```bash
bash install.sh   # 一键：装依赖 + 生成配置 + 启动原生 App
```

分步执行：
```bash
uv sync
cp config.example.yaml config.yaml   # 填你的服务与模型
uv run model-router check            # 校验配置
uv run model-router bench            # 手动测一轮看速度
uv run model-router serve            # 启动代理 (127.0.0.1:8765)
```
> `install.sh` 直接启动原生 App，不会把 Codex 永久指向一个随后停止的本地代理。

### 接 Codex（一条命令）
```bash
uv run model-router attach   # 持久接入（生成可恢复备份）
uv run model-router detach   # 校验未被外部修改后恢复原文件
```
GUI 默认采用临时接管：App 运行时写入 `~/.codex/config.toml`，关闭后自动恢复。
如果配置在 App 运行期间被 Codex 或用户改过，工具拒绝覆盖并保留备份，必须人工处理。
`attach`/`detach` 同样使用结构化 TOML 编辑、原子写入、文件指纹和并发锁。

### 接任意 OpenAI SDK
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="any")
```

## Web 控制台
独立 App 内嵌本地控制台页面，启动后自动打开；开发模式也可访问 `http://127.0.0.1:8765/console/`：
- 「路由工作台」左侧只有一份按服务分组的模型列表，显示 tokens/s、首响应、错误率、状态和能力
- 模型卡片显示上游返回的输入/输出价格（美元/百万 token）；上游未提供时显示“未提供”，不自行估算
- 支持默认已勾选优先、名称、速度、价格排序；排序偏好保存在当前浏览器
- 右侧可切换「自动择快」和「模型映射」；两者使用同一模型列表但保存独立勾选集合
- 自动择快只在候选池中选择最新测速结果最好的模型；模型映射把勾选模型按 `服务名/模型名` 暴露给 Codex
- 模型列表中的“刷新速度”运行共享自适应测速，不改变两种功能的选择结果
- 「请求日志」查看最近请求的模型、协议、状态、延迟和 Token；「统计看板」查看总请求、成功率、平均响应、Token 和模型分布
- 日志和统计仅保存在当前代理进程内，最多保留 500 条请求日志，不记录提示词、响应正文或 API Key
- 「配置服务」可在 App 内添加、编辑、删除服务，不需要手写 YAML
- 设置页提供「检查更新」，从 Release API 查询最新版本；发现新版本时可直接打开 Release 页面
- 「路由偏好」可设置测速频率（分钟），并选择同一会话是否固定模型
- 模型不手动录入：保存服务时自动调用上游兼容的 `/models` 接口，模型广场展示返回的全部模型；也可单独点击“刷新模型列表”。上游未提供能力元数据时，模型能力按“未知”处理，不会误判为不支持图片
- API Key 只显示是否已设置；编辑时留空会保留原 Key，不会回显到界面
- 保存前由 Pydantic 校验，保存后原子写入并立即重建上游、重新测速
- 服务有不可变 `id`，改名只影响显示，不会丢失 API Key、停用状态或展示顺序；旧配置首次保存时自动补齐 id

## 独立 macOS App

开发模式启动原生窗口：
```bash
uv run model-router gui -c config.yaml
```

构建 App：
```bash
bash scripts/build_app.sh
open dist/ModelRouter.app
```

构建脚本还会生成 `dist/ModelRouter-macos-arm64.dmg` 和同名 `.zip`。把 DMG
里的 App 拖到 `Applications` 即可安装；当前构建是 Apple Silicon 版本，Intel
版本需要在 Intel Mac 上重新构建。未做 Apple Developer 签名/公证时，首次打开
若出现安全提示，请右键 App 选择“打开”。

App 打开后默认只启动本地代理和内嵌控制台，不修改 Codex 配置。打开“Codex 接入”开关后，
才会把 `~/.codex/config.toml` 的当前模型切换为本地 provider。关闭窗口后自动恢复原配置；如果 App 异常退出，
下次打开时只会在文件仍是本工具托管版本时恢复。若期间发生外部修改，App 停止启动，
不覆盖用户文件。App 不打包 `config.yaml`、模型选择状态或 API key。

首次打开打包 App 时会直接进入配置服务页面，不需要编辑 YAML。添加服务、填写
Base URL/API Key、添加模型并点击“保存并重新测速”即可；配置与密钥始终保留在本机。

## CI 自动测试与打包

每次 push 到代码托管平台 都会执行全量 Python 测试、Python 编译检查和控制台 JavaScript 语法检查，随后自动构建带内嵌控制台资源的 wheel，并作为 Pipeline artifact 保存 30 天。推送形如 `v0.2.0` 的 tag 后，Pipeline 会自动创建 Release 并挂载 wheel 下载链接，同时提供 macOS arm64 App 打包任务；该任务需要项目配置 macOS arm64 Runner，普通 Runner 不会因此阻塞。

## 状态持久化
- 自动择快候选池写入 `config.state.yaml`；Codex 映射配置写入 `config.yaml`
- 重启服务后选择自动恢复，不会改动主 `config.yaml`（避免毁掉注释/格式）
- 想恢复全部模型：删除 `config.state.yaml` 后重启

## 配置说明
见 `config.example.yaml` 内注释。核心字段：
- `bench_interval`：后台测速间隔（秒）
- `stick_session_to_model`：同一会话固定模型，默认开启；会话标识只保存哈希，模型不可用时自动重选
- `bench_concurrency`：并发测速数量，默认 4
- `bench_rounds`：后台每个模型的测速轮数，默认 1
- `bench_target_tokens`：精确测速目标 token 数，默认 256；自适应测速会先用 64 token 筛选
- `bench_timeout_seconds`：单个测速探针超时（秒），默认 10，实际最多 10；不影响真实请求超时
- `timeout_seconds`：超时视为不可用
- 每服务的 `wire_api`：`responses` 或 `chat`
- 模型列表由服务的 `/models` 自动发现；旧 YAML 中的 `models` 仅用于兼容导入
- `codex.enabled`：是否在 App 启动时接入 Codex，默认关闭
- `codex.mode`：`fastest` 暴露 `route-fastest`，`mapped` 暴露选中的多个模型
- `codex.models`：映射模式使用稳定的 `service-id/model-name` 列表

## Roadmap
- [x] 客户端真实请求结果回填评分
- [x] 流式首字延迟（记录于 latency）
- [x] 简单 Web 状态页
- [ ] 全局并发限制与排队，防自我博弈
- [ ] 请求级健康检查计数入 EWMA
