# /v1/chat/completions 请求流程 - 快速参考

## 🎯 一句话总结

**用户请求 → OpenAI兼容层(消息重排序+请求包构造) → Protobuf桥接层(JSON↔Protobuf转换+JWT认证) → Warp官方API(AI推理) → 响应转换 → 返回用户**

---

## 🔄 5步核心流程

```
┌─────────────────────────────────────────────────────────────┐
│ 步骤1: 接收OpenAI格式请求 (Port 8080)                        │
│ ────────────────────────────────────────────────────────── │
│ POST /v1/chat/completions                                   │
│ {                                                           │
│   "model": "claude-3-5-sonnet",                            │
│   "messages": [{"role": "user", "content": "Hello"}],      │
│   "stream": false                                          │
│ }                                                           │
│                                                             │
│ 文件: protobuf2openai/router.py                             │
│ 函数: chat_completions()                                    │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 步骤2: 消息处理与请求包构造 (OpenAI兼容层)                   │
│ ────────────────────────────────────────────────────────── │
│ 1. 消息重排序 (Anthropic风格)                               │
│    - 提取system消息 → system_prompt                         │
│    - 确保user/assistant交替                                 │
│                                                             │
│ 2. 构造Warp请求包                                           │
│    {                                                        │
│      "task_context": {                                     │
│        "tasks": [{...}],                                   │
│        "active_task_id": "uuid"                            │
│      },                                                     │
│      "input": {                                            │
│        "user_message": {...}                               │
│      },                                                     │
│      "settings": {                                         │
│        "model_config": {"base": "claude-3-5-sonnet"}      │
│      },                                                     │
│      "mcp_context": {                                      │
│        "tools": [...]  // 如果有工具                        │
│      }                                                      │
│    }                                                        │
│                                                             │
│ 关键函数:                                                   │
│ - reorder_messages_for_anthropic()                         │
│ - packet_template()                                        │
│ - attach_user_and_tools_to_inputs()                        │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 步骤3: Protobuf编码与JWT认证 (Port 8000)                     │
│ ────────────────────────────────────────────────────────── │
│ POST http://localhost:8000/api/warp/send_stream(_sse)      │
│                                                             │
│ 1. 获取JWT Token                                            │
│    ├─ 优先: 从账号池获取 (http://localhost:8019)            │
│    ├─ 降级: 注册匿名临时账号                                │
│    └─ 最后: 使用本地.env的JWT                               │
│                                                             │
│ 2. 清理input_schema (MCP工具)                               │
│    - 补充缺失的type/description                             │
│    - 验证JSON Schema格式                                    │
│                                                             │
│ 3. JSON → Protobuf编码                                      │
│    dict_to_protobuf_bytes()                                │
│    ├─ ParseDict (JSON → Proto对象)                         │
│    └─ SerializeToString (Proto → 二进制)                   │
│                                                             │
│ 文件: warp2protobuf/api/protobuf_routes.py                  │
│ 函数: warp_send_stream(), warp_send_stream_sse()           │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 步骤4: 发送到Warp API并接收响应                              │
│ ────────────────────────────────────────────────────────── │
│ POST https://app.warp.dev/ai/multi-agent                   │
│ Content-Type: application/x-protobuf                        │
│ Authorization: Bearer {JWT}                                 │
│ x-warp-client-version: v0.2025.08.06.08.12.stable_02       │
│                                                             │
│ 请求: Protobuf二进制 (Request消息)                          │
│                                                             │
│ 响应: SSE事件流                                             │
│ data: {base64_protobuf}                                     │
│ data: {base64_protobuf}                                     │
│ ...                                                         │
│                                                             │
│ 事件类型:                                                   │
│ ├─ INITIALIZATION (会话初始化)                              │
│ ├─ APPEND_CONTENT (文本追加)                                │
│ ├─ TOOL_CALL (工具调用)                                     │
│ └─ FINISHED (流结束)                                        │
│                                                             │
│ 文件: warp2protobuf/warp/api_client.py                      │
│ 函数: send_protobuf_to_warp_api()                          │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 步骤5: 响应解码与格式转换                                     │
│ ────────────────────────────────────────────────────────── │
│ 【流式】stream=true                                         │
│                                                             │
│ Warp SSE → 逐个处理事件:                                    │
│ 1. Base64解码 → Protobuf字节                                │
│ 2. Protobuf → JSON (protobuf_to_dict)                      │
│ 3. 提取text_delta/tool_call                                │
│ 4. 转换为OpenAI SSE格式                                     │
│    data: {"choices":[{"delta":{"content":"..."}}]}        │
│ 5. 流式yield给客户端                                        │
│                                                             │
│ 文件: protobuf2openai/sse_transform.py                      │
│ 函数: stream_openai_sse()                                   │
│                                                             │
│ ──────────────────────────────────────────────────────────│
│                                                             │
│ 【非流式】stream=false                                      │
│                                                             │
│ 1. 收集所有事件到parsed_events[]                            │
│ 2. 提取完整response文本                                     │
│ 3. 识别tool_calls (如果有)                                  │
│ 4. 构造OpenAI完整响应:                                      │
│    {                                                        │
│      "id": "chatcmpl-xxx",                                 │
│      "object": "chat.completion",                          │
│      "choices": [{                                         │
│        "message": {                                        │
│          "role": "assistant",                              │
│          "content": "完整响应文本"                          │
│        },                                                   │
│        "finish_reason": "stop"                             │
│      }]                                                     │
│    }                                                        │
│                                                             │
│ 文件: protobuf2openai/router.py                             │
│ 函数: chat_completions() (168-218行)                       │
└─────────────────────────────────────────────────────────────┘
                              ↓
                      【返回给客户端】

```

---

## 📊 数据转换链路

```
OpenAI Request
    │
    ├─ messages重排序 (reorder.py)
    │
    ▼
Anthropic格式
    │
    ├─ 构造请求包 (packets.py)
    │
    ▼
Warp JSON
    │
    ├─ Protobuf编码 (protobuf_utils.py)
    │
    ▼
Protobuf Bytes
    │
    ├─ HTTP/2传输
    │
    ▼
Warp API (AI推理)
    │
    ├─ SSE流式返回
    │
    ▼
Protobuf SSE Events
    │
    ├─ Base64解码 + Protobuf解码
    │
    ▼
Warp JSON Events
    │
    ├─ 提取内容 + 转换格式 (sse_transform.py)
    │
    ▼
OpenAI SSE Chunks
    │
    ▼
Client Response
```

---

## 🔑 关键组件速查

| 层级 | 端口 | 主要文件 | 核心功能 |
|-----|------|---------|---------|
| **OpenAI兼容层** | 8080 | `protobuf2openai/router.py` | • 接收OpenAI请求<br>• 消息重排序<br>• 请求包构造 |
| **Protobuf桥接层** | 8000 | `warp2protobuf/api/protobuf_routes.py` | • Protobuf编解码<br>• JWT认证<br>• 转发到Warp |
| **Warp API** | 443 | (外部服务) | • AI模型推理<br>• SSE流式响应 |
| **账号池服务** | 8019 | `account-pool-service/main.py` | • 账号分配<br>• Token刷新 |

---

## 🚦 请求流向图

### 流式请求 (stream=true)

```
Client
  │ POST /v1/chat/completions (stream=true)
  ▼
OpenAI Compat (8080)
  │ 消息处理 + 请求包构造
  ▼
  │ POST /api/warp/send_stream_sse
  ▼
Protobuf Bridge (8000)
  │ JWT认证 + Protobuf编码
  ▼
  │ POST https://app.warp.dev/ai/multi-agent
  ▼
Warp API
  │ AI推理
  │ ───────────────┐
  ▼               │ SSE流
Protobuf Bridge   │
  │ ◄─────────────┘
  │ 解码 + 转换
  │ ───────────────┐
  ▼               │ SSE流
OpenAI Compat     │
  │ ◄─────────────┘
  │ OpenAI格式
  │ ───────────────┐
  ▼               │ SSE流
Client ◄──────────┘
```

### 非流式请求 (stream=false)

```
Client
  │ POST /v1/chat/completions (stream=false)
  ▼
OpenAI Compat (8080)
  │ 消息处理 + 请求包构造
  ▼
  │ POST /api/warp/send_stream
  ▼
Protobuf Bridge (8000)
  │ JWT认证 + Protobuf编码
  ▼
  │ POST https://app.warp.dev/ai/multi-agent
  ▼
Warp API
  │ AI推理 + 收集完整响应
  │
  ▼
Protobuf Bridge
  │ 解码 + 提取完整文本
  │
  ▼
OpenAI Compat
  │ 构造OpenAI响应格式
  │
  ▼
Client (一次性返回完整JSON)
```

---

## ⚡ 关键函数调用链

### 流式调用链

```python
# 1. OpenAI兼容层
router.chat_completions(req)
  └─ if req.stream:
      └─ sse_transform.stream_openai_sse(packet, ...)
          └─ httpx.stream("POST", "/api/warp/send_stream_sse")
              └─ async for line in response.aiter_lines():
                  └─ yield OpenAI_SSE_chunk

# 2. Protobuf桥接层
protobuf_routes.warp_send_stream_sse(request)
  └─ acquire_pool_or_anonymous_token()
      └─ dict_to_protobuf_bytes(data, message_type)
          └─ httpx.stream("POST", WARP_URL)
              └─ async for line in response.aiter_lines():
                  └─ protobuf_to_dict(bytes, message_type)
                      └─ yield SSE_event
```

### 非流式调用链

```python
# 1. OpenAI兼容层
router.chat_completions(req)
  └─ if not req.stream:
      └─ requests.post("/api/warp/send_stream")
          └─ bridge_resp = resp.json()
              └─ extract_tool_calls(bridge_resp)
                  └─ return OpenAI_completion

# 2. Protobuf桥接层
protobuf_routes.warp_send_stream(request)
  └─ acquire_pool_or_anonymous_token()
      └─ dict_to_protobuf_bytes(data, message_type)
          └─ send_protobuf_to_warp_api(bytes)
              └─ collect_all_events()
                  └─ extract_response_text()
                      └─ return {"response": text, "parsed_events": [...]}
```

---

## 🛡️ 错误处理流程

```
请求失败
  │
  ├─ 429 (配额用尽)
  │   ├─ Layer 1 (OpenAI Compat): 刷新JWT → 重试
  │   ├─ Layer 2 (Bridge): 获取新账号 → 重试
  │   └─ Layer 3 (API Client): 注册匿名账号 → 重试
  │
  ├─ 401/403 (认证失败)
  │   └─ 刷新JWT或从账号池获取新账号
  │
  ├─ 500/502/503 (服务器错误)
  │   └─ 直接返回错误给客户端
  │
  └─ Timeout
      └─ 返回超时错误
```

---

## 📈 性能关键点

| 阶段 | 优化措施 | 效果 |
|-----|---------|------|
| HTTP通信 | HTTP/2协议 | 复用连接，减少握手 |
| 数据传输 | Protobuf二进制 | 体积小，序列化快 |
| 流式处理 | AsyncIterator | 内存O(1)，实时响应 |
| 认证 | 账号池预分配 | 减少临时注册延迟 |
| 并发 | 异步I/O (asyncio) | 高并发支持 |

**典型首token延迟**: ~600ms  
**流式 vs 非流式内存**: O(1) vs O(n)

---

## 🔍 快速定位问题

### 按症状查找

| 问题 | 可能位置 | 检查方法 |
|-----|---------|---------|
| 请求参数错误 | `router.py:64` | 检查消息格式 |
| 消息转换失败 | `reorder.py` | 查看重排序逻辑 |
| Protobuf编码失败 | `protobuf_utils.py` | 检查proto定义 |
| JWT过期 | `auth.py`, `pool_auth.py` | 查看token刷新 |
| 429错误 | 各层都有处理 | 参考429_ERROR_HANDLING.md |
| 流式中断 | `sse_transform.py` | 检查SSE解析 |
| 响应格式错误 | `router.py:212` | 查看格式转换 |

### 调试命令

```bash
# 查看OpenAI兼容服务日志
tail -f logs/openai-compat.log

# 查看Protobuf桥接日志
tail -f logs/warp2api.log

# 测试Protobuf编码
curl -X POST http://localhost:8000/api/encode \
  -H "Content-Type: application/json" \
  -d '{"json_data": {...}, "message_type": "warp.multi_agent.v1.Request"}'

# 测试完整流程
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "hi"}]}'
```

---

## 📚 相关文档

- **完整流程详解**: `CHAT_COMPLETIONS_FLOW.md` (29KB)
- **AI开发指南**: `AGENTS.md` (26KB)
- **429错误处理**: `429_ERROR_HANDLING.md` (14KB)
- **部署指南**: `DEPLOYMENT.md` (8KB)
- **项目结构**: `PROJECT_STRUCTURE.md` (7KB)

---

*最后更新: 2025-09-30*  
*维护者: AI Developer Assistant*