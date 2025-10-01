# AGENTS.md - AI开发者指南

> 本文档从AI视角描述项目架构，为AI辅助开发提供全面的上下文信息

## 📋 项目概览

**项目名称**: Warp2API with Account Pool Service  
**项目类型**: Warp AI API代理服务（双层架构）  
**主要语言**: Python 3.8+  
**核心框架**: FastAPI, Protobuf, HTTPX, Uvicorn

### 核心功能
1. **Warp AI服务代理**: 将Warp的Protobuf API转换为OpenAI兼容的REST API
2. **账号池管理**: 自动化管理Warp账号的注册、刷新和分配
3. **双协议桥接**: JSON ↔ Protobuf ↔ Warp API 的完整转换链

---

## 🏗️ 架构设计

### 系统架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                         客户端应用层                              │
│              (任何OpenAI兼容的客户端/应用)                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    OpenAI兼容API服务 (Port 8080)                 │
│  功能: 转换OpenAI Chat Completions请求为Warp Protobuf格式       │
│  文件: warp2api-main/openai_compat.py                           │
│  路由: /v1/chat/completions, /v1/models                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              Warp Protobuf桥接服务 (Port 8000)                   │
│  功能: JSON ↔ Protobuf 双向转换，WebSocket监控                  │
│  文件: warp2api-main/server.py                                  │
│  路由: /api/encode, /api/decode, /api/warp/send_stream          │
└─────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │                   │
                    ▼                   ▼
        ┌───────────────────┐  ┌──────────────────┐
        │  账号池服务        │  │  Warp 官方API    │
        │  (Port 8019)      │  │  (SSE Stream)    │
        └───────────────────┘  └──────────────────┘
                    │
                    ▼
        ┌───────────────────┐
        │  SQLite数据库     │
        │  (accounts.db)    │
        └───────────────────┘
```

### 服务端口分配

| 服务 | 端口 | 协议 | 用途 |
|-----|------|------|------|
| Warp Protobuf桥接 | 8000 | HTTP/2 | Protobuf编解码、Warp API转发 |
| OpenAI兼容API | 8080 | HTTP | OpenAI Chat Completions接口 |
| 账号池服务 | 8019 | HTTP | 账号管理RESTful API |
| 监控指标(可选) | 9090 | HTTP | Prometheus metrics |

---

## 📂 项目结构详解

### 核心目录树

```
/workspace/
├── account-pool-service/          # 账号池微服务（独立服务）
│   ├── main.py                   # FastAPI应用入口
│   ├── config.py                 # 配置管理（环境变量+默认值）
│   ├── requirements.txt          # Python依赖
│   ├── account_pool/             # 核心业务逻辑
│   │   ├── pool_manager.py       # 账号池管理器（会话、分配、维护）
│   │   ├── database.py           # SQLite ORM（Account模型）
│   │   ├── batch_register.py     # 批量注册器（Firebase+MoeMail）
│   │   ├── token_refresh_service.py  # JWT令牌刷新服务
│   │   ├── firebase_api_pool.py  # Firebase认证API池
│   │   ├── moemail_client.py     # 临时邮箱客户端
│   │   └── complete_registration.py  # Warp账号激活逻辑
│   └── utils/
│       ├── logger.py             # Loguru日志工具
│       └── helpers.py            # 辅助函数
│
├── warp2api-main/                # Warp API代理主服务
│   ├── server.py                 # Protobuf桥接服务器
│   ├── openai_compat.py          # OpenAI兼容服务器启动器
│   ├── start.py                  # 统一启动入口
│   │
│   ├── protobuf2openai/          # OpenAI兼容层
│   │   ├── app.py               # FastAPI应用定义
│   │   ├── router.py            # /v1/chat/completions路由
│   │   ├── models.py            # Pydantic数据模型
│   │   ├── bridge.py            # 桥接初始化逻辑
│   │   ├── sse_transform.py     # SSE流式响应转换
│   │   ├── packets.py           # Warp请求包构造
│   │   ├── reorder.py           # 消息重排序（Anthropic风格）
│   │   └── helpers.py           # 辅助函数
│   │
│   ├── warp2protobuf/           # Warp Protobuf通信层
│   │   ├── api/
│   │   │   └── protobuf_routes.py  # Protobuf编解码路由
│   │   ├── core/
│   │   │   ├── auth.py          # JWT认证管理
│   │   │   ├── pool_auth.py     # 账号池认证集成
│   │   │   ├── protobuf.py      # Protobuf运行时初始化
│   │   │   ├── protobuf_utils.py # Protobuf工具函数
│   │   │   ├── stream_processor.py # SSE流处理器
│   │   │   ├── session.py       # 会话管理
│   │   │   └── logging.py       # 日志配置
│   │   ├── warp/
│   │   │   ├── api_client.py    # Warp API HTTP客户端
│   │   │   └── response.py      # 响应解析器
│   │   └── config/
│   │       ├── settings.py      # 服务配置
│   │       └── models.py        # 模型定义
│   │
│   └── proto/                   # Protobuf定义文件
│       ├── request.proto        # 请求消息定义
│       ├── response.proto       # 响应消息定义
│       ├── task.proto           # 任务消息定义
│       └── ...                  # 其他proto文件
│
├── config/
│   └── production.env           # 生产环境配置
│
├── docker-compose.yml           # Docker编排配置
├── Dockerfile                   # 容器镜像定义
├── start_production.sh          # 生产环境启动脚本
└── stop_production.sh           # 服务停止脚本
```

---

## 🔑 核心模块详解

### 1. 账号池服务 (account-pool-service/)

#### 1.1 PoolManager (pool_manager.py)

**职责**: 账号池的核心调度器

**关键方法**:
```python
async def allocate_accounts_for_request(request_id: str) -> List[Account]
    # 为请求分配可用账号，自动触发补充机制
    
async def release_accounts_for_request(session_id: str) -> bool
    # 释放会话占用的账号，标记为available
    
async def _ensure_minimum_accounts()
    # 确保池中至少有MIN_POOL_SIZE个可用账号
    
async def _maintenance_loop()
    # 后台维护任务：清理过期会话、自动补充账号
```

**设计模式**:
- 单例模式（通过`get_pool_manager()`获取）
- 线程安全（使用`threading.Lock`）
- 异步事件循环（asyncio）

**配置项** (config.py):
```python
MIN_POOL_SIZE = 5          # 最小账号池大小
MAX_POOL_SIZE = 50         # 最大账号池大小
ACCOUNTS_PER_REQUEST = 1   # 每次请求分配账号数
```

#### 1.2 BatchRegister (batch_register.py)

**职责**: 批量注册新Warp账号

**注册流程**:
```
1. MoeMail创建临时邮箱 → 2. Firebase邮箱登录认证 
→ 3. Warp GraphQL激活账号 → 4. 存入数据库
```

**关键依赖**:
- `MoeMailClient`: 临时邮箱服务客户端
- `FirebaseAPIPool`: Firebase认证API池（支持多API Key轮询）
- `CompleteScriptRegistration`: Warp账号激活器

**并发控制**:
```python
max_workers = 3  # 默认3个并发线程
ThreadPoolExecutor  # 使用线程池执行并发注册
```

#### 1.3 Database (database.py)

**Account模型字段**:
```python
email: str              # 账号邮箱
local_id: str          # Firebase UID
id_token: str          # JWT访问令牌
refresh_token: str     # 刷新令牌
status: str            # available/in_use/expired
session_id: str        # 当前会话ID（若in_use）
created_at: datetime   # 创建时间
last_used: datetime    # 最后使用时间
last_refresh_time: datetime  # 最后刷新时间
use_count: int         # 使用次数
```

**关键方法**:
```python
allocate_accounts_for_session(session_id, count) -> List[Account]
    # 原子操作：分配账号并更新状态
    
release_accounts_for_session(session_id) -> bool
    # 释放会话的所有账号
```

#### 1.4 API路由 (main.py)

**RESTful接口**:
```
POST /api/accounts/allocate       # 分配账号
POST /api/accounts/release        # 释放账号
GET  /api/accounts/status         # 获取池状态
POST /api/accounts/refresh-tokens # 刷新令牌
POST /api/accounts/replenish      # 手动补充账号
GET  /api/accounts/{email}        # 获取账号详情
GET  /health                      # 健康检查
```

**请求示例**:
```bash
curl -X POST http://localhost:8019/api/accounts/allocate \
  -H "Content-Type: application/json" \
  -d '{"session_id": "my_session", "count": 1}'
```

**响应示例**:
```json
{
  "success": true,
  "session_id": "my_session",
  "accounts": [{
    "email": "example@domain.com",
    "id_token": "eyJhbGci...",
    "status": "in_use",
    "use_count": 1
  }]
}
```

---

### 2. Warp Protobuf桥接服务 (warp2api-main/)

#### 2.1 服务器入口 (server.py)

**核心功能**:
1. Protobuf编解码服务
2. Warp API转发代理
3. WebSocket实时监控
4. 账号池集成

**关键端点**:
```
POST /api/encode              # JSON → Protobuf
POST /api/decode              # Protobuf → JSON
POST /api/warp/send           # 转发到Warp API（完整响应）
POST /api/warp/send_stream    # 转发到Warp API（流式解析）
POST /api/warp/send_stream_sse # SSE实时流
GET  /api/auth/status         # JWT状态
POST /api/auth/refresh        # 刷新JWT
WS   /ws                      # WebSocket监控
```

#### 2.2 认证系统

##### 本地认证 (auth.py)
```python
get_jwt_token() -> str
    # 从.env文件读取JWT
    
is_token_expired(token: str) -> bool
    # 检查JWT是否过期
    
refresh_jwt_if_needed()
    # 自动刷新即将过期的JWT
```

##### 账号池认证 (pool_auth.py)
```python
acquire_pool_or_anonymous_token() -> str
    # 优先从账号池获取，失败则降级到匿名令牌
    
get_current_account_info() -> Dict
    # 获取当前使用的账号信息
    
release_pool_session()
    # 释放账号池会话
```

**认证优先级**:
```
1. 账号池服务 (USE_POOL_SERVICE=true)
   ↓ 失败
2. 本地.env配置的JWT
   ↓ 失败
3. 匿名访问令牌（临时注册）
```

#### 2.3 Protobuf处理

##### 编码流程 (protobuf_utils.py)
```python
dict_to_protobuf_bytes(data: Dict, message_type: str) -> bytes
    1. 根据message_type动态导入proto类
    2. 使用google.protobuf.json_format.ParseDict
    3. 序列化为二进制字节
```

##### 解码流程
```python
protobuf_to_dict(protobuf_bytes: bytes, message_type: str) -> Dict
    1. 动态导入proto类并解析字节
    2. 使用MessageToDict转换为JSON
    3. 返回Python字典
```

##### Proto文件编译
```bash
# 编译.proto文件为Python代码
python -m grpc_tools.protoc \
  --python_out=. \
  --proto_path=proto/ \
  proto/*.proto
```

#### 2.4 流式处理 (stream_processor.py)

**SSE事件解析**:
```python
async def process_sse_stream(response: httpx.Response):
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            # 解码Base64 protobuf数据
            # 转换为JSON事件
            # 提取文本内容/工具调用
            yield event
```

**事件类型**:
- `INITIALIZATION`: 会话初始化
- `APPEND_CONTENT`: 文本内容追加
- `TOOL_CALL`: 工具调用请求
- `TOOL_RESPONSE`: 工具执行结果
- `FINISHED`: 流结束

---

### 3. OpenAI兼容层 (protobuf2openai/)

#### 3.1 路由处理器 (router.py)

**核心端点**:
```python
@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionsRequest):
    # 1. 消息重排序（Anthropic风格）
    # 2. 提取system_prompt
    # 3. 构造Warp请求包
    # 4. 调用桥接服务
    # 5. 流式返回OpenAI格式响应
```

#### 3.2 消息重排序 (reorder.py)

**Anthropic风格转换**:
```python
# OpenAI格式:
[{"role": "system", "content": "..."}, 
 {"role": "user", "content": "..."}]

# 转换为Anthropic格式（system独立）:
system_prompt = "..."
messages = [{"role": "user", "content": "..."}]
```

#### 3.3 请求包构造 (packets.py)

**Warp请求包结构**:
```python
{
  "task_context": {
    "tasks": [{
      "task_id": "uuid",
      "model": "claude-3-5-sonnet-20241022",
      "system_prompt": "...",
      "temperature": 0.7
    }]
  },
  "inputs": {
    "messages": [...],  # 历史消息
    "tools": [...]      # MCP工具定义
  }
}
```

#### 3.4 SSE转换 (sse_transform.py)

**OpenAI SSE格式**:
```python
data: {"choices": [{"delta": {"content": "Hello"}}]}
data: {"choices": [{"delta": {"content": " World"}}]}
data: [DONE]
```

**转换逻辑**:
```python
async def stream_openai_sse(warp_events: AsyncIterator):
    async for event in warp_events:
        # 提取文本内容
        # 构造OpenAI格式
        # 转换为SSE
        yield f"data: {json.dumps(chunk)}\n\n"
```

---

## 🔄 数据流详解

### 完整请求流程

```
1. 客户端请求
   POST /v1/chat/completions
   {"model": "claude-3-5-sonnet", "messages": [...]}
   
2. OpenAI兼容层 (router.py)
   ├─ 消息重排序 (reorder.py)
   ├─ 提取system_prompt
   └─ 构造Warp请求包 (packets.py)
   
3. 调用Protobuf桥接
   POST http://localhost:8000/api/warp/send_stream_sse
   Content-Type: application/json
   
4. Protobuf桥接 (server.py)
   ├─ 获取认证令牌
   │  ├─ 从账号池分配账号 (pool_auth.py)
   │  └─ 或使用本地JWT (auth.py)
   ├─ JSON → Protobuf编码 (protobuf_utils.py)
   └─ 转发到Warp API
   
5. Warp API调用 (api_client.py)
   POST https://app.warp.dev/ai/multi-agent
   Content-Type: application/x-protobuf
   Authorization: Bearer {jwt}
   x-warp-client-version: v0.2025.08.06.08.12.stable_02
   
6. SSE流处理
   Warp API → Protobuf字节流
   ├─ Base64解码 (stream_processor.py)
   ├─ Protobuf → JSON解码
   └─ 转换为OpenAI SSE格式
   
7. 返回客户端
   text/event-stream
   data: {"choices": [{"delta": {"content": "..."}}]}
```

---

## 🛠️ 开发指南

### AI开发时的关键注意事项

#### 1. 修改账号池逻辑
**文件**: `account-pool-service/account_pool/pool_manager.py`

**常见任务**:
- 调整补充策略：修改`_ensure_minimum_accounts()`
- 改变分配逻辑：修改`allocate_accounts_for_request()`
- 添加健康检查：扩展`get_pool_status()`

**陷阱**:
- ⚠️ 必须使用`self._lock`保护共享状态
- ⚠️ 数据库操作需要异步包装
- ⚠️ 会话超时时间在`SessionContext.is_expired()`中

#### 2. 添加新的Protobuf消息类型
**步骤**:
1. 在`proto/`目录添加/修改`.proto`文件
2. 编译proto文件：
   ```bash
   cd warp2api-main
   python -m grpc_tools.protoc \
     --python_out=. \
     --proto_path=proto/ \
     proto/your_new_file.proto
   ```
3. 在`protobuf_utils.py`的`MESSAGE_TYPE_MAP`中注册
4. 更新`api/protobuf_routes.py`的路由

#### 3. 修改OpenAI兼容逻辑
**文件**: `warp2api-main/protobuf2openai/router.py`

**关键点**:
- 消息格式转换在`reorder_messages_for_anthropic()`
- 请求包构造在`packet_template()`
- 流式响应在`stream_openai_sse()`

**工具调用支持**:
- MCP工具定义在`packets.py`的`attach_user_and_tools_to_inputs()`
- 工具响应处理在`sse_transform.py`

#### 4. 调试技巧

##### 启用详细日志
```python
# account-pool-service/config.py
LOG_LEVEL = "DEBUG"

# warp2api-main/warp2protobuf/core/logging.py
logger.add(sys.stdout, level="DEBUG")
```

##### 查看Protobuf原始数据
```python
# 在protobuf_utils.py中添加
logger.debug(f"Protobuf hex: {protobuf_bytes.hex()}")
```

##### 测试账号池独立服务
```bash
cd account-pool-service
python main.py

# 另一终端
curl http://localhost:8019/api/accounts/status | jq
```

##### 测试Protobuf编码
```bash
curl -X POST http://localhost:8000/api/encode \
  -H "Content-Type: application/json" \
  -d '{
    "message_type": "warp.multi_agent.v1.AgentRequest",
    "json_data": {"version": 7}
  }'
```

---

## 🔐 安全配置

### 敏感信息位置

#### 1. Firebase API密钥
**配置位置**:
- `account-pool-service/config.py`: `FIREBASE_API_KEYS`
- `config/production.env`: `FIREBASE_API_KEY_1`
- `docker-compose.yml`: `FIREBASE_API_KEY`

**用途**: Firebase邮箱认证

#### 2. MoeMail API密钥
**配置位置**:
- `account-pool-service/config.py`: `MOEMAIL_API_KEY`
- `config/production.env`: `MOEMAIL_API_KEY`

**用途**: 临时邮箱服务认证

#### 3. JWT令牌
**存储位置**:
- `warp2api-main/.env`: `WARP_JWT`, `WARP_REFRESH_TOKEN`
- 运行时从账号池动态获取

**生命周期**: 约1小时，自动刷新

### 环境变量优先级

```
1. 系统环境变量（最高优先级）
2. config/production.env（生产环境）
3. .env文件（本地开发）
4. config.py中的默认值（最低优先级）
```

---

## 🐛 常见问题排查

### 问题1: 账号池无法分配账号

**症状**: `/api/accounts/allocate`返回`success: false`

**排查步骤**:
```bash
# 1. 检查账号池状态
curl http://localhost:8019/api/accounts/status

# 2. 查看日志
tail -f logs/pool-service.log | grep ERROR

# 3. 检查数据库
sqlite3 account-pool-service/accounts.db \
  "SELECT status, COUNT(*) FROM accounts GROUP BY status;"
```

**常见原因**:
- MoeMail服务不可用（检查`MOEMAIL_URL`）
- Firebase API密钥失效
- 并发注册失败（网络问题）

**解决方案**:
```bash
# 手动补充账号
curl -X POST http://localhost:8019/api/accounts/replenish \
  -H "Content-Type: application/json" \
  -d '{"count": 10}'
```

### 问题2: Protobuf编码失败

**症状**: `/api/encode`返回500错误

**排查**:
```python
# 检查proto文件是否编译
ls warp2api-main/proto/*_pb2.py

# 重新编译
cd warp2api-main
python -m grpc_tools.protoc \
  --python_out=. \
  --proto_path=proto/ \
  proto/*.proto
```

**常见原因**:
- `message_type`拼写错误
- JSON数据字段与proto定义不匹配
- 缺少必填字段

### 问题3: JWT令牌过期

**症状**: 请求返回401 Unauthorized

**自动处理**:
- 系统会自动刷新即将过期的令牌
- 账号池模式下会自动申请新账号

**手动刷新**:
```bash
# 刷新Protobuf桥接服务的JWT
curl -X POST http://localhost:8000/api/auth/refresh
```

### 问题4: 流式响应中断

**症状**: SSE流提前结束或超时

**排查**:
```python
# 增加超时时间
# warp2api-main/warp2protobuf/warp/api_client.py
async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)):  # 改为120秒
```

**常见原因**:
- 网络不稳定
- Warp API限流
- 请求内容触发安全检查

---

## 📊 性能优化建议

### 1. 账号池大小调优

**配置文件**: `account-pool-service/config.py`

```python
# 高并发场景
MIN_POOL_SIZE = 20
MAX_POOL_SIZE = 100
ACCOUNTS_PER_REQUEST = 1

# 低并发场景
MIN_POOL_SIZE = 5
MAX_POOL_SIZE = 20
```

**监控指标**:
```bash
# 每30秒检查一次
watch -n 30 'curl -s http://localhost:8019/api/accounts/status | jq ".pool_stats"'
```

### 2. 并发注册优化

**配置**: `batch_register.py`

```python
# 默认3个并发
BatchRegister(max_workers=3)

# 高性能服务器可提升至10
BatchRegister(max_workers=10)
```

### 3. HTTP连接池

**配置**: `account-pool-service/config.py`

```python
CONNECTION_POOL_SIZE = 20    # 增加连接池大小
HTTP_KEEPALIVE = 60         # 延长Keep-Alive时间
```

### 4. 数据库索引

**自动创建**: `database.py`已创建关键索引

```sql
CREATE INDEX idx_email ON accounts(email);
CREATE INDEX idx_status ON accounts(status);
CREATE INDEX idx_session_id ON accounts(session_id);
```

---

## 🚀 部署模式

### 模式1: Docker Compose（推荐生产环境）

```bash
# 启动
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止
docker-compose down
```

**优势**:
- 环境隔离
- 自动重启
- 统一配置

### 模式2: 直接运行

```bash
# 启动所有服务
./start_production.sh

# 停止所有服务
./stop_production.sh
```

**优势**:
- 开发调试方便
- 资源占用少

### 模式3: Systemd服务

**创建服务文件**: `/etc/systemd/system/warp2api.service`

```ini
[Unit]
Description=Warp2API Services
After=network.target

[Service]
Type=forking
ExecStart=/path/to/start_production.sh
ExecStop=/path/to/stop_production.sh
Restart=always

[Install]
WantedBy=multi-user.target
```

**管理命令**:
```bash
sudo systemctl start warp2api
sudo systemctl status warp2api
sudo systemctl enable warp2api
```

---

## 🧪 测试策略

### 单元测试

**账号池服务测试**:
```python
# tests/test_pool_manager.py
async def test_allocate_accounts():
    manager = get_pool_manager()
    accounts = await manager.allocate_accounts_for_request("test_session")
    assert len(accounts) == 1
    assert accounts[0].status == "in_use"
```

### 集成测试

**完整流程测试**:
```python
# tests/test_integration.py
async def test_full_flow():
    # 1. 分配账号
    response = requests.post(
        "http://localhost:8019/api/accounts/allocate",
        json={"count": 1}
    )
    session_id = response.json()["session_id"]
    
    # 2. 使用账号发送请求
    response = requests.post(
        "http://localhost:8080/v1/chat/completions",
        json={
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Hello"}]
        }
    )
    assert response.status_code == 200
    
    # 3. 释放账号
    requests.post(
        "http://localhost:8019/api/accounts/release",
        json={"session_id": session_id}
    )
```

### 压力测试

```bash
# 使用Apache Bench
ab -n 100 -c 10 \
  -p request.json \
  -T "application/json" \
  http://localhost:8080/v1/chat/completions
```

---

## 📝 AI开发者检查清单

### 新功能开发前

- [ ] 阅读相关模块的现有代码
- [ ] 检查是否有类似功能实现
- [ ] 确认配置项位置和默认值
- [ ] 了解数据流路径
- [ ] 查看相关的proto定义

### 代码修改时

- [ ] 保持线程安全（使用锁）
- [ ] 添加适当的日志记录
- [ ] 处理异常情况
- [ ] 更新相关的配置项
- [ ] 遵循现有的代码风格

### 提交前

- [ ] 测试基本功能
- [ ] 检查日志输出
- [ ] 验证错误处理
- [ ] 更新文档
- [ ] 检查是否有硬编码的敏感信息

### 部署前

- [ ] 检查环境变量配置
- [ ] 验证生产环境配置文件
- [ ] 测试健康检查端点
- [ ] 准备回滚方案
- [ ] 监控日志和指标

---

## 🔗 关键依赖版本

**Python核心**:
- Python: 3.8+
- FastAPI: 0.104.1
- Uvicorn: 0.24.0
- Pydantic: 2.5.0

**HTTP客户端**:
- HTTPX: 最新版（支持HTTP/2）
- Requests: 2.31.0

**Protobuf**:
- Protobuf: 最新版
- grpcio-tools: 最新版

**数据库**:
- SQLite3: 内置

**日志**:
- Loguru: 0.7.2

**其他**:
- python-dotenv: 1.0.0
- websockets: 15.0.1+

---

## 📖 相关文档

1. **README.md**: 项目总览和快速开始
2. **PROJECT_STRUCTURE.md**: 详细架构说明
3. **DEPLOYMENT.md**: 生产环境部署指南
4. **account-pool-service/README.md**: 账号池服务文档
5. **warp2api-main/README.md**: Warp2API主服务文档

---

## 🤝 AI开发协作建议

### 理解上下文

**优先级顺序**:
1. 阅读本文档（AGENTS.md）
2. 查看具体模块的源代码
3. 检查相关配置文件
4. 查看日志输出

### 提问模板

**功能实现**:
```
我想实现[功能描述]，这个功能应该：
1. 在[模块名称]中实现
2. 涉及的文件：[文件列表]
3. 关键逻辑：[简要描述]
请帮我：[具体需求]
```

**问题排查**:
```
遇到问题：[错误描述]
复现步骤：[步骤列表]
相关日志：[日志摘要]
已尝试：[尝试过的方案]
需要帮助：[具体问题]
```

### 代码审查重点

1. **安全性**: 敏感信息是否泄露
2. **并发安全**: 是否正确使用锁
3. **错误处理**: 异常是否妥善处理
4. **日志记录**: 关键步骤是否有日志
5. **配置化**: 硬编码是否移到配置

---

## 🎯 总结

本项目是一个**三层架构**的Warp AI代理系统：

1. **OpenAI兼容层**: 提供标准化接口
2. **Protobuf桥接层**: 处理协议转换
3. **账号池服务**: 自动化账号管理

**核心设计理念**:
- **微服务化**: 服务独立部署、独立扩展
- **自动化**: 账号注册、刷新、分配全自动
- **降级策略**: 账号池失败自动降级
- **可观测性**: 完善的日志和监控

**AI开发时的最佳实践**:
- 先理解数据流，再修改代码
- 遵循现有的模式和约定
- 充分利用日志进行调试
- 测试完整的请求链路

---

*文档版本: 1.0*  
*最后更新: 2025-09-30*  
*维护者: AI Developer Assistant*