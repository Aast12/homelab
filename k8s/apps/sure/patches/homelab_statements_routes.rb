# frozen_string_literal: true

# Routes for Api::V1::HomelabStatementsController (homelab_statements_controller.rb).
# Prepended, so they answer before any statement routes a newer Sure adds.
Rails.application.routes.prepend do
  namespace :api do
    namespace :v1 do
      resources :account_statements, only: %i[show create update], controller: "homelab_statements"
    end
  end
end
