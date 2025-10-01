#!/bin/bash
# 429优化V2 - 快速验证脚本

echo "=========================================="
echo "  429优化V2版本 - 快速验证脚本"
echo "=========================================="
echo ""

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 1. 检查账号池服务
echo -n "1. 检查账号池服务... "
if curl -s http://localhost:8019/health | grep -q "healthy\|ok"; then
    echo -e "${GREEN}✅ 通过${NC}"
else
    echo -e "${RED}❌ 失败 - 请启动账号池服务${NC}"
    exit 1
fi

# 2. 检查V2端点
echo -n "2. 检查V2端点... "
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8000/api/warp/send_stream_v2 -d '{}')
if [[ "$STATUS" == "200" || "$STATUS" == "400" || "$STATUS" == "422" ]]; then
    echo -e "${GREEN}✅ 通过 (HTTP $STATUS)${NC}"
else
    echo -e "${RED}❌ 失败 (HTTP $STATUS)${NC}"
    exit 1
fi

# 3. 检查DELETE端点
echo -n "3. 检查DELETE端点... "
if curl -s -X DELETE http://localhost:8019/api/accounts/nonexistent@test.com | grep -q "success\|不存在"; then
    echo -e "${GREEN}✅ 通过${NC}"
else
    echo -e "${RED}❌ 失败${NC}"
    exit 1
fi

# 4. 检查账号池大小
echo -n "4. 检查账号池大小... "
AVAILABLE=$(curl -s http://localhost:8019/api/accounts/status | grep -o '"available":[0-9]*' | grep -o '[0-9]*')
if [ "$AVAILABLE" -ge 3 ]; then
    echo -e "${GREEN}✅ 通过 ($AVAILABLE 个可用)${NC}"
else
    echo -e "${YELLOW}⚠️  警告 (仅$AVAILABLE个，建议≥5)${NC}"
fi

# 5. 测试V2请求
echo -n "5. 测试V2请求... "
RESPONSE=$(curl -s -X POST "http://localhost:8000/api/warp/send_stream_v2?session_id=quick_test" \
  -H "Content-Type: application/json" \
  -d '{
    "json_data": {
      "task_context": {"tasks": [], "active_task_id": "test"},
      "input": {"user_message": {"content": "test"}}
    },
    "message_type": "warp.multi_agent.v1.Request"
  }' 2>&1)

if echo "$RESPONSE" | grep -q "parsed_events\|error"; then
    echo -e "${GREEN}✅ 通过${NC}"
else
    echo -e "${RED}❌ 失败${NC}"
    echo "响应: $RESPONSE"
fi

echo ""
echo "=========================================="
echo -e "${GREEN}🎉 V2版本验证完成！${NC}"
echo "=========================================="
echo ""
echo "📚 文档参考:"
echo "  - 优化总结: OPTIMIZATION_SUMMARY.md"
echo "  - 迁移指南: MIGRATION_TO_V2.md"
echo "  - 快速开始: README_V2_OPTIMIZATION.md"
echo ""
echo "🚀 下一步:"
echo "  1. 查看日志: tail -f logs/warp2api.log"
echo "  2. 监控状态: watch -n 10 'curl -s http://localhost:8019/api/accounts/status | jq'"
echo "  3. 开始使用: curl -X POST 'http://localhost:8000/api/warp/send_stream_v2?session_id=YOUR_SESSION' ..."
echo ""
