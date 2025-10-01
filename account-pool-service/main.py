#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立账号池服务
提供RESTful API供其他服务调用，支持多进程并发
"""

import asyncio
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.responses import JSONResponse
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field
from datetime import datetime

from config import config
from utils.logger import logger
from account_pool.pool_manager import get_pool_manager
from account_pool.database import Account

# 请求响应模型
class AllocateAccountRequest(BaseModel):
    """分配账号请求"""
    session_id: Optional[str] = Field(None, description="会话ID，如果不提供会自动生成")
    count: Optional[int] = Field(1, description="需要分配的账号数量", ge=1, le=10)

class ReleaseAccountRequest(BaseModel):
    """释放账号请求"""
    session_id: str = Field(..., description="要释放的会话ID")

class RefreshTokenRequest(BaseModel):
    """刷新Token请求"""
    email: Optional[str] = Field(None, description="指定账号邮箱")
    force: Optional[bool] = Field(False, description="是否强制刷新（忽略时间限制）")

class ManualReplenishRequest(BaseModel):
    """手动补充账号请求"""
    count: Optional[int] = Field(None, description="补充数量，默认使用配置值")

class AccountInfo(BaseModel):
    """账号信息响应"""
    email: str
    local_id: str  # Warp UID
    id_token: str
    refresh_token: str
    status: str
    created_at: Optional[str]
    last_used: Optional[str]
    last_refresh_time: Optional[str]
    use_count: int
    session_id: Optional[str]

class AllocateAccountResponse(BaseModel):
    """分配账号响应"""
    success: bool
    session_id: str
    accounts: List[AccountInfo]
    message: Optional[str] = None

class PoolStatusResponse(BaseModel):
    """账号池状态响应"""
    pool_stats: Dict[str, int]
    active_sessions: int
    running: bool
    min_pool_size: int
    accounts_per_request: int
    health: str
    timestamp: str

# 全局账号池管理器
pool_manager = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global pool_manager
    
    # 启动时初始化
    logger.info("🚀 启动账号池服务...")
    
    try:
        pool_manager = get_pool_manager()
        await pool_manager.start()
        logger.success("✅ 账号池服务启动完成")
    except Exception as e:
        logger.error(f"❌ 服务启动失败: {e}")
        raise
    
    yield
    
    # 关闭时清理
    logger.info("🛑 关闭账号池服务...")
    try:
        if pool_manager:
            await pool_manager.stop()
        logger.info("✅ 账号池服务已关闭")
    except Exception as e:
        logger.error(f"❌ 服务关闭时出错: {e}")

# 创建FastAPI应用
app = FastAPI(
    title="账号池服务",
    description="独立的Warp账号池管理服务，提供RESTful API接口",
    version="1.0.0",
    lifespan=lifespan
)

# API路由

@app.get("/health")
async def health_check():
    """健康检查"""
    if not pool_manager or not pool_manager._running:
        raise HTTPException(status_code=503, detail="服务不可用")
    
    status = await pool_manager.get_pool_status()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "pool_health": status["health"]
    }

@app.post("/api/accounts/allocate", response_model=AllocateAccountResponse)
async def allocate_accounts(request: AllocateAccountRequest):
    """分配账号给请求"""
    if not pool_manager or not pool_manager._running:
        raise HTTPException(status_code=503, detail="服务不可用")
    
    try:
        # 分配账号
        accounts = await pool_manager.allocate_accounts_for_request(
            request_id=request.session_id
        )
        
        if not accounts:
            return AllocateAccountResponse(
                success=False,
                session_id=request.session_id or "",
                accounts=[],
                message="无法分配账号，账号池可能不足"
            )
        
        # 转换为响应格式
        account_list = []
        for acc in accounts:
            account_list.append(AccountInfo(
                email=acc.email,
                local_id=acc.local_id,
                id_token=acc.id_token,
                refresh_token=acc.refresh_token,
                status=acc.status,
                created_at=acc.created_at.isoformat() if acc.created_at else None,
                last_used=acc.last_used.isoformat() if acc.last_used else None,
                last_refresh_time=acc.last_refresh_time.isoformat() if acc.last_refresh_time else None,
                use_count=acc.use_count,
                session_id=acc.session_id
            ))
        
        # 获取实际的session_id（可能是自动生成的）
        actual_session_id = accounts[0].session_id if accounts else request.session_id
        
        return AllocateAccountResponse(
            success=True,
            session_id=actual_session_id,
            accounts=account_list,
            message=f"成功分配 {len(accounts)} 个账号"
        )
        
    except Exception as e:
        logger.error(f"分配账号失败: {e}")
        raise HTTPException(status_code=500, detail=f"分配账号失败: {str(e)}")

@app.post("/api/accounts/release")
async def release_accounts(request: ReleaseAccountRequest):
    """释放会话的账号"""
    if not pool_manager or not pool_manager._running:
        raise HTTPException(status_code=503, detail="服务不可用")
    
    try:
        success = await pool_manager.release_accounts_for_request(request.session_id)
        
        if success:
            return {
                "success": True,
                "message": f"成功释放会话 {request.session_id} 的账号"
            }
        else:
            return {
                "success": False,
                "message": f"释放会话 {request.session_id} 失败"
            }
            
    except Exception as e:
        logger.error(f"释放账号失败: {e}")
        raise HTTPException(status_code=500, detail=f"释放账号失败: {str(e)}")

@app.get("/api/accounts/status", response_model=PoolStatusResponse)
async def get_pool_status():
    """获取账号池状态"""
    if not pool_manager:
        raise HTTPException(status_code=503, detail="服务不可用")
    
    try:
        status = await pool_manager.get_pool_status()
        
        return PoolStatusResponse(
            pool_stats=status["pool_stats"],
            active_sessions=status["active_sessions"],
            running=status["running"],
            min_pool_size=status["min_pool_size"],
            accounts_per_request=status["accounts_per_request"],
            health=status["health"],
            timestamp=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"获取状态失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取状态失败: {str(e)}")

@app.post("/api/accounts/refresh-tokens")
async def refresh_tokens(request: RefreshTokenRequest):
    """刷新账号Token"""
    if not pool_manager or not pool_manager._running:
        raise HTTPException(status_code=503, detail="服务不可用")
    
    try:
        result = await pool_manager.refresh_account_tokens_manually(
            email=request.email,
            force=request.force
        )
        
        return {
            "success": result["success_count"] > 0,
            "result": result
        }
        
    except Exception as e:
        logger.error(f"刷新Token失败: {e}")
        raise HTTPException(status_code=500, detail=f"刷新Token失败: {str(e)}")

@app.post("/api/accounts/replenish")
async def manual_replenish(request: ManualReplenishRequest):
    """手动补充账号"""
    if not pool_manager or not pool_manager._running:
        raise HTTPException(status_code=503, detail="服务不可用")
    
    try:
        available_count = await pool_manager.manual_replenish(request.count)
        
        return {
            "success": True,
            "message": f"补充操作完成",
            "available_count": available_count
        }
        
    except Exception as e:
        logger.error(f"补充账号失败: {e}")
        raise HTTPException(status_code=500, detail=f"补充账号失败: {str(e)}")

@app.post("/api/pool/refresh")
async def refresh_pool():
    """刷新整个账号池"""
    if not pool_manager or not pool_manager._running:
        raise HTTPException(status_code=503, detail="服务不可用")
    
    try:
        success = await pool_manager.refresh_pool()
        
        return {
            "success": success,
            "message": "账号池刷新完成" if success else "账号池刷新失败"
        }
        
    except Exception as e:
        logger.error(f"刷新账号池失败: {e}")
        raise HTTPException(status_code=500, detail=f"刷新账号池失败: {str(e)}")

@app.get("/api/accounts/{email}")
async def get_account_info(email: str):
    """获取指定账号信息"""
    if not pool_manager:
        raise HTTPException(status_code=503, detail="服务不可用")
    
    try:
        account = pool_manager.db.get_account_by_email(email)
        
        if not account:
            raise HTTPException(status_code=404, detail=f"账号 {email} 不存在")
        
        return AccountInfo(
            email=account.email,
            local_id=account.local_id,
            id_token=account.id_token,
            refresh_token=account.refresh_token,
            status=account.status,
            created_at=account.created_at.isoformat() if account.created_at else None,
            last_used=account.last_used.isoformat() if account.last_used else None,
            last_refresh_time=account.last_refresh_time.isoformat() if account.last_refresh_time else None,
            use_count=account.use_count,
            session_id=account.session_id
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取账号信息失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取账号信息失败: {str(e)}")

@app.delete("/api/accounts/{email}")
async def delete_account(email: str):
    """删除指定账号（标记为失效）"""
    if not pool_manager:
        raise HTTPException(status_code=503, detail="服务不可用")
    
    try:
        logger.warning(f"收到删除账号请求: {email}")
        
        # 检查账号是否存在
        account = pool_manager.db.get_account_by_email(email)
        if not account:
            raise HTTPException(status_code=404, detail=f"账号 {email} 不存在")
        
        # 标记为过期/失效
        success = pool_manager.db.mark_account_as_expired(email)
        
        if success:
            logger.info(f"✅ 成功删除账号: {email}")
            return {
                "success": True,
                "message": f"账号 {email} 已标记为失效"
            }
        else:
            logger.error(f"❌ 删除账号失败: {email}")
            return {
                "success": False,
                "message": f"删除账号 {email} 失败"
            }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除账号异常: {e}")
        raise HTTPException(status_code=500, detail=f"删除账号失败: {str(e)}")

# 错误处理
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTP异常处理"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "timestamp": datetime.now().isoformat()
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """通用异常处理"""
    logger.error(f"未处理的异常: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "内部服务器错误",
            "detail": str(exc),
            "timestamp": datetime.now().isoformat()
        }
    )

def main():
    """主函数"""
    logger.info(f"账号池服务启动配置:")
    logger.info(f"  主机: {config.POOL_SERVICE_HOST}")
    logger.info(f"  端口: {config.POOL_SERVICE_PORT}")
    logger.info(f"  最小池大小: {config.MIN_POOL_SIZE}")
    logger.info(f"  最大池大小: {config.MAX_POOL_SIZE}")
    logger.info(f"  每请求账号数: {config.ACCOUNTS_PER_REQUEST}")
    
    uvicorn.run(
        "main:app",
        host=config.POOL_SERVICE_HOST,
        port=config.POOL_SERVICE_PORT,
        reload=False,
        log_level="info"
    )

if __name__ == "__main__":
    main()