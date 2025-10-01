#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号池认证模块 V2 - 优化版429处理
实现会话级账号管理和智能重试机制
"""

import os
import time
import asyncio
import httpx
from typing import Optional, Dict, Any, Tuple
import threading

from .logging import logger
from .auth import is_token_expired, update_env_file

# 账号池服务配置
POOL_SERVICE_URL = os.getenv("POOL_SERVICE_URL", "http://localhost:8019")
USE_POOL_SERVICE = os.getenv("USE_POOL_SERVICE", "true").lower() == "true"
MAX_429_RETRIES = int(os.getenv("MAX_429_RETRIES", "3"))  # 最大重试次数

# 会话账号映射 (session_id -> account_info)
_session_accounts: Dict[str, Dict[str, Any]] = {}
_session_lock = threading.Lock()


class PoolAuthManagerV2:
    """账号池认证管理器 V2 - 支持会话级账号绑定"""
    
    def __init__(self):
        self.pool_url = POOL_SERVICE_URL
        self.http_timeout = httpx.Timeout(30.0)
        
    async def acquire_account_for_session(self, session_id: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        """
        为会话获取账号（如果会话已有账号则复用，否则从池中获取新账号）
        
        Args:
            session_id: 会话ID，如果为None则不绑定会话
            
        Returns:
            (access_token, account_info)
        """
        # 如果提供了session_id，检查是否已有绑定账号
        if session_id:
            with _session_lock:
                if session_id in _session_accounts:
                    account_info = _session_accounts[session_id]
                    if self._is_account_valid(account_info):
                        logger.info(f"复用会话 {session_id} 的现有账号: {account_info.get('email')}")
                        return account_info["access_token"], account_info
                    else:
                        # 账号已失效，移除
                        logger.warning(f"会话 {session_id} 的账号已失效，重新获取")
                        del _session_accounts[session_id]
        
        # 从账号池获取新账号
        logger.info(f"从账号池获取新账号 (会话: {session_id or 'None'})")
        
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            try:
                # 分配账号
                response = await client.post(
                    f"{self.pool_url}/api/accounts/allocate",
                    json={"count": 1}
                )
                
                if response.status_code != 200:
                    raise RuntimeError(f"分配账号失败: HTTP {response.status_code} {response.text}")
                
                data = response.json()
                
                if not data.get("success"):
                    raise RuntimeError(f"分配账号失败: {data.get('message', '未知错误')}")
                
                accounts = data.get("accounts", [])
                if not accounts:
                    raise RuntimeError("未获得任何账号")
                
                account = accounts[0]
                pool_session_id = data.get("session_id")
                
                logger.info(f"✅ 成功获得账号: {account['email']}, 池会话: {pool_session_id}")
                
                # 获取访问令牌
                access_token = await self._get_access_token_from_account(account)
                
                # 构造账号信息
                account_info = {
                    "email": account["email"],
                    "local_id": account.get("local_id"),
                    "access_token": access_token,
                    "id_token": account.get("id_token"),
                    "refresh_token": account.get("refresh_token"),
                    "pool_session_id": pool_session_id,
                    "created_at": time.time(),
                    "retry_count": 0  # 429重试计数
                }
                
                # 如果提供了session_id，绑定账号到会话
                if session_id:
                    with _session_lock:
                        _session_accounts[session_id] = account_info
                    logger.info(f"账号 {account['email']} 已绑定到会话 {session_id}")
                
                # 更新环境变量（兼容现有代码）
                update_env_file(access_token)
                
                return access_token, account_info
                
            except Exception as e:
                logger.error(f"从账号池获取账号失败: {e}")
                raise RuntimeError(f"账号池服务错误: {str(e)}")
    
    async def _get_access_token_from_account(self, account: Dict[str, Any]) -> str:
        """
        从账号信息获取访问令牌
        
        Args:
            account: 账号信息
            
        Returns:
            访问令牌
        """
        # 优先使用id_token（通常已是有效的JWT）
        id_token = account.get("id_token")
        if id_token and not is_token_expired(id_token):
            return id_token
        
        # 尝试刷新token
        refresh_token = account.get("refresh_token")
        if refresh_token:
            try:
                return await self._refresh_access_token(refresh_token)
            except Exception as e:
                logger.warning(f"刷新token失败: {e}，使用id_token")
                if id_token:
                    return id_token
                raise
        
        # 最后尝试使用id_token（即使可能过期）
        if id_token:
            return id_token
        
        raise RuntimeError("账号缺少有效的认证令牌")
    
    async def _refresh_access_token(self, refresh_token: str) -> str:
        """刷新访问令牌"""
        refresh_url = os.getenv("REFRESH_URL", "https://app.warp.dev/proxy/token?key=AIzaSyBdy3O3S9hrdayLJxJ7mriBR4qgUaUygAs")
        
        payload = f"grant_type=refresh_token&refresh_token={refresh_token}".encode("utf-8")
        headers = {
            "x-warp-client-version": os.getenv("CLIENT_VERSION", "v0.2025.08.06.08.12.stable_02"),
            "x-warp-os-category": os.getenv("OS_CATEGORY", "Windows"),
            "x-warp-os-name": os.getenv("OS_NAME", "Windows"),
            "x-warp-os-version": os.getenv("OS_VERSION", "11 (26100)"),
            "content-type": "application/x-www-form-urlencoded",
        }
        
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.post(refresh_url, headers=headers, content=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"刷新token失败: HTTP {resp.status_code}")
            
            token_data = resp.json()
            access_token = token_data.get("access_token") or token_data.get("id_token")
            
            if not access_token:
                raise RuntimeError(f"响应中无访问令牌: {token_data}")
            
            return access_token
    
    def _is_account_valid(self, account_info: Dict[str, Any]) -> bool:
        """检查账号是否有效"""
        # 检查是否过期（30分钟）
        if time.time() - account_info.get("created_at", 0) > 1800:
            return False
        
        # 检查token是否过期
        access_token = account_info.get("access_token")
        if not access_token:
            return False
        
        try:
            if is_token_expired(access_token):
                return False
        except:
            pass
        
        return True
    
    async def mark_account_as_failed(self, email: str, session_id: Optional[str] = None):
        """
        标记账号为失败（429错误等）
        
        Args:
            email: 账号邮箱
            session_id: 会话ID（如果有）
        """
        logger.warning(f"标记账号为失败: {email}")
        
        # 从会话映射中移除
        if session_id:
            with _session_lock:
                if session_id in _session_accounts:
                    del _session_accounts[session_id]
                    logger.info(f"已从会话 {session_id} 移除失败账号")
        
        # 调用账号池服务删除账号
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await client.delete(
                    f"{self.pool_url}/api/accounts/{email}"
                )
                
                if response.status_code == 200:
                    logger.info(f"✅ 成功从账号池删除失败账号: {email}")
                else:
                    logger.warning(f"删除账号失败: HTTP {response.status_code}")
        except Exception as e:
            logger.error(f"调用删除账号API失败: {e}")
    
    async def trigger_pool_replenish(self):
        """
        触发账号池补充检查
        检查池大小，如果低于最小值则触发补充
        """
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                # 获取账号池状态
                response = await client.get(f"{self.pool_url}/api/accounts/status")
                
                if response.status_code != 200:
                    logger.warning(f"获取账号池状态失败: HTTP {response.status_code}")
                    return
                
                status = response.json()
                pool_stats = status.get("pool_stats", {})
                available = pool_stats.get("available", 0)
                min_size = status.get("min_pool_size", 5)
                
                logger.info(f"账号池状态: 可用={available}, 最小值={min_size}")
                
                # 如果低于最小值，触发补充
                if available < min_size:
                    needed = min_size - available
                    logger.warning(f"⚠️ 账号池不足！需要补充 {needed} 个账号")
                    
                    # 调用补充接口
                    response = await client.post(
                        f"{self.pool_url}/api/accounts/replenish",
                        json={"count": needed}
                    )
                    
                    if response.status_code == 200:
                        logger.info(f"✅ 成功触发账号池补充: {needed} 个账号")
                    else:
                        logger.error(f"触发账号池补充失败: HTTP {response.status_code}")
                else:
                    logger.info("✅ 账号池充足，无需补充")
                    
        except Exception as e:
            logger.error(f"触发账号池补充异常: {e}")
    
    async def release_session_account(self, session_id: str):
        """释放会话绑定的账号"""
        with _session_lock:
            if session_id not in _session_accounts:
                logger.info(f"会话 {session_id} 无绑定账号，跳过释放")
                return
            
            account_info = _session_accounts[session_id]
            pool_session_id = account_info.get("pool_session_id")
            email = account_info.get("email")
            
            logger.info(f"释放会话 {session_id} 的账号: {email}")
            
            # 从映射中移除
            del _session_accounts[session_id]
        
        # 调用账号池服务释放账号
        if pool_session_id:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                    response = await client.post(
                        f"{self.pool_url}/api/accounts/release",
                        json={"session_id": pool_session_id}
                    )
                    
                    if response.status_code == 200:
                        logger.info(f"✅ 成功释放账号池会话: {pool_session_id}")
                    else:
                        logger.warning(f"释放账号池会话失败: HTTP {response.status_code}")
            except Exception as e:
                logger.error(f"释放账号池会话异常: {e}")


# 全局管理器实例
_pool_manager_v2: Optional[PoolAuthManagerV2] = None


def get_pool_manager_v2() -> PoolAuthManagerV2:
    """获取账号池管理器V2实例"""
    global _pool_manager_v2
    if _pool_manager_v2 is None:
        _pool_manager_v2 = PoolAuthManagerV2()
    return _pool_manager_v2


async def handle_429_with_retry(
    session_id: Optional[str],
    error_content: str,
    execute_request_func,
    max_retries: int = MAX_429_RETRIES
) -> Any:
    """
    处理429错误的智能重试逻辑
    
    Args:
        session_id: 会话ID（可选，用于绑定账号）
        error_content: 429错误内容
        execute_request_func: 执行请求的异步函数，接收jwt参数
        max_retries: 最大重试次数
        
    Returns:
        请求结果
        
    Raises:
        RuntimeError: 重试耗尽后仍失败
    """
    if not USE_POOL_SERVICE:
        raise RuntimeError("账号池服务未启用，无法处理429错误")
    
    manager = get_pool_manager_v2()
    
    # 检查是否为配额用尽错误
    is_quota_error = ("No remaining quota" in error_content) or ("No AI requests remaining" in error_content)
    
    if not is_quota_error:
        logger.warning(f"429错误但非配额用尽，不进行重试: {error_content[:100]}")
        raise RuntimeError(f"429错误: {error_content}")
    
    logger.warning(f"🔄 检测到配额用尽错误，开始智能重试 (最多{max_retries}次)")
    
    # 如果有会话绑定的账号，先标记为失败
    with _session_lock:
        if session_id and session_id in _session_accounts:
            old_account = _session_accounts[session_id]
            old_email = old_account.get("email")
            logger.warning(f"标记当前账号为失败: {old_email}")
            # 异步标记失败（不阻塞）
            asyncio.create_task(manager.mark_account_as_failed(old_email, session_id))
    
    # 开始重试循环
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"📍 第 {attempt}/{max_retries} 次重试：从账号池获取新账号...")
            
            # 从账号池获取新账号
            new_jwt, account_info = await manager.acquire_account_for_session(session_id)
            email = account_info.get("email")
            
            logger.info(f"✅ 获取新账号成功: {email}，执行请求...")
            
            # 使用新账号执行请求
            result = await execute_request_func(new_jwt)
            
            logger.info(f"🎉 重试成功！使用账号: {email}")
            
            # 触发账号池补充检查（异步）
            asyncio.create_task(manager.trigger_pool_replenish())
            
            return result
            
        except Exception as e:
            error_msg = str(e)
            
            # 检查是否又是429错误
            if "429" in error_msg:
                logger.error(f"❌ 第 {attempt} 次重试仍返回429: {error_msg[:200]}")
                
                # 标记当前账号为失败
                if session_id:
                    with _session_lock:
                        if session_id in _session_accounts:
                            failed_account = _session_accounts[session_id]
                            failed_email = failed_account.get("email")
                            asyncio.create_task(manager.mark_account_as_failed(failed_email, session_id))
                
                # 如果还有重试机会，继续
                if attempt < max_retries:
                    logger.info(f"⏳ 继续下一次重试...")
                    await asyncio.sleep(1)  # 短暂延迟
                    continue
                else:
                    logger.error(f"💥 已达最大重试次数 ({max_retries})，放弃")
                    # 最后触发一次账号池补充
                    asyncio.create_task(manager.trigger_pool_replenish())
                    raise RuntimeError(f"429错误重试{max_retries}次后仍失败")
            else:
                # 其他错误，直接抛出
                logger.error(f"❌ 请求执行失败（非429）: {error_msg[:200]}")
                raise


async def release_pool_session_v2(session_id: Optional[str] = None):
    """释放账号池会话（清理资源）"""
    if USE_POOL_SERVICE and session_id:
        try:
            manager = get_pool_manager_v2()
            await manager.release_session_account(session_id)
        except Exception as e:
            logger.error(f"释放会话失败: {e}")


def get_current_account_info_v2(session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """获取当前账号信息"""
    if not session_id:
        return None
    
    with _session_lock:
        if session_id in _session_accounts:
            account = _session_accounts[session_id]
            return {
                "email": account.get("email"),
                "uid": account.get("local_id"),
                "session_id": session_id,
                "created_at": account.get("created_at"),
                "retry_count": account.get("retry_count", 0)
            }
    return None
