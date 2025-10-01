# V2无缝集成方案 - 用户无感知升级

## 🎯 当前状态分析

### 现状问题
目前V2是**并行版本**，用户需要：
- ❌ 修改端点URL（从 `/api/warp/send_stream` 改为 `/api/warp/send_stream_v2`）
- ❌ 添加 `session_id` 参数
- ❌ 需要代码改动

**这不是无感知升级！**

---

## ✅ 解决方案：三种集成方式

### 方案1：直接替换（推荐 - 完全无感知）

**原理**：让V2完全替代V1，用户无需任何改动

#### 步骤1：修改OpenAI兼容层自动使用V2

**修改文件**：`warp2api-main/protobuf2openai/router.py`

```python
# 在文件顶部添加导入
from ..warp2protobuf.core.pool_auth_v2 import get_pool_manager_v2, handle_429_with_retry
import uuid

# 修改 chat_completions 函数（约第140-166行）
@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionsRequest):
    # ... 前面的代码保持不变 ...
    
    created_ts = int(time.time())
    completion_id = str(uuid.uuid4())
    model_id = req.model or "warp-default"
    
    # 使用completion_id作为session_id（自动生成）
    session_id = completion_id
    
    if req.stream:
        async def _agen():
            async for chunk in stream_openai_sse(packet, completion_id, created_ts, model_id, session_id):  # 传递session_id
                yield chunk
        return StreamingResponse(_agen(), media_type="text/event-stream", ...)
    
    # 非流式请求 - 使用V2端点
    def _post_once() -> requests.Response:
        return requests.post(
            f"{BRIDGE_BASE_URL}/api/warp/send_stream_v2?session_id={session_id}",  # ← 自动使用V2
            json={"json_data": packet, "message_type": "warp.multi_agent.v1.Request"},
            timeout=(5.0, 180.0),
        )
    
    # ... 后面的代码保持不变 ...
```

**修改文件**：`warp2api-main/protobuf2openai/sse_transform.py`

```python
# 修改 stream_openai_sse 函数签名（约第14行）
async def stream_openai_sse(
    packet: Dict[str, Any], 
    completion_id: str, 
    created_ts: int, 
    model_id: str,
    session_id: str = None  # 新增参数
) -> AsyncGenerator[str, None]:
    
    # ... 前面代码保持不变 ...
    
    # 修改请求部分（约第33-38行）
    def _do_stream():
        # 如果没有session_id，使用completion_id
        sid = session_id or completion_id
        return client.stream(
            "POST",
            f"{BRIDGE_BASE_URL}/api/warp/send_stream_sse_v2?session_id={sid}",  # ← 使用V2
            headers={"accept": "text/event-stream"},
            json={"json_data": packet, "message_type": "warp.multi_agent.v1.Request"},
        )
    
    # ... 后面代码保持不变 ...
```

**效果**：
- ✅ 用户完全无感知
- ✅ 自动使用V2的429智能重试
- ✅ 自动使用completion_id作为session_id
- ✅ 无需任何代码改动

---

### 方案2：智能路由（兼容两种模式）

**原理**：V1端点自动转发到V2，保持向后兼容

#### 修改原有端点，内部调用V2

**修改文件**：`warp2api-main/warp2protobuf/api/protobuf_routes.py`

在原有的 `send_to_warp_api_stream_sse` 函数中：

```python
@app.post("/api/warp/send_stream_sse")
async def send_to_warp_api_stream_sse(
    request: EncodeRequest,
    session_id: str = Query(None, description="可选的会话ID")
):
    """
    原有端点，内部自动使用V2逻辑
    如果没有session_id，自动生成一个
    """
    # 自动生成session_id
    if not session_id:
        import uuid
        session_id = str(uuid.uuid4())
    
    # 直接调用V2的实现
    from .warp_routes_v2 import send_to_warp_api_stream_sse_v2
    return await send_to_warp_api_stream_sse_v2(request, session_id)
```

**效果**：
- ✅ V1端点路径不变
- ✅ 自动使用V2逻辑
- ✅ 完全向后兼容

---

### 方案3：配置开关（渐进式升级）

**原理**：通过环境变量控制是否使用V2

```python
# config中添加
USE_V2_429_HANDLING = os.getenv("USE_V2_429_HANDLING", "true").lower() == "true"

# 在请求处理中
if USE_V2_429_HANDLING:
    # 使用V2逻辑
    resp = requests.post(f"{BRIDGE_BASE_URL}/api/warp/send_stream_v2?session_id={session_id}", ...)
else:
    # 使用V1逻辑
    resp = requests.post(f"{BRIDGE_BASE_URL}/api/warp/send_stream", ...)
```

**效果**：
- ✅ 可以随时切换
- ✅ 灰度发布友好
- ⚠️ 需要配置管理

---

## 🚀 推荐实施步骤

### 第一阶段：自动集成（完全无感知）

**执行方案1的修改**：

1. **修改 router.py**
```bash
# 备份原文件
cp warp2api-main/protobuf2openai/router.py warp2api-main/protobuf2openai/router.py.bak

# 应用修改（见下方完整代码）
```

2. **修改 sse_transform.py**
```bash
# 备份原文件
cp warp2api-main/protobuf2openai/sse_transform.py warp2api-main/protobuf2openai/sse_transform.py.bak

# 应用修改（见下方完整代码）
```

3. **重启服务**
```bash
./stop_production.sh
./start_production.sh
```

4. **验证**
```bash
# 用户使用原来的方式，无需改动
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-5-sonnet",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# 后台自动使用V2逻辑，日志会显示智能重试
tail -f logs/warp2api.log | grep "V2\|重试"
```

### 第二阶段：清理冗余（可选）

V2稳定运行后，可以：
1. 移除V1的429处理代码
2. 删除 `acquire_anonymous_access_token` 相关代码
3. 统一使用V2模块

---

## 📝 具体代码修改

### 修改1: router.py（非流式）

```python
# warp2api-main/protobuf2openai/router.py
# 找到 chat_completions 函数，修改如下：

@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionsRequest):
    try:
        initialize_once()
    except Exception as e:
        logger.warning(f"[OpenAI Compat] initialize_once failed or skipped: {e}")

    if not req.messages:
        raise HTTPException(400, "messages 不能为空")

    # ... 消息处理代码保持不变 ...
    
    created_ts = int(time.time())
    completion_id = str(uuid.uuid4())
    model_id = req.model or "warp-default"
    
    # 🔑 关键：使用completion_id作为session_id
    session_id = completion_id

    if req.stream:
        # 流式请求：传递session_id到sse_transform
        async def _agen():
            async for chunk in stream_openai_sse(packet, completion_id, created_ts, model_id, session_id):
                yield chunk
        return StreamingResponse(_agen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # 非流式请求：直接使用V2端点
    def _post_once() -> requests.Response:
        return requests.post(
            f"{BRIDGE_BASE_URL}/api/warp/send_stream_v2?session_id={session_id}",  # ✅ V2端点
            json={"json_data": packet, "message_type": "warp.multi_agent.v1.Request"},
            timeout=(5.0, 180.0),
        )

    try:
        resp = _post_once()
        # V2端点内部已处理429，这里无需特殊处理
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"bridge_error: {resp.text}")
        bridge_resp = resp.json()
    except Exception as e:
        raise HTTPException(502, f"bridge_unreachable: {e}")

    # ... 后续处理代码保持不变 ...
```

### 修改2: sse_transform.py（流式）

```python
# warp2api-main/protobuf2openai/sse_transform.py
# 修改函数签名和请求部分：

async def stream_openai_sse(
    packet: Dict[str, Any], 
    completion_id: str, 
    created_ts: int, 
    model_id: str,
    session_id: str = None  # 🔑 新增参数
) -> AsyncGenerator[str, None]:
    try:
        first = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": model_id,
            "choices": [{"index": 0, "delta": {"role": "assistant"}}],
        }
        try:
            logger.info("[OpenAI Compat] 转换后的 SSE(emit): %s", json.dumps(first, ensure_ascii=False))
        except Exception:
            pass
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"

        timeout = httpx.Timeout(60.0)
        async with httpx.AsyncClient(http2=True, timeout=timeout, trust_env=True) as client:
            def _do_stream():
                # 🔑 使用session_id，如果没有则使用completion_id
                sid = session_id or completion_id
                return client.stream(
                    "POST",
                    f"{BRIDGE_BASE_URL}/api/warp/send_stream_sse_v2?session_id={sid}",  # ✅ V2端点
                    headers={"accept": "text/event-stream"},
                    json={"json_data": packet, "message_type": "warp.multi_agent.v1.Request"},
                )

            # V2端点内部已处理429重试，这里只需正常处理响应
            response_cm = _do_stream()
            async with response_cm as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    error_content = error_text.decode("utf-8") if error_text else ""
                    logger.error(f"[OpenAI Compat] Bridge HTTP error {response.status_code}: {error_content[:300]}")
                    raise RuntimeError(f"bridge error: {error_content}")
                
                # ... 流式处理代码保持不变 ...
```

---

## ✅ 验证无感知集成

### 测试脚本

```bash
#!/bin/bash
# test_seamless_v2.sh

echo "=== 测试V2无感知集成 ==="

# 1. 用户使用原有API（无任何改动）
echo "1. 测试原有API调用方式..."
RESPONSE=$(curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-5-sonnet",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }')

if echo "$RESPONSE" | grep -q "choices"; then
    echo "   ✅ API调用成功"
else
    echo "   ❌ API调用失败"
    echo "   响应: $RESPONSE"
    exit 1
fi

# 2. 检查后台是否使用V2
echo "2. 检查是否自动使用V2逻辑..."
if grep -q "send_stream_v2" logs/warp2api.log 2>/dev/null; then
    echo "   ✅ 已自动使用V2端点"
else
    echo "   ⚠️  可能仍在使用V1端点"
fi

# 3. 测试流式请求
echo "3. 测试流式请求..."
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-5-sonnet",
    "messages": [{"role": "user", "content": "Hi"}],
    "stream": true
  }' | head -5 | grep -q "data:" && echo "   ✅ 流式请求成功" || echo "   ❌ 流式请求失败"

echo ""
echo "=== 无感知集成验证完成 ==="
```

### 预期结果

**用户视角**：
- ✅ API调用方式完全不变
- ✅ 请求和响应格式不变
- ✅ 无需任何代码修改

**系统视角**：
- ✅ 自动使用V2的智能429重试
- ✅ 自动管理session_id
- ✅ 自动维护账号池
- ✅ 日志显示V2逻辑生效

---

## 🎯 总结对比

### 当前状态（V2并行版本）

```
用户需要改动：
❌ 修改端点 URL
❌ 添加 session_id 参数
❌ 需要代码改动

示例：
# 旧代码
POST /api/warp/send_stream

# 新代码（需要改）
POST /api/warp/send_stream_v2?session_id=xxx
```

### 无感知集成后

```
用户无需改动：
✅ API调用方式不变
✅ 参数格式不变
✅ 零代码改动

示例：
# 用户代码完全不变
POST /v1/chat/completions
{
  "model": "claude-3-5-sonnet",
  "messages": [...]
}

# 后台自动：
# 1. 生成session_id = completion_id
# 2. 调用V2端点
# 3. 智能429重试
# 4. 账号池维护
```

---

## 📋 实施检查清单

### 代码修改
- [ ] 修改 `router.py` - 非流式自动使用V2
- [ ] 修改 `sse_transform.py` - 流式自动使用V2
- [ ] 添加 session_id 自动生成逻辑
- [ ] 备份原文件

### 测试验证
- [ ] 非流式请求测试
- [ ] 流式请求测试
- [ ] 429重试验证
- [ ] 日志检查V2生效

### 部署上线
- [ ] 灰度发布（可选）
- [ ] 监控日志
- [ ] 性能对比
- [ ] 用户反馈

---

## 🚀 快速实施

### 一键应用修改

```bash
# 1. 备份文件
cp warp2api-main/protobuf2openai/router.py warp2api-main/protobuf2openai/router.py.bak
cp warp2api-main/protobuf2openai/sse_transform.py warp2api-main/protobuf2openai/sse_transform.py.bak

# 2. 应用修改（见上方代码）

# 3. 重启服务
./stop_production.sh
./start_production.sh

# 4. 验证
bash test_seamless_v2.sh
```

### 回滚方案

```bash
# 如果有问题，立即回滚
cp warp2api-main/protobuf2openai/router.py.bak warp2api-main/protobuf2openai/router.py
cp warp2api-main/protobuf2openai/sse_transform.py.bak warp2api-main/protobuf2openai/sse_transform.py
./stop_production.sh
./start_production.sh
```

---

**结论**：通过方案1的修改，用户完全无感知，系统自动使用V2的所有优化！✅

*文档更新: 2025-09-30*  
*版本: 无感知集成方案 v1.0*