{{/*
Shared helpers for ai-sre-platform. All names are DNS-1123 safe because
component keys in values.yaml already are (gateway, order-service, ...).
*/}}
{{- define "ai-sre.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ai-sre.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "ai-sre.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ai-sre.labels" -}}
app.kubernetes.io/name: {{ include "ai-sre.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ai-sre.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ai-sre.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "ai-sre.componentLabels" -}}
{{ include "ai-sre.labels" . }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "ai-sre.componentSelector" -}}
{{ include "ai-sre.selectorLabels" . }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "ai-sre.appImage" -}}
{{- $registry := .Values.global.imageRegistry -}}
{{- $repo := .Values.global.appImage.repository -}}
{{- $tag := .Values.global.appImage.tag -}}
{{- if $registry -}}{{ printf "%s/%s:%s" $registry $repo $tag }}{{- else -}}{{ printf "%s:%s" $repo $tag }}{{- end -}}
{{- end -}}

{{- define "ai-sre.frontendImage" -}}
{{- $registry := .Values.global.imageRegistry -}}
{{- $repo := .Values.global.frontendImage.repository -}}
{{- $tag := .Values.global.frontendImage.tag -}}
{{- if $registry -}}{{ printf "%s/%s:%s" $registry $repo $tag }}{{- else -}}{{ printf "%s:%s" $repo $tag }}{{- end -}}
{{- end -}}

{{/* Non-secret shared env for every Python service (mirrors compose anchors). */}}
{{- define "ai-sre.appEnv" -}}
- name: ENVIRONMENT
  value: {{ .Values.global.environment | quote }}
- name: LOG_LEVEL
  value: {{ .Values.appDefaults.logLevel | quote }}
- name: SERVICE_VERSION
  value: {{ .Values.global.serviceVersion | quote }}
- name: TELEMETRY_ENABLED
  value: {{ .Values.appDefaults.telemetryEnabled | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.appDefaults.otelEndpoint | quote }}
- name: OTEL_EXPORT_TIMEOUT_SECONDS
  value: {{ .Values.appDefaults.otelExportTimeoutSeconds | quote }}
- name: OTEL_BATCH_SCHEDULE_DELAY_MILLISECONDS
  value: {{ .Values.appDefaults.otelBatchScheduleDelayMilliseconds | quote }}
- name: OUTBOUND_HTTP_TIMEOUT_SECONDS
  value: {{ .Values.appDefaults.outboundTimeoutSeconds | quote }}
- name: OUTBOUND_HTTP_MAX_ATTEMPTS
  value: {{ .Values.appDefaults.outboundMaxAttempts | quote }}
- name: OUTBOUND_HTTP_RETRY_BACKOFF_SECONDS
  value: {{ .Values.appDefaults.outboundRetryBackoffSeconds | quote }}
{{- end -}}

{{- define "ai-sre.dbEnv" -}}
- name: POSTGRES_HOST
  value: {{ .Values.database.host | quote }}
- name: POSTGRES_PORT
  value: {{ .Values.database.port | quote }}
- name: POSTGRES_DB
  value: {{ .Values.database.name | quote }}
- name: POSTGRES_USER
  value: {{ .Values.database.user | quote }}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "ai-sre.fullname" . }}-demo-secrets
      key: postgres-password
- name: DATABASE_CONNECT_TIMEOUT_SECONDS
  value: {{ .Values.database.connectTimeoutSeconds | quote }}
{{- end -}}

{{- define "ai-sre.queueEnv" -}}
- name: CELERY_BROKER_URL
  value: {{ .Values.queue.brokerUrl | quote }}
- name: CELERY_RESULT_BACKEND_URL
  value: {{ .Values.queue.resultBackendUrl | quote }}
- name: QUEUE_PUBLISH_TIMEOUT_SECONDS
  value: {{ .Values.queue.publishTimeoutSeconds | quote }}
- name: INVESTIGATION_MAX_ATTEMPTS
  value: {{ .Values.queue.maxAttempts | quote }}
- name: INVESTIGATION_RETRY_BASE_SECONDS
  value: {{ .Values.queue.retryBaseSeconds | quote }}
- name: INVESTIGATION_RETRY_MAX_SECONDS
  value: {{ .Values.queue.retryMaxSeconds | quote }}
- name: INVESTIGATION_JOB_LEASE_SECONDS
  value: {{ .Values.queue.leaseSeconds | quote }}
- name: CELERY_VISIBILITY_TIMEOUT_SECONDS
  value: {{ .Values.queue.visibilityTimeoutSeconds | quote }}
{{- end -}}

{{/* Standard /health probes for uvicorn services (mirrors compose healthcheck). */}}
{{- define "ai-sre.httpProbes" -}}
livenessProbe:
  httpGet:
    path: /health/live
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 5
  timeoutSeconds: 3
  failureThreshold: 6
readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 5
  timeoutSeconds: 3
  failureThreshold: 6
{{- end -}}

{{/* Hardened pod+container context for the non-root Python image (uid 10001). */}}
{{- define "ai-sre.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "ai-sre.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: 10001
capabilities:
  drop:
    - ALL
seccompProfile:
  type: RuntimeDefault
{{- end -}}
