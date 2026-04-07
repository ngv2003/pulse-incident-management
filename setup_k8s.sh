#!/bin/bash
# setup_k8s.sh — Deploy Pulse demo pods to your LOCAL Rancher Desktop cluster.
# Your IBM Cloud contexts are never touched.
#
# Usage: bash setup_k8s.sh

set -e

echo ""
echo "╔════════════════════════════════════════╗"
echo "║   Pulse — Kubernetes Setup             ║"
echo "╚════════════════════════════════════════╝"
echo ""

# ── 1. Check prerequisites ────────────────────────────────────────────────────
echo "[1/5] Checking prerequisites..."
command -v kubectl >/dev/null || { echo "✗ kubectl not found. Run: brew install kubectl"; exit 1; }

kubectl config get-contexts rancher-desktop >/dev/null 2>&1 || {
    echo "✗ rancher-desktop context not found."
    echo "  Install Rancher Desktop: brew install --cask rancher"
    exit 1
}
echo "  ✓ kubectl found"
echo "  ✓ rancher-desktop context exists"

# ── 2. Switch to Rancher Desktop (local, safe) ────────────────────────────────
echo ""
echo "[2/5] Switching to local Rancher Desktop context..."
echo "  ⚠  Your IBM Cloud contexts will NOT be touched."
echo ""

PREVIOUS_CTX=$(kubectl config current-context 2>/dev/null || echo "none")
kubectl config use-context rancher-desktop
echo "  ✓ Active context: rancher-desktop"
echo "  ℹ  To restore IBM context after demo:"
echo "     kubectl config use-context $PREVIOUS_CTX"

# ── 3. Verify cluster is responding ──────────────────────────────────────────
echo ""
echo "[3/5] Waiting for local cluster..."
for i in $(seq 1 12); do
    if kubectl get nodes --request-timeout=5s >/dev/null 2>&1; then
        echo "  ✓ Cluster ready"
        kubectl get nodes
        break
    fi
    printf "  ... attempt %d/12 (waiting 5s)\n" $i
    sleep 5
    if [ $i -eq 12 ]; then
        echo "  ✗ Rancher Desktop not responding."
        exit 1
    fi
done

# ── 4. Deploy services ────────────────────────────────────────────────────────
echo ""
echo "[4/5] Deploying Pulse mock services..."
kubectl apply -f k8s/deployments.yaml

echo "  Waiting for pods to be ready (up to 2 min)..."
kubectl wait deployment/checkout-svc  --for=condition=available --timeout=120s
kubectl wait deployment/inventory-svc --for=condition=available --timeout=120s
kubectl wait deployment/db-primary    --for=condition=available --timeout=120s

echo ""
echo "  ✓ Pods running:"
kubectl get pods -o wide

# ── 5. Port-forward so Pulse can reach pods ───────────────────────────────────
echo ""
echo "[5/5] Starting port-forwards (background)..."
pkill -f "kubectl port-forward" 2>/dev/null || true
sleep 1

kubectl port-forward deployment/checkout-svc  9101:8080 >/dev/null 2>&1 &
kubectl port-forward deployment/inventory-svc 9102:8080 >/dev/null 2>&1 &
kubectl port-forward deployment/db-primary    9103:8080 >/dev/null 2>&1 &
sleep 2

echo "  ✓ Port-forwards active:"
echo "    checkout-svc  → localhost:9101"
echo "    inventory-svc → localhost:9102"
echo "    db-primary    → localhost:9103"

echo ""
echo "Verifying service health..."
for port in 9101 9102 9103; do
    STATUS=$(curl -s http://localhost:$port/health 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null \
        || echo "unreachable")
    echo "  localhost:$port → $STATUS"
done

echo ""
echo "╔════════════════════════════════════════╗"
echo "║   ✓ Setup complete!                    ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "Next: bash reset.sh  then start 3 terminals"
echo "After demo: kubectl config use-context $PREVIOUS_CTX"
echo ""