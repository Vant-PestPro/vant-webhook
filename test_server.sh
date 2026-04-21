#!/bin/bash
cd /Users/vant/.openclaw/workspace/projects/vant-phone-agent/webhook
source venv/bin/activate

python3 server.py &
SERVER_PID=$!
sleep 2

echo "=== Health check ==="
curl -s http://localhost:5050/ | python3 -m json.tool

echo ""
echo "=== Daniel calling in ==="
curl -s -X POST http://localhost:5050/webhook \
  -H "Content-Type: application/json" \
  -d '{"message":{"type":"assistant-request","call":{"customer":{"number":"+19544106389"}}}}' \
  | python3 -m json.tool

echo ""
echo "=== Unknown caller ==="
curl -s -X POST http://localhost:5050/webhook \
  -H "Content-Type: application/json" \
  -d '{"message":{"type":"assistant-request","call":{"customer":{"number":"+14075551234"}}}}' \
  | python3 -m json.tool

kill $SERVER_PID 2>/dev/null
echo "Done."
