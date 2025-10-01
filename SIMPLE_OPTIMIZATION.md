# 简化版429优化方案 - 基于现有代码

## 🎯 设计原则

**一套代码，环境变量控制**
- ✅ 不创建V2分支
- ✅ 直接修改现有逻辑
- ✅ 通过配置切换优化/降级
- ✅ 代码统一，易维护

---

## 🔧 核心改动

### 环境变量配置

```bash
# config/production.env

# 429优化开关（默认开启）
ENABLE_SMART_429_RETRY=true

# 最大重试次数（默认3次）
MAX_429_RETRIES=3

# 是否删除失败账号（默认true）
DELETE_FAILED_ACCOUNTS=true

# 是否自动补充账号池（默认true）
AUTO_REPLENISH_POOL=true

# ===== 降级设置 =====
# 如果想用回旧逻辑，设置：
# ENABLE_SMART_429_RETRY=false
```

---

## 📝 代码修改方案

### 修改1: 增强 `pool_auth.py`（不创建新文件）

**位置**: `warp2api-main/warp2protobuf/core/pool_auth.py`

```python
import os

# 配置项
ENABLE_SMART_429_RETRY = os.getenv("ENABLE_SMART_429_RETRY", "true").lower() == "true"
MAX_429_RETRIES = int(os.getenv("MAX_429_RETRIES", "3"))
DELETE_FAILED_ACCOUNTS = os.getenv("DELETE_FAILED_ACCOUNTS", "true").lower() == "true"
AUTO_REPLENISH_POOL = os.getenv("AUTO_REPLENISH_POOL", "true").lower() == "true"

async def acquire_pool_or_anonymous_token() -> str:
    """
    获取访问令牌（优先从账号池，失败则创建临时账号）
    
    Returns:
        访问令牌
    """
    if USE_POOL_SERVICE:
        try:
            # 尝试从账号池获取
            manager = get_pool_manager()
            return await manager.acquire_pool_access_token()
        except Exception as e:
            logger.warning(f"账号池服务不可用: {e}")
            
            # 根据配置决定是否降级
            if ENABLE_SMART_429_RETRY:
                # 新逻辑：不降级，直接失败
                raise RuntimeError(f"账号池服务错误: {str(e)}")
            else:
                # 旧逻辑：降级到临时账号
                logger.warning("降级到临时账号")
    
    # 降级到原来的临时账号逻辑
    from .auth import acquire_anonymous_access_token
    return await acquire_anonymous_access_token()


async def handle_429_error(
    session_id: str,
    error_content: str,
    execute_request_func
) -> Any:
    """
    处理429错误的智能重试
    
    如果 ENABLE_SMART_429_RETRY=false，直接抛出错误
    如果 ENABLE_SMART_429_RETRY=true，执行智能重试
    """
    # 检查是否启用智能重试
    if not ENABLE_SMART_429_RETRY:
        logger.warning("智能429重试已禁用，使用旧逻辑")
        raise RuntimeError(f"429错误: {error_content}")
    
    # 检查是否为配额用尽
    if not ("No remaining quota" in error_content or "No AI requests remaining" in error_content):
        logger.warning("429错误但非配额用尽，不重试")
        raise RuntimeError(f"429错误: {error_content}")
    
    logger.warning(f"🔄 启动智能429重试（最多{MAX_429_RETRIES}次）")
    
    manager = get_pool_manager()
    
    # 重试循环
    for attempt in range(1, MAX_429_RETRIES + 1):
        try:
            logger.info(f"📍 第 {attempt}/{MAX_429_RETRIES} 次重试：获取新账号...")
            
            # 获取新账号
            new_jwt = await manager.acquire_pool_access_token()
            
            # 执行请求
            result = await execute_request_func(new_jwt)
            
            logger.info(f"🎉 重试成功！")
            
            # 触发账号池补充
            if AUTO_REPLENISH_POOL:
                asyncio.create_task(_trigger_pool_replenish())
            
            return result
            
        except Exception as e:
            error_msg = str(e)
            
            if "429" in error_msg:
                logger.error(f"❌ 第 {attempt} 次重试仍返回429")
                
                # 删除失败账号（如果启用）
                if DELETE_FAILED_ACCOUNTS:
                    # 这里需要记录当前使用的账号email
                    # 实际实现时从manager获取
                    pass
                
                if attempt < MAX_429_RETRIES:
                    await asyncio.sleep(1)
                    continue
                else:
                    raise RuntimeError(f"429重试{MAX_429_RETRIES}次后仍失败")
            else:
                raise


async def _trigger_pool_replenish():
    """触发账号池补充"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{POOL_SERVICE_URL}/api/accounts/status")
            if response.status_code == 200:
                status = response.json()
                available = status.get("pool_stats", {}).get("available", 0)
                min_size = status.get("min_pool_size", 5)
                
                if available < min_size:
                    needed = min_size - available
                    logger.warning(f"⚠️ 账号池不足，补充 {needed} 个")
                    await client.post(
                        f"{POOL_SERVICE_URL}/api/accounts/replenish",
                        json={"count": needed}
                    )
    except Exception as e:
        logger.error(f"触发账号池补充失败: {e}")
```

### 修改2: 集成到现有 `api_client.py`

**位置**: `warp2api-main/warp2protobuf/warp/api_client.py`

在现有的429处理部分添加智能重试：

```python
# 在 send_protobuf_to_warp_api 函数中（约105行）

if response.status_code == 429 and attempt == 0 and (
    ("No remaining quota" in error_content) or ("No AI requests remaining" in error_content)
):
    # 根据配置选择处理方式
    if ENABLE_SMART_429_RETRY:
        # 新逻辑：智能重试
        logger.warning("启用智能429重试")
        try:
            from ..core.pool_auth import handle_429_error
            
            # 定义执行函数
            async def execute_with_jwt(jwt_token):
                # 重新执行请求的逻辑
                ...
            
            # 调用智能重试
            result = await handle_429_error(
                session_id=None,  # 可选
                error_content=error_content,
                execute_request_func=execute_with_jwt
            )
            return result
            
        except Exception as e:
            logger.error(f"智能重试失败: {e}")
            return f"❌ 429错误: {error_content}", None, None
    else:
        # 旧逻辑：临时账号
        logger.warning("使用旧的429处理逻辑")
        try:
            new_jwt = await acquire_anonymous_access_token()
        except Exception:
            new_jwt = None
        if new_jwt:
            jwt = new_jwt
            continue
        else:
            logger.error("匿名token申请失败")
            return f"❌ Warp API Error (HTTP {response.status_code}): {error_content}", None, None
```

---

## 🎛️ 使用方式

### 默认配置（启用优化）

```bash
# 不需要任何配置，默认就是优化模式
./start_production.sh
```

**效果**：
- ✅ 智能429重试（最多3次）
- ✅ 自动删除失败账号
- ✅ 自动补充账号池
- ✅ 仅使用账号池（不用临时账号）

### 降级到旧版本

```bash
# 设置环境变量
export ENABLE_SMART_429_RETRY=false

# 或修改 config/production.env
echo "ENABLE_SMART_429_RETRY=false" >> config/production.env

# 重启服务
./stop_production.sh
./start_production.sh
```

**效果**：
- ✅ 使用旧的429处理逻辑
- ✅ 账号池失败时降级到临时账号
- ✅ 简单重试1-2次

### 自定义配置

```bash
# 启用优化，但只重试2次
export ENABLE_SMART_429_RETRY=true
export MAX_429_RETRIES=2

# 启用优化，但不自动补充账号池
export ENABLE_SMART_429_RETRY=true
export AUTO_REPLENISH_POOL=false

# 启用优化，但不删除失败账号
export ENABLE_SMART_429_RETRY=true
export DELETE_FAILED_ACCOUNTS=false
```

---

## 📊 配置对比

| 配置 | 默认值 | 效果 |
|-----|--------|------|
| `ENABLE_SMART_429_RETRY=true` | 是 | 启用智能重试 |
| `ENABLE_SMART_429_RETRY=false` | - | 使用旧逻辑 |
| `MAX_429_RETRIES=3` | 是 | 最多重试3次 |
| `DELETE_FAILED_ACCOUNTS=true` | 是 | 删除失败账号 |
| `AUTO_REPLENISH_POOL=true` | 是 | 自动补充池 |

---

## ✅ 优势

### 1. 简单明了
- ✅ 一套代码，不分叉
- ✅ 环境变量控制
- ✅ 易于理解和维护

### 2. 灵活可控
- ✅ 随时切换优化/旧版
- ✅ 可单独控制每个特性
- ✅ 支持灰度发布

### 3. 向后兼容
- ✅ 默认启用优化
- ✅ 可随时降级
- ✅ 不影响现有代码

---

## 📝 实施步骤

### 步骤1: 修改 pool_auth.py

```bash
# 编辑文件
vim warp2api-main/warp2protobuf/core/pool_auth.py

# 添加上述配置和函数
```

### 步骤2: 修改 api_client.py

```bash
# 编辑文件  
vim warp2api-main/warp2protobuf/warp/api_client.py

# 在429处理部分添加条件判断
```

### 步骤3: 添加配置（可选）

```bash
# 如果想自定义配置
vim config/production.env

# 添加：
# ENABLE_SMART_429_RETRY=true
# MAX_429_RETRIES=3
```

### 步骤4: 重启服务

```bash
./stop_production.sh
./start_production.sh
```

### 步骤5: 验证

```bash
# 查看配置是否生效
tail -f logs/warp2api.log | grep "智能429重试"

# 如果看到这个日志，说明新逻辑已启用
```

---

## 🔄 切换示例

### 临时禁用优化（测试）

```bash
# 临时设置
export ENABLE_SMART_429_RETRY=false
python warp2api-main/server.py

# 恢复
unset ENABLE_SMART_429_RETRY
python warp2api-main/server.py
```

### 永久配置

```bash
# 编辑配置文件
cat >> config/production.env << EOF
# 429优化配置
ENABLE_SMART_429_RETRY=true
MAX_429_RETRIES=3
DELETE_FAILED_ACCOUNTS=true
AUTO_REPLENISH_POOL=true
EOF

# 重启生效
./start_production.sh
```

---

## 📌 总结

### 简化后的方案

**不再有V2分支，只有一套代码 + 配置开关**

```
一套代码
  ├─ ENABLE_SMART_429_RETRY=true  → 优化逻辑
  └─ ENABLE_SMART_429_RETRY=false → 旧逻辑
```

**优势**：
- ✅ 代码统一，易维护
- ✅ 配置灵活，随时切换
- ✅ 无需学习两套系统
- ✅ 降低复杂度

**实施**：
1. 修改现有文件（不创建新文件）
2. 添加配置开关
3. 默认启用优化
4. 可随时降级

---

*简化方案版本: 1.0*  
*更新时间: 2025-09-30*  
*原则: 简单、统一、可控*