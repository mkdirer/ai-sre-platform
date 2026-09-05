#!/usr/bin/env bash
# Post-deploy smoke for the dev cluster release (deploy.yml).
# Bounded and honest: readiness on every in-cluster service, one checkout
# through the gateway, then stop. Requires: kubectl context already set to
# the dev cluster (done by the workflow), NAMESPACE + RELEASE_NAME exported.
set -euo pipefail

NAMESPACE="${NAMESPACE:?set NAMESPACE to the release namespace}"
RELEASE_NAME="${RELEASE_NAME:?set RELEASE_NAME to the helm release name}"
TIMEOUT="${SMOKE_TIMEOUT_SECS:-180}"
POLL_SECS=5

echo "waiting for rollout: release=$RELEASE_NAME namespace=$NAMESPACE"
kubectl -n "$NAMESPACE" rollout status "deployment/gateway" --timeout="${TIMEOUT}s"
kubectl -n "$NAMESPACE" rollout status "deployment/incident-api" --timeout="${TIMEOUT}s"

echo "starting port-forwards (gateway 8001, incident-api 8006)"
kubectl -n "$NAMESPACE" port-forward "svc/gateway" 8001:8000 >/tmp/pf-gateway.log 2>&1 &
PF_GATEWAY=$!
kubectl -n "$NAMESPACE" port-forward "svc/incident-api" 8006:8000 >/tmp/pf-api.log 2>&1 &
PF_API=$!
trap 'kill $PF_GATEWAY $PF_API 2>/dev/null || true' EXIT
sleep "$POLL_SECS"

echo "port-forwards healthy"
kill -0 "$PF_GATEWAY" 2>/dev/null || { echo "gateway port-forward died"; cat /tmp/pf-gateway.log; exit 1; }
kill -0 "$PF_API" 2>/dev/null || { echo "incident-api port-forward died"; cat /tmp/pf-api.log; exit 1; }

echo "readiness probes"
curl -sf --max-time 10 http://127.0.0.1:8001/health/ready > /dev/null
curl -sf --max-time 10 http://127.0.0.1:8006/health/ready > /dev/null

echo "single bounded checkout"
KEY="post-deploy-$(date +%s)"
curl -sf --max-time 20 -X POST http://127.0.0.1:8001/checkout \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $KEY" \
  -d '{"customer_id":"post-deploy","sku":"widget-001","quantity":1}' > /dev/null

echo "POST-DEPLOY SMOKE OK (namespace=$NAMESPACE release=$RELEASE_NAME)"
