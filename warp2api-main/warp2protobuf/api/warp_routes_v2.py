#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Warp API路由 V2 - 优化版429处理
使用账号池智能重试机制
"""

import os
import re
import httpx
import base64
from typing import Any, Dict
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..core.logging import logger
from ..core.protobuf_utils import protobuf_to_dict, dict_to_protobuf_bytes
from ..core.pool_auth_v2 import get_pool_manager_v2, handle_429_with_retry
from ..core.schema_sanitizer import sanitize_mcp_input_schema_in_packet
from ..config.settings import CLIENT_VERSION, OS_CATEGORY, OS_NAME, OS_VERSION, WARP_URL as CONFIG_WARP_URL
from .protobuf_routes import EncodeRequest, _encode_smd_inplace, _decode_smd_inplace

router_v2 = APIRouter()


def _parse_payload_bytes(data_str: str):
    """解析SSE数据负载"""
    s = re.sub(r"\s+", "", data_str or "")
    if not s:
        return None
    if re.fullmatch(r"[0-9a-fA-F]+", s or ""):
        try:
            return bytes.fromhex(s)
        except Exception:
            pass
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    try:
        return base64.urlsafe_b64decode(s + pad)
    except Exception:
        try:
            return base64.b64decode(s + pad)
        except Exception:
            return None


@router_v2.post("/api/warp/send_stream_sse_v2")
async def send_to_warp_api_stream_sse_v2(
    request: EncodeRequest,
    session_id: str = Query(None, description="会话ID用于账号绑定和429重试")
):
    """
    发送Warp请求并流式返回SSE事件（V2版本 - 支持智能429重试）
    
    优化：
    - 429错误时从账号池获取新账号并重试（最多3次）
    - 自动删除失败账号
    - 触发账号池补充机制
    """
    from fastapi.responses import StreamingResponse
    
    try:
        # 1. 准备数据
        actual_data = request.get_data()
        if not actual_data:
            raise HTTPException(400, "数据包不能为空")
        
        # 清理schema
        wrapped = {"json_data": actual_data}
        wrapped = sanitize_mcp_input_schema_in_packet(wrapped)
        actual_data = wrapped.get("json_data", actual_data)
        
        # 编码server_message_data
        actual_data = _encode_smd_inplace(actual_data)
        
        # 2. Protobuf编码
        protobuf_bytes = dict_to_protobuf_bytes(actual_data, request.message_type)
        logger.info(f"✅ 编码完成: {len(protobuf_bytes)} 字节")
        
        # 3. 定义执行请求的函数
        async def execute_warp_request(jwt: str):
            """使用指定JWT执行Warp请求"""
            warp_url = CONFIG_WARP_URL
            
            headers = {
                "accept": "text/event-stream",
                "content-type": "application/x-protobuf",
                "x-warp-client-version": CLIENT_VERSION,
                "x-warp-os-category": OS_CATEGORY,
                "x-warp-os-name": OS_NAME,
                "x-warp-os-version": OS_VERSION,
                "authorization": f"Bearer {jwt}",
                "content-length": str(len(protobuf_bytes)),
            }
            
            verify_opt = True
            if os.getenv("WARP_INSECURE_TLS", "").lower() in ("1", "true", "yes"):
                verify_opt = False
            
            async with httpx.AsyncClient(http2=True, timeout=httpx.Timeout(60.0), verify=verify_opt, trust_env=True) as client:
                async with client.stream("POST", warp_url, headers=headers, content=protobuf_bytes) as response:
                    if response.status_code != 200:
                        error_text = await response.aread()
                        error_content = error_text.decode("utf-8") if error_text else "No error content"
                        
                        # 抛出异常，包含状态码和错误内容
                        raise RuntimeError(f"HTTP {response.status_code}: {error_content}")
                    
                    # 成功，收集所有事件
                    events = []
                    current_data = ""
                    
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            payload = line[5:].strip()
                            if not payload:
                                continue
                            if payload == "[DONE]":
                                break
                            current_data += payload
                            continue
                        
                        if (line.strip() == "") and current_data:
                            raw_bytes = _parse_payload_bytes(current_data)
                            current_data = ""
                            if raw_bytes is None:
                                continue
                            
                            try:
                                event_data = protobuf_to_dict(raw_bytes, "warp.multi_agent.v1.ResponseEvent")
                                events.append(event_data)
                            except Exception:
                                continue
                    
                    return events
        
        # 4. 智能429重试流式包装
        async def stream_with_retry():
            """带429重试的流式生成器"""
            try:
                # 首先尝试获取账号（如果有session_id则绑定）
                manager = get_pool_manager_v2()
                jwt, account_info = await manager.acquire_account_for_session(session_id)
                email = account_info.get("email")
                
                logger.info(f"🔑 使用账号: {email}")
                
                # 执行请求
                try:
                    events = await execute_warp_request(jwt)
                    
                    # 成功，流式返回所有事件
                    for event in events:
                        yield f"data: {str(event)}\n\n"
                    yield "data: [DONE]\n\n"
                    
                except RuntimeError as e:
                    error_msg = str(e)
                    
                    # 检查是否为429错误
                    if "429" in error_msg and ("No remaining quota" in error_msg or "No AI requests remaining" in error_msg):
                        logger.warning(f"🔄 检测到429配额用尽，启动智能重试...")
                        
                        # 使用智能重试处理
                        events = await handle_429_with_retry(
                            session_id=session_id,
                            error_content=error_msg,
                            execute_request_func=execute_warp_request
                        )
                        
                        # 重试成功，返回事件
                        for event in events:
                            yield f"data: {str(event)}\n\n"
                        yield "data: [DONE]\n\n"
                    else:
                        # 其他错误
                        raise
                        
            except Exception as e:
                logger.error(f"❌ 流式请求失败: {e}")
                yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"
                yield "data: [DONE]\n\n"
        
        return StreamingResponse(stream_with_retry(), media_type="text/event-stream")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 处理请求失败: {e}")
        raise HTTPException(500, f"处理请求失败: {str(e)}")


@router_v2.post("/api/warp/send_stream_v2")
async def send_to_warp_api_stream_v2(
    request: EncodeRequest,
    session_id: str = Query(None, description="会话ID用于账号绑定和429重试")
):
    """
    发送Warp请求并返回完整结果（V2版本 - 支持智能429重试）
    
    非流式版本，返回完整的解析结果
    """
    try:
        # 1. 准备数据
        actual_data = request.get_data()
        if not actual_data:
            raise HTTPException(400, "数据包不能为空")
        
        wrapped = {"json_data": actual_data}
        wrapped = sanitize_mcp_input_schema_in_packet(wrapped)
        actual_data = wrapped.get("json_data", actual_data)
        actual_data = _encode_smd_inplace(actual_data)
        
        # 2. Protobuf编码
        protobuf_bytes = dict_to_protobuf_bytes(actual_data, request.message_type)
        
        # 3. 定义执行函数
        async def execute_warp_request(jwt: str):
            warp_url = CONFIG_WARP_URL
            
            headers = {
                "accept": "text/event-stream",
                "content-type": "application/x-protobuf",
                "x-warp-client-version": CLIENT_VERSION,
                "x-warp-os-category": OS_CATEGORY,
                "x-warp-os-name": OS_NAME,
                "x-warp-os-version": OS_VERSION,
                "authorization": f"Bearer {jwt}",
                "content-length": str(len(protobuf_bytes)),
            }
            
            verify_opt = True
            if os.getenv("WARP_INSECURE_TLS", "").lower() in ("1", "true", "yes"):
                verify_opt = False
            
            async with httpx.AsyncClient(http2=True, timeout=httpx.Timeout(60.0), verify=verify_opt, trust_env=True) as client:
                async with client.stream("POST", warp_url, headers=headers, content=protobuf_bytes) as response:
                    if response.status_code != 200:
                        error_text = await response.aread()
                        error_content = error_text.decode("utf-8") if error_text else ""
                        raise RuntimeError(f"HTTP {response.status_code}: {error_content}")
                    
                    # 收集所有事件
                    events = []
                    current_data = ""
                    
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            payload = line[5:].strip()
                            if not payload or payload == "[DONE]":
                                continue
                            current_data += payload
                            continue
                        
                        if (line.strip() == "") and current_data:
                            raw_bytes = _parse_payload_bytes(current_data)
                            current_data = ""
                            if raw_bytes:
                                try:
                                    event_data = protobuf_to_dict(raw_bytes, "warp.multi_agent.v1.ResponseEvent")
                                    events.append(event_data)
                                except:
                                    pass
                    
                    return {"parsed_events": events}
        
        # 4. 执行请求（带429重试）
        manager = get_pool_manager_v2()
        jwt, account_info = await manager.acquire_account_for_session(session_id)
        
        try:
            result = await execute_warp_request(jwt)
            return result
        except RuntimeError as e:
            error_msg = str(e)
            
            # 429重试
            if "429" in error_msg and ("No remaining quota" in error_msg or "No AI requests remaining" in error_msg):
                result = await handle_429_with_retry(
                    session_id=session_id,
                    error_content=error_msg,
                    execute_request_func=execute_warp_request
                )
                return {"parsed_events": result}
            else:
                raise HTTPException(500, error_msg)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理请求失败: {e}")
        raise HTTPException(500, f"处理请求失败: {str(e)}")
