#!/bin/bash
# Upload training data and start first training round on the fine-tune lab
# Usage: ./upload_and_train.sh [host] [port]

HOST="${1:-192.168.53.190}"
PORT="${2:-8881}"
BASE_URL="http://${HOST}:${PORT}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Fine-Tune Lab: Upload & Train ==="
echo "Target: ${BASE_URL}"
echo

# Check if service is running
echo "Checking service..."
STATUS=$(curl -s --connect-timeout 5 "${BASE_URL}/" 2>/dev/null)
if [ -z "$STATUS" ]; then
    echo "ERROR: Service not reachable at ${BASE_URL}"
    exit 1
fi
echo "Service: $(echo $STATUS | python3 -m json.tool 2>/dev/null || echo $STATUS)"
echo

# Load training data and send to /train with extra_data
echo "Loading training data..."
TRAINING_DATA=$(cat "${SCRIPT_DIR}/training_data.json")
PAYLOAD=$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
payload = {
    'extra_data': data,
    'epochs': 3,
    'learning_rate': 2e-5,
    'batch_size': 8
}
print(json.dumps(payload))
" <<< "$TRAINING_DATA")

echo "Starting training with $(echo $TRAINING_DATA | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') examples..."
RESULT=$(curl -s -X POST "${BASE_URL}/train" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")
echo "Response: $(echo $RESULT | python3 -m json.tool 2>/dev/null || echo $RESULT)"
echo

# Poll status
echo "Monitoring training progress (Ctrl+C to stop)..."
while true; do
    sleep 10
    STATUS=$(curl -s "${BASE_URL}/status" 2>/dev/null)
    RUNNING=$(echo "$STATUS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('running', False))" 2>/dev/null)
    STAGE=$(echo "$STATUS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('stage', '?'))" 2>/dev/null)
    EPOCH=$(echo "$STATUS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('current_epoch', '?'))" 2>/dev/null)
    LOSS=$(echo "$STATUS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('loss', '?'))" 2>/dev/null)

    echo "[$(date +%H:%M:%S)] stage=$STAGE epoch=$EPOCH loss=$LOSS running=$RUNNING"

    if [ "$RUNNING" = "False" ] || [ "$RUNNING" = "false" ]; then
        ERROR=$(echo "$STATUS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error', 'None'))" 2>/dev/null)
        if [ "$ERROR" != "None" ] && [ -n "$ERROR" ]; then
            echo "TRAINING FAILED: $ERROR"
            exit 1
        fi
        if [ "$STAGE" = "done" ]; then
            echo "TRAINING COMPLETE!"
            break
        fi
        break
    fi
done

echo
echo "=== Running comparison test ==="
COMPARE=$(curl -s -X POST "${BASE_URL}/compare" \
    -H "Content-Type: application/json" \
    -d '{"prompt": "What VLAN should I put IoT devices on and why?", "temperature": 0.7}')
echo "$COMPARE" | python3 -m json.tool 2>/dev/null || echo "$COMPARE"
