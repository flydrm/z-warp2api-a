#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号池认证模块
从账号池服务获取账号，替代临时账号注册
支持智能429重试（通过环境变量控制）
"""

import os
import json
import time
import asyncio
import httpx
from typing import Optional, Tuple, Dict, Any, Callable
from datetime import datetime
import threading

from .logging import logger
from .auth import decode_jwt_payload, is_token_expired, update_env_file

# 账号池服务配置
POOL_SERVICE_URL = os.getenv("POOL_SERVICE_URL", "http://localhost:8019")
USE_POOL_SERVICE = os.getenv("USE_POOL_SERVICE", "true").lower() == "true"

# 429优化配置
ENABLE_SMART_429_RETRY = os.getenv("ENABLE_SMART_429_RETRY", "true").lower() == "true"
MAX_429_RETRIES = int(os.getenv("MAX_429_RETRIES", "3"))
DELETE_FAILED_ACCOUNTS = os.getenv("DELETE_FAILED_ACCOUNTS", "true").lower() == "true"
AUTO_REPLENISH_POOL = os.getenv("AUTO_REPLENISH_POOL", "true").lower() == "true"

# 全局账号信息
_current_session: Optional[Dict[str, Any]] = None
_session_lock = threading.Lock()


class PoolAuthManager:
    """账号池认证管理器"""
    
    def __init__(self):
        self.pool_url = POOL_SERVICE_URL
        self.current_session_id = None
        self.current_account = None
        self.access_token = None
        
    async def acquire_pool_access_token(self) -> str:
        """
        从账号池获取访问令牌
        
        Returns:
            访问令牌
        """
        global _current_session
        
        logger.info(f"从账号池服务获取账号: {self.pool_url}")
        
        try:
            # 检查是否有现有会话
            with _session_lock:
                if _current_session and self._is_session_valid(_current_session):
                    logger.info("使用现有会话账号")
                    return _current_session["access_token"]
            
            # 从账号池分配新账号
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
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
                session_id = data.get("session_id")
                
                logger.info(f"成功获得账号: {account['email']}, 会话: {session_id}")
                
                # 获取访问令牌
                access_token = await self._get_access_token_from_account(account)
                
                # 保存会话信息
                with _session_lock:
                    _current_session = {
                        "session_id": session_id,
                        "account": account,
                        "access_token": access_token,
                        "created_at": time.time()
                    }
                
                self.current_session_id = session_id
                self.current_account = account
                self.access_token = access_token
                
                # 更新环境变量（兼容现有代码）
                update_env_file(access_token)
                
                return access_token
                
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
        # 使用账号的refresh_token获取新的access_token
        refresh_token = account.get("refresh_token")
        if not refresh_token:
            # 如果没有refresh_token，直接使用id_token
            id_token = account.get("id_token")
            if id_token:
                return id_token
            raise RuntimeError("账号缺少认证令牌")
        
        # 调用Warp的token刷新接口
        refresh_url = os.getenv("REFRESH_URL", "https://app.warp.dev/proxy/token?key=AIzaSyBdy3O3S9hrdayLJxJ7mriBR4qgUaUygAs")
        
        payload = f"grant_type=refresh_token&refresh_token={refresh_token}".encode("utf-8")
        headers = {
            "x-warp-client-version": os.getenv("CLIENT_VERSION", "v0.2025.08.06.08.12.stable_02"),
            "x-warp-os-category": os.getenv("OS_CATEGORY", "Darwin"),
            "x-warp-os-name": os.getenv("OS_NAME", "macOS"),
            "x-warp-os-version": os.getenv("OS_VERSION", "14.0"),
            "content-type": "application/x-www-form-urlencoded",
            "accept": "*/*",
            "accept-encoding": "gzip, br",
            "content-length": str(len(payload))
        }
        
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(refresh_url, headers=headers, content=payload)
            if resp.status_code != 200:
                # 如果刷新失败，尝试使用id_token
                logger.warning(f"刷新令牌失败，尝试使用id_token")
                id_token = account.get("id_token")
                if id_token:
                    return id_token
                raise RuntimeError(f"获取access_token失败: HTTP {resp.status_code}")
            
            token_data = resp.json()
            access_token = token_data.get("access_token")
            
            if not access_token:
                # 如果没有access_token，使用id_token
                id_token = account.get("id_token") or token_data.get("id_token")
                if id_token:
                    return id_token
                raise RuntimeError(f"响应中无访问令牌: {token_data}")
            
            return access_token
    
    def _is_session_valid(self, session: Dict[str, Any]) -> bool:
        """
        检查会话是否有效
        
        Args:
            session: 会话信息
            
        Returns:
            是否有效
        """
        # 检查会话是否过期（30分钟）
        if time.time() - session.get("created_at", 0) > 1800:
            return False
        
        # 检查token是否过期
        access_token = session.get("access_token")
        if not access_token:
            return False
        
        # 尝试解码JWT检查过期
        try:
            if is_token_expired(access_token):
                return False
        except:
            # 如果不是JWT格式，检查id_token
            account = session.get("account", {})
            id_token = account.get("id_token")
            if id_token:
                try:
                    if is_token_expired(id_token):
                        return False
                except:
                    pass
        
        return True
    
    async def release_current_session(self):
        """释放当前会话"""
        global _current_session
        
        with _session_lock:
            if not _current_session:
                return
            
            session_id = _current_session.get("session_id")
            if not session_id:
                return
            
            logger.info(f"释放会话: {session_id}")
            
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                    response = await client.post(
                        f"{self.pool_url}/api/accounts/release",
                        json={"session_id": session_id}
                    )
                    
                    if response.status_code == 200:
                        logger.info(f"成功释放会话: {session_id}")
                    else:
                        logger.warning(f"释放会话失败: {response.status_code}")
                        
            except Exception as e:
                logger.error(f"释放会话异常: {e}")
            finally:
                _current_session = None
                self.current_session_id = None
                self.current_account = None
                self.access_token = None


# 全局管理器实例
_pool_manager = None


def get_pool_manager() -> PoolAuthManager:
    """获取账号池管理器实例"""
    global _pool_manager
    if _pool_manager is None:
        _pool_manager = PoolAuthManager()
    return _pool_manager


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
            logger.warning(f"账号池服务不可用，降级到临时账号: {e}")
    
    # 降级到原来的临时账号逻辑
    from .auth import acquire_anonymous_access_token
    return await acquire_anonymous_access_token()


async def release_pool_session():
    """释放账号池会话（清理资源）"""
    if USE_POOL_SERVICE:
        try:
            manager = get_pool_manager()
            await manager.release_current_session()
        except Exception as e:
            logger.error(f"释放会话失败: {e}")


def get_current_account_info() -> Optional[Dict[str, Any]]:
    """获取当前账号信息"""
    with _session_lock:
        if _current_session:
            account = _current_session.get("account")
            if account:
                return {
                    "email": account.get("email"),
                    "uid": account.get("local_id"),
                    "session_id": _current_session.get("session_id"),
                    "created_at": _current_session.get("created_at")
                }
    return None


async def handle_429_with_smart_retry(
    error_content: str,
    execute_request_func: Callable,
    session_id: Optional[str] = None
) -> Any:
    """
    智能429重试处理（通过环境变量控制）
    
    Args:
        error_content: 429错误内容
        execute_request_func: 执行请求的异步函数，接收jwt参数
        session_id: 会话ID（可选）
        
    Returns:
        请求结果
        
    Raises:
        RuntimeError: 重试失败或未启用智能重试
    """
    # 检查是否启用智能重试
    if not ENABLE_SMART_429_RETRY:
        logger.info("智能429重试未启用，使用旧逻辑（临时账号）")
        from .auth import acquire_anonymous_access_token
        try:
            new_jwt = await acquire_anonymous_access_token()
            return await execute_request_func(new_jwt)
        except Exception as e:
            raise RuntimeError(f"临时账号获取失败: {str(e)}")
    
    # 检查是否为配额用尽错误
    is_quota_error = ("No remaining quota" in error_content) or ("No AI requests remaining" in error_content)
    if not is_quota_error:
        logger.warning(f"429错误但非配额用尽，不进行智能重试")
        raise RuntimeError(f"429错误: {error_content}")
    
    logger.warning(f"🔄 启动智能429重试（最多{MAX_429_RETRIES}次）")
    
    if not USE_POOL_SERVICE:
        raise RuntimeError("账号池服务未启用，无法进行智能重试")
    
    manager = get_pool_manager()
    
    # 获取当前失败的账号（如果有）
    current_account = get_current_account_info()
    if current_account and DELETE_FAILED_ACCOUNTS:
        failed_email = current_account.get("email")
        if failed_email:
            logger.warning(f"标记失败账号: {failed_email}")
            asyncio.create_task(_delete_failed_account(failed_email))
    
    # 释放当前会话
    await release_pool_session()
    
    # 重试循环
    for attempt in range(1, MAX_429_RETRIES + 1):
        try:
            logger.info(f"📍 第 {attempt}/{MAX_429_RETRIES} 次重试：从账号池获取新账号...")
            
            # 获取新账号
            new_jwt = await manager.acquire_pool_access_token()
            current_account = get_current_account_info()
            if current_account:
                logger.info(f"✅ 使用账号: {current_account.get('email')}")
            
            # 执行请求
            result = await execute_request_func(new_jwt)
            
            logger.info(f"🎉 第 {attempt} 次重试成功！")
            
            # 触发账号池补充检查
            if AUTO_REPLENISH_POOL:
                asyncio.create_task(_trigger_pool_replenish())
            
            return result
            
        except Exception as e:
            error_msg = str(e)
            
            # 检查是否又是429错误
            if "429" in error_msg:
                logger.error(f"❌ 第 {attempt} 次重试仍返回429: {error_msg[:100]}")
                
                # 标记当前账号为失败
                if DELETE_FAILED_ACCOUNTS:
                    current_account = get_current_account_info()
                    if current_account:
                        failed_email = current_account.get("email")
                        asyncio.create_task(_delete_failed_account(failed_email))
                
                # 释放会话准备下次重试
                await release_pool_session()
                
                # 如果还有重试机会，继续
                if attempt < MAX_429_RETRIES:
                    logger.info(f"⏳ 继续下一次重试...")
                    await asyncio.sleep(1)  # 短暂延迟
                    continue
                else:
                    logger.error(f"💥 已达最大重试次数({MAX_429_RETRIES})，放弃")
                    # 最后触发账号池补充
                    if AUTO_REPLENISH_POOL:
                        asyncio.create_task(_trigger_pool_replenish())
                    raise RuntimeError(f"429错误重试{MAX_429_RETRIES}次后仍失败")
            else:
                # 其他错误，直接抛出
                logger.error(f"❌ 请求失败（非429）: {error_msg[:100]}")
                raise


async def _delete_failed_account(email: str):
    """删除失败账号（异步后台任务）"""
    try:
        logger.info(f"🗑️ 删除失败账号: {email}")
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.delete(
                f"{POOL_SERVICE_URL}/api/accounts/{email}"
            )
            if response.status_code == 200:
                logger.info(f"✅ 成功删除失败账号: {email}")
            else:
                logger.warning(f"删除账号失败: HTTP {response.status_code}")
    except Exception as e:
        logger.error(f"删除账号异常: {e}")


async def _trigger_pool_replenish():
    """触发账号池补充检查（异步后台任务）"""
    try:
        logger.info("🔍 检查账号池是否需要补充...")
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            # 获取账号池状态
            response = await client.get(f"{POOL_SERVICE_URL}/api/accounts/status")
            
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
                logger.warning(f"⚠️ 账号池不足！触发补充 {needed} 个账号")
                
                # 调用补充接口
                response = await client.post(
                    f"{POOL_SERVICE_URL}/api/accounts/replenish",
                    json={"count": needed}
                )
                
                if response.status_code == 200:
                    logger.info(f"✅ 成功触发账号池补充: {needed} 个")
                else:
                    logger.error(f"触发账号池补充失败: HTTP {response.status_code}")
            else:
                logger.info("✅ 账号池充足，无需补充")
                
    except Exception as e:
        logger.error(f"触发账号池补充异常: {e}")