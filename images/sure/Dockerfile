# Sure with the homelab's statement API (homelab_statements_*.rb), which v0.7.6 lacks.
# Built by .github/workflows/sure-image.yml as ghcr.io/aast12/homelab-sure:<TAG>.
#
# To move to another Sure release: change FROM, bump TAG, and set the same tag in
# k8s/apps/sure/40-app.yaml (both deployments).
FROM ghcr.io/we-promise/sure:0.7.6-hotfix.1@sha256:002cde609f3af4b69ba2564c2482f3996e1fe29092dd169962b9b6ed943fcb10

COPY homelab_statements_controller.rb /rails/app/controllers/api/v1/
COPY homelab_statements_routes.rb /rails/config/initializers/
