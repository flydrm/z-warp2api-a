#!/bin/bash
# 429优化功能完整测试脚本

set -e

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=========================================="
echo "  429优化功能完整测试"
echo -e "==========================================${NC}"
echo ""

# 加载环境变量
if [ -f "/workspace/config/test.env" ]; then
    set -a
    source /workspace/config/test.env
    set +a
    echo -e "${GREEN}✅ 环境变量已加载${NC}"
fi

# 测试1: 账号池服务健康检查
echo ""
echo -e "${BLUE}【测试1】账号池服务健康检查${NC}"
echo "----------------------------------------"
HEALTH_RESP=$(curl -s http://localhost:8019/health)
if echo "$HEALTH_RESP" | grep -q "healthy\|ok"; then
    echo -e "${GREEN}✅ 通过${NC}"
    echo "$HEALTH_RESP" | python3 -m json.tool 2>/dev/null || echo "$HEALTH_RESP"
else
    echo -e "${RED}❌ 失败 - 服务未运行${NC}"
    echo "响应: $HEALTH_RESP"
    exit 1
fi

# 测试2: 获取账号池状态
echo ""
echo -e "${BLUE}【测试2】账号池状态查询${NC}"
echo "----------------------------------------"
STATUS_RESP=$(curl -s http://localhost:8019/api/accounts/status)
echo "$STATUS_RESP" | python3 -m json.tool 2>/dev/null || echo "$STATUS_RESP"

AVAILABLE=$(echo "$STATUS_RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('pool_stats', {}).get('available', 0))" 2>/dev/null || echo "0")
echo -e "\n${YELLOW}可用账号数: $AVAILABLE${NC}"

if [ "$AVAILABLE" -lt 1 ]; then
    echo -e "${RED}⚠️  警告: 账号池中无可用账号，部分测试将跳过${NC}"
fi

# 测试3: DELETE API端点
echo ""
echo -e "${BLUE}【测试3】DELETE账号API${NC}"
echo "----------------------------------------"
DEL_RESP=$(curl -s -X DELETE http://localhost:8019/api/accounts/nonexist@test.com)
echo "$DEL_RESP" | python3 -m json.tool 2>/dev/null || echo "$DEL_RESP"

if echo "$DEL_RESP" | grep -q "success\|不存在\|404"; then
    echo -e "${GREEN}✅ DELETE端点正常工作${NC}"
else
    echo -e "${RED}❌ DELETE端点异常${NC}"
fi

# 测试4: 账号分配
if [ "$AVAILABLE" -ge 1 ]; then
    echo ""
    echo -e "${BLUE}【测试4】账号分配功能${NC}"
    echo "----------------------------------------"
    ALLOC_RESP=$(curl -s -X POST http://localhost:8019/api/accounts/allocate \
      -H "Content-Type: application/json" \
      -d '{"count": 1}')
    
    echo "$ALLOC_RESP" | python3 -m json.tool 2>/dev/null || echo "$ALLOC_RESP"
    
    if echo "$ALLOC_RESP" | grep -q '"success": *true'; then
        echo -e "${GREEN}✅ 账号分配成功${NC}"
        
        # 提取信息
        SESSION_ID=$(echo "$ALLOC_RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('session_id', ''))" 2>/dev/null)
        ACCOUNT_EMAIL=$(echo "$ALLOC_RESP" | python3 -c "import sys, json; accs=json.load(sys.stdin).get('accounts',[]); print(accs[0]['email'] if accs else '')" 2>/dev/null)
        
        echo "   Session ID: $SESSION_ID"
        echo "   Account: $ACCOUNT_EMAIL"
        
        # 测试5: 账号释放
        echo ""
        echo -e "${BLUE}【测试5】账号释放功能${NC}"
        echo "----------------------------------------"
        if [ -n "$SESSION_ID" ]; then
            REL_RESP=$(curl -s -X POST http://localhost:8019/api/accounts/release \
              -H "Content-Type: application/json" \
              -d "{\"session_id\": \"$SESSION_ID\"}")
            
            echo "$REL_RESP" | python3 -m json.tool 2>/dev/null || echo "$REL_RESP"
            
            if echo "$REL_RESP" | grep -q '"success": *true'; then
                echo -e "${GREEN}✅ 账号释放成功${NC}"
            else
                echo -e "${RED}❌ 账号释放失败${NC}"
            fi
        else
            echo -e "${YELLOW}⚠️  无session_id，跳过${NC}"
        fi
    else
        echo -e "${RED}❌ 账号分配失败${NC}"
    fi
else
    echo ""
    echo -e "${YELLOW}⚠️  账号池无可用账号，跳过测试4和5${NC}"
fi

# 测试6: 账号补充API
echo ""
echo -e "${BLUE}【测试6】账号补充API${NC}"
echo "----------------------------------------"
REP_RESP=$(curl -s -X POST http://localhost:8019/api/accounts/replenish \
  -H "Content-Type: application/json" \
  -d '{"count": 1}')

echo "$REP_RESP" | python3 -m json.tool 2>/dev/null || echo "$REP_RESP"

if echo "$REP_RESP" | grep -q '"success": *true'; then
    echo -e "${GREEN}✅ 补充API调用成功${NC}"
else
    echo -e "${YELLOW}⚠️  补充API响应异常（可能正在后台处理）${NC}"
fi

# 测试7: 检查配置是否生效
echo ""
echo -e "${BLUE}【测试7】429优化配置检查${NC}"
echo "----------------------------------------"
echo "   ENABLE_SMART_429_RETRY: ${ENABLE_SMART_429_RETRY:-未设置}"
echo "   MAX_429_RETRIES: ${MAX_429_RETRIES:-未设置}"
echo "   DELETE_FAILED_ACCOUNTS: ${DELETE_FAILED_ACCOUNTS:-未设置}"
echo "   AUTO_REPLENISH_POOL: ${AUTO_REPLENISH_POOL:-未设置}"
echo "   MIN_POOL_SIZE: ${MIN_POOL_SIZE:-未设置}"
echo "   MAX_POOL_SIZE: ${MAX_POOL_SIZE:-未设置}"

# 总结
echo ""
echo -e "${BLUE}=========================================="
echo "  测试总结"
echo -e "==========================================${NC}"
echo ""
echo -e "${GREEN}✅ 已完成测试:${NC}"
echo "   1. 账号池服务健康检查"
echo "   2. 账号池状态查询"
echo "   3. DELETE API端点"
echo "   4. 账号分配功能（如有可用账号）"
echo "   5. 账号释放功能"
echo "   6. 账号补充API"
echo "   7. 配置验证"
echo ""
echo -e "${YELLOW}⏸️  待测试（需完整系统）:${NC}"
echo "   - 完整的429重试流程"
echo "   - OpenAI兼容API调用"
echo "   - 实际Warp API请求"
echo ""
echo -e "${BLUE}📝 建议:${NC}"
echo "   1. 降低MIN_POOL_SIZE到5-10（当前100太高）"
echo "   2. 修复Warp GraphQL激活422问题"
echo "   3. 等待账号池初始化完成后进行完整测试"
echo ""
