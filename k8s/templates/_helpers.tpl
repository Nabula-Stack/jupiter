{{/* Expand the name of the chart. */}}
{{- define "nebula.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Create a default fully qualified app name. */}}
{{- define "nebula.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Namespace for all resources. */}}
{{- define "nebula.namespace" -}}
{{- if .Values.namespaceOverride -}}
{{- .Values.namespaceOverride -}}
{{- else -}}
{{- .Values.namespace.name -}}
{{- end -}}
{{- end -}}

{{/* Labels common across all resources. */}}
{{- define "nebula.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "nebula.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Selector labels used by workloads and services. */}}
{{- define "nebula.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nebula.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Service account name. */}}
{{- define "nebula.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "nebula.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Secret name in use by app containers. */}}
{{- define "nebula.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else if .Values.secrets.name -}}
{{- .Values.secrets.name -}}
{{- else -}}
{{- printf "%s-app-secret" (include "nebula.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* Database service host used by env vars. */}}
{{- define "nebula.dbHost" -}}
{{- if .Values.database.enabled -}}
{{- printf "%s-postgresql" (include "nebula.fullname" .) -}}
{{- else -}}
{{- required "externalDatabase.host is required when database.enabled=false" .Values.externalDatabase.host -}}
{{- end -}}
{{- end -}}

{{/* Redis service host used by env vars. */}}
{{- define "nebula.redisHost" -}}
{{- if .Values.redis.enabled -}}
{{- printf "%s-redis" (include "nebula.fullname" .) -}}
{{- else -}}
{{- required "externalRedis.host is required when redis.enabled=false" .Values.externalRedis.host -}}
{{- end -}}
{{- end -}}
