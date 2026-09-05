# Partial GCS backend. The bucket and prefix are supplied at init time so
# no personal bucket or project is hard-coded anywhere in this stack:
#
#   terraform init \
#     -backend-config="bucket=${TF_STATE_BUCKET}" \
#     -backend-config="prefix=ai-sre-platform/dev"
#
# See docs/runbooks/gcp-bootstrap.md for the one-time bucket setup.
terraform {
  backend "gcs" {}
}
