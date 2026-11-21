# frozen_string_literal: true

# Statement API for Sure versions that only have the web upload (v0.7.6).
# Built into the homelab Sure image (images/sure/Dockerfile).
#
#   POST  /api/v1/account_statements      file + account_id: stored and read by
#                                         Sure's own upload code, as the web form does
#   GET   /api/v1/account_statements/:id
#   PATCH /api/v1/account_statements/:id  statement[period_start_on, period_end_on,
#                                         opening_balance, closing_balance, currency]
#
# Create and show answer like Sure 0.7.7's own API ({data, meta: {duplicate}}),
# so a client works against either. Update is ours: upstream has none.
class Api::V1::HomelabStatementsController < Api::V1::BaseController
  before_action :ensure_read_scope, only: [ :show ]
  before_action :ensure_write_scope, only: [ :create, :update ]
  before_action :ensure_statement_manager
  before_action :set_statement, only: [ :show, :update ]

  def show
    render_statement
  end

  def create
    file = params[:file]
    unless file.respond_to?(:read) && file.respond_to?(:original_filename)
      render_validation_error("file is required (multipart/form-data)")
      return
    end

    account = target_account
    return if performed?

    @statement = AccountStatement.create_from_prepared_upload!(
      family: family, account: account, prepared_upload: AccountStatement.prepare_upload!(file)
    )
    render_statement(status: :created)
  rescue AccountStatement::DuplicateUploadError => e
    raise ActiveRecord::RecordNotFound unless e.statement.viewable_by?(current_resource_owner)

    @statement = e.statement
    render_statement(duplicate: true)
  rescue AccountStatement::InvalidUploadError
    render_validation_error(
      "file must be a readable #{AccountStatement::ACCEPTED_FILE_EXTENSIONS.join(", ")} document " \
      "whose contents match its extension, at most #{AccountStatement::MAX_FILE_SIZE / 1.megabyte} MB"
    )
  rescue ActiveRecord::RecordInvalid => e
    render_invalid(e, "Statement could not be stored")
  end

  def update
    if @statement.account && !family.accounts.writable_by(current_resource_owner).exists?(@statement.account_id)
      raise ActiveRecord::RecordNotFound
    end

    @statement.update!(params.require(:statement).permit(
      :period_start_on, :period_end_on, :opening_balance, :closing_balance, :currency
    ))
    render_statement
  rescue ActiveRecord::RecordInvalid => e
    render_invalid(e, "Statement could not be updated")
  end

  private

    def family
      current_resource_owner.family
    end

    def set_statement
      raise ActiveRecord::RecordNotFound unless valid_uuid?(params[:id])

      @statement = family.account_statements.includes(:account).find(params[:id])
      raise ActiveRecord::RecordNotFound unless @statement.viewable_by?(current_resource_owner)
    end

    # Uploading links the statement straight away, so it needs the same write
    # access to the account as the web upload form.
    def target_account
      account_id = params[:account_id].presence
      return nil if account_id.blank?

      account = family.accounts.writable_by(current_resource_owner).find_by(id: account_id) if valid_uuid?(account_id)
      return account if account

      render json: { error: "not_found", message: "No account you can write to matched account_id" }, status: :not_found
      nil
    end

    def render_statement(status: :ok, duplicate: false)
      s = @statement
      render status: status, json: {
        data: {
          id: s.id,
          filename: s.filename,
          review_status: s.review_status,
          period_start_on: s.period_start_on&.iso8601,
          period_end_on: s.period_end_on&.iso8601,
          currency: s.statement_currency,
          opening_balance: s.opening_balance&.to_s,
          closing_balance: s.closing_balance&.to_s,
          account: s.account && { id: s.account.id, name: s.account.name, currency: s.account.currency },
          read_from: Array(s.sanitized_parser_output&.dig("metadata_sources")),
          reconciliation: s.reconciliation_status
        },
        meta: { duplicate: duplicate }
      }
    end

    def render_invalid(error, message)
      render json: { error: "validation_failed", message: message, errors: error.record.errors.full_messages },
             status: :unprocessable_entity
    end

    def ensure_statement_manager
      return if AccountStatement.statement_manager?(current_resource_owner)

      render json: { error: "forbidden", message: "Statements need an admin or member role" }, status: :forbidden
    end

    def ensure_read_scope
      authorize_scope!(:read)
    end

    def ensure_write_scope
      authorize_scope!(:write)
    end
end
