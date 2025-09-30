# Warp2API系统429错误处理机制详解

## 📋 概述

当用户通过OpenAI兼容接口调用chat completions时，系统有**多层429错误处理机制**，确保服务的高可用性。

---

## 🔄 完整处理流程

### 第一层：OpenAI兼容层 (端口8080)

**文件位置**: 
- `/workspace/warp2api-main/protobuf2openai/router.py` (非流式)
- `/workspace/warp2api-main/protobuf2openai/sse_transform.py` (流式)

#### 非流式请求处理 (router.py: 145-167行)

```python
# 1. 第一次请求
resp = requests.post(
    f"{BRIDGE_BASE_URL}/api/warp/send_stream",
    json={"json_data": packet, "message_type": "warp.multi_agent.v1.Request"},
    timeout=(5.0, 180.0)
)

# 2. 如果返回429，立即刷新JWT并重试
if resp.status_code == 429:
    try:
        # 调用桥接服务的刷新接口
        r = requests.post(f"{BRIDGE_BASE_URL}/api/auth/refresh", timeout=10.0)
        logger.warning("[OpenAI Compat] Bridge returned 429. Tried JWT refresh -> HTTP %s", r.status_code)
    except Exception as _e:
        logger.warning("[OpenAI Compat] JWT refresh attempt failed after 429: %s", _e)
    
    # 3. 使用新token重试
    resp = _post_once()

# 4. 如果仍然失败，返回错误给客户端
if resp.status_code != 200:
    raise HTTPException(resp.status_code, f"bridge_error: {resp.text}")
```

#### 流式请求处理 (sse_transform.py: 43-57行)

```python
# 1. 发起流式请求
async with response_cm as response:
    # 2. 检测到429错误
    if response.status_code == 429:
        try:
            # 3. 刷新JWT
            r = await client.post(f"{BRIDGE_BASE_URL}/api/auth/refresh", timeout=10.0)
            logger.warning("[OpenAI Compat] Bridge returned 429. Tried JWT refresh -> HTTP %s", r.status_code)
        except Exception as _e:
            logger.warning("[OpenAI Compat] JWT refresh attempt failed after 429: %s", _e)
        
        # 4. 重新建立流式连接
        response_cm2 = _do_stream()
        async with response_cm2 as response2:
            response = response2
            if response.status_code != 200:
                error_text = await response.aread()
                logger.error(f"[OpenAI Compat] Bridge HTTP error {response.status_code}: {error_content[:300]}")
                raise RuntimeError(f"bridge error: {error_content}")
```

**处理策略**:
- ✅ 立即刷新JWT token
- ✅ 自动重试一次
- ❌ 如果重试失败，抛出异常给客户端

---

### 第二层：Protobuf桥接层 (端口8000)

**文件位置**: `/workspace/warp2api-main/warp2protobuf/api/protobuf_routes.py` (516-526行)

```python
async with client.stream("POST", warp_url, headers=headers, content=protobuf_bytes) as response:
    if response.status_code != 200:
        error_text = await response.aread()
        error_content = error_text.decode("utf-8") if error_text else ""
        
        # 检测配额用尽的429错误
        if response.status_code == 429 and attempt == 0 and (
            ("No remaining quota" in error_content) or 
            ("No AI requests remaining" in error_content)
        ):
            logger.warning("Warp API 返回 429 (额度用尽, SSE 代理)。尝试获取新token并重试一次…")
            try:
                # 使用账号池或匿名token
                new_jwt = await acquire_pool_or_anonymous_token()
            except Exception:
                new_jwt = None
            
            if new_jwt:
                jwt = new_jwt
                continue  # 重试
            else:
                # 获取新token失败，返回错误
                raise HTTPException(429, error_content)
```

**处理策略**:
- ✅ 检查错误内容是否包含配额用尽信息
- ✅ 调用 `acquire_pool_or_anonymous_token()` 获取新账号
- ✅ 最多重试1次（total 2次尝试）

---

### 第三层：Warp API客户端层

**文件位置**: `/workspace/warp2api-main/warp2protobuf/warp/api_client.py`

#### 主处理函数 (105-123行)

```python
# 循环最多2次
for attempt in range(2):
    jwt = await get_valid_jwt() if attempt == 0 else jwt
    
    async with client.stream("POST", warp_url, headers=headers, content=protobuf_bytes) as response:
        if response.status_code != 200:
            error_text = await response.aread()
            error_content = error_text.decode('utf-8') if error_text else "No error content"
            
            # 第一次失败 + 429 + 配额用尽
            if response.status_code == 429 and attempt == 0 and (
                ("No remaining quota" in error_content) or 
                ("No AI requests remaining" in error_content)
            ):
                logger.warning("WARP API 返回 429 (配额用尽)。尝试申请匿名token并重试一次…")
                try:
                    new_jwt = await acquire_anonymous_access_token()
                except Exception:
                    new_jwt = None
                
                if new_jwt:
                    jwt = new_jwt
                    continue  # 进入第二次循环
                else:
                    logger.error("匿名token申请失败，无法重试。")
                    return f"❌ Warp API Error (HTTP {response.status_code}): {error_content}", None, None
            
            # 其他错误或第二次失败
            logger.error(f"WARP API HTTP ERROR {response.status_code}: {error_content}")
            return f"❌ Warp API Error (HTTP {response.status_code}): {error_content}", None, None
```

**处理策略**:
- ✅ 智能检测：只对配额用尽的429错误重试
- ✅ 自动申请匿名token
- ✅ 最多2次尝试
- ❌ 失败后返回错误信息

---

## 🔑 Token获取优先级

### `acquire_pool_or_anonymous_token()` 函数

**文件位置**: `/workspace/warp2api-main/warp2protobuf/core/pool_auth.py` (210-250行)

```python
async def acquire_pool_or_anonymous_token() -> str:
    """
    优先级策略：
    1. 账号池服务（如果启用）
    2. 匿名临时账号
    """
    global _current_session
    
    # 第一优先级：账号池服务
    if USE_POOL_SERVICE:
        try:
            manager = PoolAuthManager()
            token = await manager.acquire_pool_access_token()
            logger.info("✅ 成功从账号池获取token")
            return token
        except Exception as e:
            logger.warning(f"⚠️ 从账号池获取token失败: {e}，降级到匿名token")
    
    # 第二优先级：匿名临时账号
    try:
        token = await acquire_anonymous_access_token()
        logger.info("✅ 成功获取匿名访问token")
        return token
    except Exception as e:
        logger.error(f"❌ 匿名token申请失败: {e}")
        raise RuntimeError(f"无法获取访问token: {str(e)}")
```

**优先级顺序**:
1. **账号池服务** (如果 `USE_POOL_SERVICE=true`)
   - 从 `http://localhost:8019/api/accounts/allocate` 获取账号
   - 使用账号的refresh_token刷新获取新的access_token
2. **匿名临时账号** (降级方案)
   - 调用 `acquire_anonymous_access_token()` 临时注册新账号
3. **失败** - 抛出异常

---

## 📊 完整流程图

```
用户请求 (POST /v1/chat/completions)
    │
    ▼
┌─────────────────────────────────────┐
│  OpenAI兼容层 (8080端口)             │
│  - 检测429                           │
│  - 刷新JWT                           │
│  - 重试1次                           │
└─────────────────────────────────────┘
    │ 转发
    ▼
┌─────────────────────────────────────┐
│  Protobuf桥接层 (8000端口)           │
│  - 检测429 + 配额用尽                │
│  - 调用 acquire_pool_or_anonymous   │
│  - 重试1次                           │
└─────────────────────────────────────┘
    │ HTTP/2 Protobuf
    ▼
┌─────────────────────────────────────┐
│  Warp API客户端                      │
│  - 检测429 + 配额用尽                │
│  - 申请匿名token                     │
│  - 重试1次                           │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  Warp官方API                         │
│  (https://app.warp.dev/ai/...)      │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  Token获取策略                       │
│  1. 账号池服务 (8019端口)            │
│  2. 匿名临时账号注册                 │
└─────────────────────────────────────┘
```

---

## 🎯 关键特性

### 1. 多层防护
- **3层429检测和处理**：OpenAI层 → 桥接层 → API客户端层
- 每层都有独立的重试机制

### 2. 智能重试
- **条件判断**: 只对 "配额用尽" 的429错误进行重试
- **避免无效重试**: 其他类型的429（如限流）不重试，直接返回错误

### 3. Token自动切换
```python
# 检测错误内容
if "No remaining quota" in error_content or "No AI requests remaining" in error_content:
    # 智能切换token源
    acquire_pool_or_anonymous_token()
```

### 4. 降级策略
```
账号池服务 → 匿名临时账号 → 返回错误
   (优先)      (降级)         (最终)
```

---

## 🔍 错误信息识别

系统通过**错误内容关键词**识别配额用尽：

```python
# 关键词匹配
("No remaining quota" in error_content) or 
("No AI requests remaining" in error_content)
```

**示例错误响应**:
```json
{
  "error": "No remaining quota for this account",
  "code": 429,
  "type": "quota_exceeded"
}
```

---

## ⚙️ 配置项

### 环境变量

| 变量 | 默认值 | 说明 |
|-----|--------|------|
| `USE_POOL_SERVICE` | `true` | 是否启用账号池服务 |
| `POOL_SERVICE_URL` | `http://localhost:8019` | 账号池服务地址 |
| `BRIDGE_BASE_URL` | `http://localhost:8000` | Protobuf桥接服务地址 |

### 重试次数配置

- **OpenAI兼容层**: 1次重试（共2次请求）
- **Protobuf桥接层**: 1次重试（共2次请求）
- **API客户端层**: 1次重试（共2次请求）

**理论最大重试次数**: 8次 (2×2×2)，但实际上通过智能条件判断避免了过度重试

---

## 🛡️ 容错机制

### 1. 账号池不可用时
```python
if USE_POOL_SERVICE:
    try:
        # 尝试从账号池获取
        token = await manager.acquire_pool_access_token()
    except Exception as e:
        logger.warning(f"账号池获取失败: {e}，降级到匿名token")
        # 自动降级到匿名token
        token = await acquire_anonymous_access_token()
```

### 2. 匿名注册失败时
```python
try:
    token = await acquire_anonymous_access_token()
except Exception as e:
    logger.error(f"❌ 匿名token申请失败: {e}")
    raise RuntimeError(f"无法获取访问token: {str(e)}")
```

### 3. JWT刷新失败时
```python
if resp.status_code != 200:
    logger.warning(f"刷新令牌失败，尝试使用id_token")
    id_token = account.get("id_token")
    if id_token:
        return id_token
    raise RuntimeError(f"获取access_token失败")
```

---

## 📝 日志示例

### 正常流程（429后成功恢复）
```
[OpenAI Compat] Bridge returned 429. Tried JWT refresh -> HTTP 200
✅ 成功从账号池获取token
[Warp API Client] ✅ 收到HTTP 200响应
```

### 降级流程（账号池失败）
```
⚠️ 从账号池获取token失败: HTTP 503，降级到匿名token
[Auth] 开始注册匿名临时账号...
✅ 成功获取匿名访问token
```

### 最终失败流程
```
WARP API 返回 429 (配额用尽)。尝试申请匿名token并重试一次…
❌ 匿名token申请失败，无法重试
[OpenAI Compat] Bridge HTTP error 429: No remaining quota
HTTPException: 429 - No remaining quota
```

---

## 🚀 优化建议

### 当前机制的优势
✅ 多层防护确保高可用性  
✅ 智能识别配额用尽错误  
✅ 自动切换token源  
✅ 降级策略完善  

### 可能的改进方向
1. **添加重试延迟**: 当前立即重试，可能遇到同样的限流
   ```python
   await asyncio.sleep(1)  # 延迟1秒后重试
   ```

2. **添加重试计数器**: 记录429错误频率，触发告警
   ```python
   if retry_count_429 > threshold:
       send_alert("429错误频率过高")
   ```

3. **Token预热机制**: 提前从账号池获取token，避免临时申请
   ```python
   # 启动时预先分配token
   await pool_manager.pre_allocate_tokens(count=5)
   ```

4. **添加指数退避**: 多次429后增加等待时间
   ```python
   wait_time = min(2 ** attempt, 30)  # 最多等待30秒
   await asyncio.sleep(wait_time)
   ```

---

## 📌 总结

### 当Warp服务返回429时，系统的处理流程：

1. **第一次检测** (OpenAI兼容层)
   - 调用 `/api/auth/refresh` 刷新JWT
   - 重试请求

2. **第二次检测** (Protobuf桥接层)  
   - 检查是否为配额用尽错误
   - 调用 `acquire_pool_or_anonymous_token()`
   - 从账号池获取新账号或注册匿名账号
   - 重试请求

3. **第三次检测** (API客户端层)
   - 检查是否为配额用尽错误
   - 直接申请匿名token
   - 重试请求

4. **失败处理**
   - 如果所有重试都失败
   - 返回429错误给客户端
   - 记录详细错误日志

**核心机制**: 智能识别配额用尽 → 自动切换token源 → 多层重试 → 降级处理

---

*文档更新时间: 2025-09-30*  
*维护者: AI Developer Assistant*