use domain::import::{
    ImportBatchStatus, ImportErrorPreview, ImportErrorSeverity, ImportMappingDefinitionError,
    ImportMappingField, ImportMappingResponse, ImportPreviewRow, ImportTemplateSummary,
    ImportTemplateVersionResponse, ensure_status_transition, validate_mapping_fields,
};
use serde_json::json;
use sqlx::{PgPool, Row};
use time::OffsetDateTime;
use uuid::Uuid;

#[derive(Debug, Clone)]
pub struct NewImportUpload {
    pub object_id: Uuid,
    pub import_id: Uuid,
    pub file_id: Uuid,
    pub workspace_id: Uuid,
    pub actor_user_id: Uuid,
    pub object_key: String,
    pub sha256: String,
    pub size_bytes: i64,
    pub original_filename: String,
    pub declared_mime_type: String,
    pub detected_format: String,
    pub request_id: Uuid,
}

#[derive(Debug, Clone)]
pub struct ImportUploadRecord {
    pub import_id: Uuid,
    pub status: ImportBatchStatus,
    pub file_id: Uuid,
    pub object_key: String,
    pub original_filename: String,
    pub declared_mime_type: String,
    pub detected_format: String,
    pub sha256: String,
    pub size_bytes: i64,
    pub created_at: OffsetDateTime,
    pub updated_at: OffsetDateTime,
}

#[derive(Debug, Clone)]
pub struct InspectionUpdate<'a> {
    pub workspace_id: Uuid,
    pub actor_user_id: Uuid,
    pub import_id: Uuid,
    pub detected_encoding: Option<&'a str>,
    pub detected_delimiter: Option<&'a str>,
    pub selected_sheet: Option<&'a str>,
    pub header_row: i32,
}

#[derive(Debug, Clone, Copy)]
pub struct InspectionSaveResult {
    pub status: ImportBatchStatus,
    pub preview_invalidated: bool,
}

#[derive(Debug, Clone)]
pub struct PreviewInputSnapshot<'a> {
    pub detected_encoding: Option<&'a str>,
    pub detected_delimiter: Option<&'a str>,
    pub selected_sheet: Option<&'a str>,
    pub header_row: i32,
    pub fields: &'a [ImportMappingField],
}

#[derive(Debug, Clone)]
pub struct PreviewSave<'a> {
    pub workspace_id: Uuid,
    pub actor_user_id: Uuid,
    pub import_id: Uuid,
    pub snapshot: PreviewInputSnapshot<'a>,
    pub rows: &'a [ImportPreviewRow],
    pub errors: &'a [ImportErrorPreview],
    pub warnings: &'a [ImportErrorPreview],
}

#[derive(Debug, Clone)]
struct ExistingMapping {
    dataset_type: String,
    template_version_id: Option<Uuid>,
    fields: Vec<ImportMappingField>,
}

#[derive(Debug, thiserror::Error)]
pub enum ImportRepositoryError {
    #[error("import is not visible")]
    NotFound,
    #[error("invalid import status transition")]
    InvalidTransition,
    #[error("invalid import status stored in database")]
    InvalidStoredStatus,
    #[error("database operation failed")]
    Database(#[from] sqlx::Error),
    #[error("invalid template configuration stored in database")]
    InvalidTemplateConfiguration,
    #[error("mapping does not match a supported dataset definition")]
    InvalidMappingDefinition(ImportMappingDefinitionError),
    #[error("template version is not visible")]
    TemplateVersionNotFound,
    #[error("mapping does not match the selected template version")]
    TemplateVersionMismatch,
    #[error("inspection or mapping changed before preview could be saved")]
    PreviewInputsChanged,
}

pub async fn register_upload(
    pool: &PgPool,
    upload: &NewImportUpload,
) -> Result<ImportUploadRecord, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, upload.workspace_id).await?;
    sqlx::query(
        "insert into stored_objects
            (id, workspace_id, object_key, sha256, size_bytes, mime_type, backend, state,
             retention_until, created_by)
         values ($1, $2, $3, $4, $5, $6, 'local', 'available', null, $7)",
    )
    .bind(upload.object_id)
    .bind(upload.workspace_id)
    .bind(&upload.object_key)
    .bind(&upload.sha256)
    .bind(upload.size_bytes)
    .bind(&upload.declared_mime_type)
    .bind(upload.actor_user_id)
    .execute(&mut *tx)
    .await?;
    let batch_row = sqlx::query(
        "insert into import_batches (id, workspace_id, status, dataset_type, created_by)
         values ($1, $2, 'uploaded', 'generic', $3)
         returning created_at, updated_at",
    )
    .bind(upload.import_id)
    .bind(upload.workspace_id)
    .bind(upload.actor_user_id)
    .fetch_one(&mut *tx)
    .await?;
    sqlx::query(
        "insert into import_files
            (id, workspace_id, import_batch_id, stored_object_id, original_filename,
             declared_mime_type, detected_format, sha256, size_bytes, created_by)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
    )
    .bind(upload.file_id)
    .bind(upload.workspace_id)
    .bind(upload.import_id)
    .bind(upload.object_id)
    .bind(&upload.original_filename)
    .bind(&upload.declared_mime_type)
    .bind(&upload.detected_format)
    .bind(&upload.sha256)
    .bind(upload.size_bytes)
    .bind(upload.actor_user_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, 'import.upload', 'success', $4, jsonb_build_object('import_id', $5::text))",
    )
    .bind(Uuid::now_v7())
    .bind(upload.workspace_id)
    .bind(upload.actor_user_id)
    .bind(upload.request_id)
    .bind(upload.import_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;

    Ok(ImportUploadRecord {
        import_id: upload.import_id,
        status: ImportBatchStatus::Uploaded,
        file_id: upload.file_id,
        object_key: upload.object_key.clone(),
        original_filename: upload.original_filename.clone(),
        declared_mime_type: upload.declared_mime_type.clone(),
        detected_format: upload.detected_format.clone(),
        sha256: upload.sha256.clone(),
        size_bytes: upload.size_bytes,
        created_at: batch_row.get("created_at"),
        updated_at: batch_row.get("updated_at"),
    })
}

pub async fn record_upload_denied(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    request_id: Uuid,
    error_code: &'static str,
) -> Result<(), ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    sqlx::query(
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, 'import.upload', 'denied', $4,
                 jsonb_build_object('error_code', $5::text))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(request_id)
    .bind(error_code)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}

pub async fn get_import(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<ImportUploadRecord, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let row = sqlx::query(
        "select b.id as import_id, b.status::text as status, b.created_at, b.updated_at,
                f.id as file_id, f.original_filename, f.declared_mime_type,
                f.detected_format, f.sha256, f.size_bytes, o.object_key
         from import_batches b
         join import_files f
           on f.workspace_id = b.workspace_id and f.import_batch_id = b.id
         join stored_objects o
           on o.workspace_id = f.workspace_id and o.id = f.stored_object_id
         where b.workspace_id = $1 and b.id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?;
    tx.commit().await?;
    let row = row.ok_or(ImportRepositoryError::NotFound)?;
    let status: String = row.get("status");
    Ok(ImportUploadRecord {
        import_id: row.get("import_id"),
        status: ImportBatchStatus::parse(&status)
            .ok_or(ImportRepositoryError::InvalidStoredStatus)?,
        file_id: row.get("file_id"),
        object_key: row.get("object_key"),
        original_filename: row.get("original_filename"),
        declared_mime_type: row.get("declared_mime_type"),
        detected_format: row.get("detected_format"),
        sha256: row.get("sha256"),
        size_bytes: row.get("size_bytes"),
        created_at: row.get("created_at"),
        updated_at: row.get("updated_at"),
    })
}

pub async fn transition_status(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
    from: ImportBatchStatus,
    to: ImportBatchStatus,
) -> Result<(), ImportRepositoryError> {
    ensure_status_transition(from, to).map_err(|_| ImportRepositoryError::InvalidTransition)?;
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let affected = sqlx::query(
        "update import_batches
         set status = $1::import_batch_status, updated_at = now()
         where workspace_id = $2 and id = $3 and status = $4::import_batch_status",
    )
    .bind(to.as_str())
    .bind(workspace_id)
    .bind(import_id)
    .bind(from.as_str())
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if affected != 1 {
        return Err(ImportRepositoryError::InvalidTransition);
    }
    tx.commit().await?;
    Ok(())
}

pub async fn save_inspection(
    pool: &PgPool,
    update: InspectionUpdate<'_>,
) -> Result<InspectionSaveResult, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    let workspace_id = update.workspace_id;
    let import_id = update.import_id;
    set_workspace(&mut tx, workspace_id).await?;
    let status = lock_import_batch(&mut tx, workspace_id, import_id).await?;
    if !matches!(
        status,
        ImportBatchStatus::Uploaded
            | ImportBatchStatus::Inspected
            | ImportBatchStatus::Mapped
            | ImportBatchStatus::PreviewReady
    ) {
        return Err(ImportRepositoryError::InvalidTransition);
    }
    let previous = sqlx::query(
        "select detected_encoding, detected_delimiter, selected_sheet, header_row
         from import_files
         where workspace_id = $1 and import_batch_id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    let parameters_changed = previous.get::<Option<String>, _>("detected_encoding")
        != update.detected_encoding.map(str::to_string)
        || previous.get::<Option<String>, _>("detected_delimiter")
            != update.detected_delimiter.map(str::to_string)
        || previous.get::<Option<String>, _>("selected_sheet")
            != update.selected_sheet.map(str::to_string)
        || previous.get::<Option<i32>, _>("header_row") != Some(update.header_row);
    sqlx::query(
        "update import_files
         set detected_encoding = $1,
             detected_delimiter = $2,
             selected_sheet = $3,
             header_row = $4,
             inspected_at = now(),
             updated_at = now()
         where workspace_id = $5 and import_batch_id = $6",
    )
    .bind(update.detected_encoding)
    .bind(update.detected_delimiter)
    .bind(update.selected_sheet)
    .bind(update.header_row)
    .bind(workspace_id)
    .bind(import_id)
    .execute(&mut *tx)
    .await?;
    let (status, preview_invalidated) = if status == ImportBatchStatus::Uploaded {
        update_status_in_tx(
            &mut tx,
            workspace_id,
            import_id,
            ImportBatchStatus::Uploaded,
            ImportBatchStatus::Inspected,
        )
        .await?;
        (ImportBatchStatus::Inspected, false)
    } else if preview_invalidation_required(status, parameters_changed) {
        invalidate_preview_in_tx(&mut tx, workspace_id, import_id, status).await?;
        (ImportBatchStatus::Mapped, true)
    } else {
        if parameters_changed {
            delete_preview_data_in_tx(&mut tx, workspace_id, import_id).await?;
        }
        (status, false)
    };
    sqlx::query(
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, 'import.inspect', 'success', $4, jsonb_build_object('import_id', $5::text))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(update.actor_user_id)
    .bind(Uuid::now_v7())
    .bind(import_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(InspectionSaveResult {
        status,
        preview_invalidated,
    })
}

pub async fn get_mapping_fields(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<Vec<ImportMappingField>, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let row = sqlx::query(
        "select mapping_json
         from import_mappings
         where workspace_id = $1 and import_batch_id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?;
    tx.commit().await?;
    let Some(row) = row else {
        return Ok(Vec::new());
    };
    let value: serde_json::Value = row.get("mapping_json");
    parse_fields_from_value(value)
}

pub async fn save_mapping(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    import_id: Uuid,
    dataset_type: &str,
    template_version_id: Option<Uuid>,
    fields: &[ImportMappingField],
) -> Result<ImportMappingResponse, ImportRepositoryError> {
    validate_mapping_fields(dataset_type, fields)
        .map_err(ImportRepositoryError::InvalidMappingDefinition)?;
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let status = lock_import_batch(&mut tx, workspace_id, import_id).await?;
    if !matches!(
        status,
        ImportBatchStatus::Inspected | ImportBatchStatus::Mapped | ImportBatchStatus::PreviewReady
    ) {
        return Err(ImportRepositoryError::InvalidTransition);
    }
    let existing_mapping = existing_mapping_for_update(&mut tx, workspace_id, import_id).await?;
    if !template_version_binding_is_allowed(
        existing_mapping
            .as_ref()
            .and_then(|mapping| mapping.template_version_id),
        template_version_id,
    ) {
        return Err(ImportRepositoryError::TemplateVersionMismatch);
    }
    if let Some(template_version_id) = template_version_id {
        validate_template_version_binding(
            &mut tx,
            workspace_id,
            template_version_id,
            dataset_type,
            fields,
        )
        .await?;
    }
    let mapping_changed = mapping_has_changed(
        existing_mapping.as_ref(),
        dataset_type,
        template_version_id,
        fields,
    );
    let (status, preview_invalidated) = if mapping_changed {
        if status == ImportBatchStatus::PreviewReady {
            invalidate_preview_in_tx(&mut tx, workspace_id, import_id, status).await?;
            (ImportBatchStatus::Mapped, true)
        } else {
            delete_preview_data_in_tx(&mut tx, workspace_id, import_id).await?;
            (status, false)
        }
    } else {
        (status, false)
    };
    let mapping_json = json!({ "fields": fields });
    let affected = sqlx::query(
        "insert into import_mappings
            (id, workspace_id, import_batch_id, template_version_id, dataset_type, mapping_json, created_by)
         values ($1, $2, $3, $4, $5, $6, $7)
         on conflict (workspace_id, import_batch_id) do update
         set template_version_id = excluded.template_version_id,
             dataset_type = excluded.dataset_type,
             mapping_json = excluded.mapping_json,
             updated_at = now()
          where import_mappings.template_version_id is null
             or import_mappings.template_version_id = excluded.template_version_id",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(import_id)
    .bind(template_version_id)
    .bind(dataset_type)
    .bind(mapping_json)
    .bind(actor_user_id)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if affected != 1 {
        return Err(ImportRepositoryError::TemplateVersionMismatch);
    }
    sqlx::query(
        "update import_batches
         set dataset_type = $1, updated_at = now()
         where workspace_id = $2 and id = $3",
    )
    .bind(dataset_type)
    .bind(workspace_id)
    .bind(import_id)
    .execute(&mut *tx)
    .await?;
    let status = if status == ImportBatchStatus::Inspected {
        update_status_in_tx(
            &mut tx,
            workspace_id,
            import_id,
            ImportBatchStatus::Inspected,
            ImportBatchStatus::Mapped,
        )
        .await?;
        ImportBatchStatus::Mapped
    } else {
        status
    };
    insert_audit_event(
        &mut tx,
        workspace_id,
        actor_user_id,
        "import.mapping",
        import_id,
    )
    .await?;
    tx.commit().await?;
    Ok(ImportMappingResponse {
        import_id,
        status,
        dataset_type: dataset_type.to_string(),
        template_version_id,
        fields: fields.to_vec(),
        preview_invalidated,
    })
}

pub async fn save_preview(
    pool: &PgPool,
    update: PreviewSave<'_>,
) -> Result<(), ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    let workspace_id = update.workspace_id;
    let import_id = update.import_id;
    set_workspace(&mut tx, workspace_id).await?;
    let status = lock_import_batch(&mut tx, workspace_id, import_id).await?;
    if !matches!(
        status,
        ImportBatchStatus::Mapped | ImportBatchStatus::PreviewReady
    ) {
        return Err(ImportRepositoryError::InvalidTransition);
    }
    if !preview_inputs_match(&mut tx, workspace_id, import_id, &update.snapshot).await? {
        return Err(ImportRepositoryError::PreviewInputsChanged);
    }
    delete_preview_data_in_tx(&mut tx, workspace_id, import_id).await?;
    for row in update.rows {
        let raw_values = json_object_from_cells(row, |cell| &cell.raw_value);
        let normalized_values = json_object_from_cells(row, |cell| &cell.normalized_value);
        let target_fields =
            json_object_from_cells(row, |cell| cell.target_field.as_deref().unwrap_or(""));
        sqlx::query(
            "insert into import_staging_rows
                (id, workspace_id, import_batch_id, row_number, raw_values, normalized_values,
                 target_fields, warnings, created_by)
             values ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        )
        .bind(Uuid::now_v7())
        .bind(workspace_id)
        .bind(import_id)
        .bind(row.row_number as i32)
        .bind(raw_values)
        .bind(normalized_values)
        .bind(target_fields)
        .bind(json!(&row.warnings))
        .bind(update.actor_user_id)
        .execute(&mut *tx)
        .await?;
    }
    for item in update.errors.iter().chain(update.warnings.iter()) {
        insert_error(&mut tx, workspace_id, update.actor_user_id, import_id, item).await?;
    }
    if status == ImportBatchStatus::Mapped {
        update_status_in_tx(
            &mut tx,
            workspace_id,
            import_id,
            ImportBatchStatus::Mapped,
            ImportBatchStatus::PreviewReady,
        )
        .await?;
    }
    insert_audit_event(
        &mut tx,
        workspace_id,
        update.actor_user_id,
        "import.preview",
        import_id,
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

pub async fn list_errors(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<Vec<ImportErrorPreview>, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let _ = current_status(&mut tx, workspace_id, import_id).await?;
    let rows = sqlx::query(
        "select row_number, field_name, severity, error_code, raw_value, message
         from import_errors
         where workspace_id = $1 and import_batch_id = $2
         order by row_number nulls first, created_at, id
         limit 500",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    rows.into_iter()
        .map(|row| {
            let severity: String = row.get("severity");
            Ok(ImportErrorPreview {
                row_number: row
                    .get::<Option<i32>, _>("row_number")
                    .map(|value| value as u32),
                field_name: row.get("field_name"),
                severity: ImportErrorSeverity::parse(&severity)
                    .ok_or(ImportRepositoryError::InvalidStoredStatus)?,
                error_code: row.get("error_code"),
                raw_value: row.get("raw_value"),
                message: row.get("message"),
            })
        })
        .collect()
}

pub async fn create_template(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    dataset_type: &str,
    name: &str,
    description: Option<&str>,
    fields: &[ImportMappingField],
) -> Result<ImportTemplateVersionResponse, ImportRepositoryError> {
    validate_mapping_fields(dataset_type, fields)
        .map_err(ImportRepositoryError::InvalidMappingDefinition)?;
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let template_id = Uuid::now_v7();
    let version_id = Uuid::now_v7();
    sqlx::query(
        "insert into import_templates
            (id, workspace_id, dataset_type, name, description, created_by)
         values ($1, $2, $3, $4, $5, $6)",
    )
    .bind(template_id)
    .bind(workspace_id)
    .bind(dataset_type)
    .bind(name)
    .bind(description)
    .bind(actor_user_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "insert into import_template_versions
            (id, workspace_id, template_id, version_number, dataset_type,
             configuration_json, created_by)
         values ($1, $2, $3, 1, $4, $5, $6)",
    )
    .bind(version_id)
    .bind(workspace_id)
    .bind(template_id)
    .bind(dataset_type)
    .bind(json!({ "fields": fields }))
    .bind(actor_user_id)
    .execute(&mut *tx)
    .await?;
    insert_audit_event(
        &mut tx,
        workspace_id,
        actor_user_id,
        "import.template",
        template_id,
    )
    .await?;
    tx.commit().await?;
    Ok(ImportTemplateVersionResponse {
        id: version_id,
        template_id,
        version_number: 1,
        dataset_type: dataset_type.to_string(),
        fields: fields.to_vec(),
    })
}

pub async fn list_templates(
    pool: &PgPool,
    workspace_id: Uuid,
) -> Result<Vec<ImportTemplateSummary>, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(
        "select t.id, t.dataset_type, t.name, t.description,
                v.id as latest_version_id, v.version_number as latest_version_number,
                v.configuration_json
         from import_templates t
         join lateral (
             select id, version_number, configuration_json
             from import_template_versions v
             where v.workspace_id = t.workspace_id and v.template_id = t.id
             order by version_number desc
             limit 1
         ) v on true
         where t.workspace_id = $1
         order by t.created_at desc, t.id",
    )
    .bind(workspace_id)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    rows.into_iter()
        .map(|row| {
            let configuration: serde_json::Value = row.get("configuration_json");
            Ok(ImportTemplateSummary {
                id: row.get("id"),
                dataset_type: row.get("dataset_type"),
                name: row.get("name"),
                description: row.get("description"),
                latest_version_id: row.get("latest_version_id"),
                latest_version_number: row.get("latest_version_number"),
                fields: parse_fields_from_value(configuration)?,
            })
        })
        .collect()
}

async fn existing_mapping_for_update(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<Option<ExistingMapping>, ImportRepositoryError> {
    let row = sqlx::query(
        "select dataset_type, template_version_id, mapping_json
         from import_mappings
         where workspace_id = $1 and import_batch_id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut **tx)
    .await?;
    row.map(|row| {
        Ok(ExistingMapping {
            dataset_type: row.get("dataset_type"),
            template_version_id: row.get("template_version_id"),
            fields: parse_fields_from_value(row.get("mapping_json"))?,
        })
    })
    .transpose()
}

async fn validate_template_version_binding(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    template_version_id: Uuid,
    dataset_type: &str,
    fields: &[ImportMappingField],
) -> Result<(), ImportRepositoryError> {
    let row = sqlx::query(
        "select v.dataset_type, v.configuration_json
         from import_template_versions v
         where v.workspace_id = $1 and v.id = $2",
    )
    .bind(workspace_id)
    .bind(template_version_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ImportRepositoryError::TemplateVersionNotFound)?;
    let template_dataset_type: String = row.get("dataset_type");
    let configuration: serde_json::Value = row.get("configuration_json");
    let template_fields = parse_fields_from_value(configuration)?;
    if !template_binding_matches(
        &template_dataset_type,
        dataset_type,
        &template_fields,
        fields,
    ) {
        return Err(ImportRepositoryError::TemplateVersionMismatch);
    }
    Ok(())
}

fn template_binding_matches(
    template_dataset_type: &str,
    mapping_dataset_type: &str,
    template_fields: &[ImportMappingField],
    mapping_fields: &[ImportMappingField],
) -> bool {
    template_dataset_type == mapping_dataset_type && template_fields == mapping_fields
}

fn template_version_binding_is_allowed(existing: Option<Uuid>, requested: Option<Uuid>) -> bool {
    existing.is_none() || existing == requested
}

fn mapping_has_changed(
    existing: Option<&ExistingMapping>,
    dataset_type: &str,
    template_version_id: Option<Uuid>,
    fields: &[ImportMappingField],
) -> bool {
    match existing {
        None => true,
        Some(mapping) => {
            mapping.dataset_type != dataset_type
                || mapping.template_version_id != template_version_id
                || mapping.fields.as_slice() != fields
        }
    }
}

fn preview_invalidation_required(status: ImportBatchStatus, configuration_changed: bool) -> bool {
    configuration_changed && status == ImportBatchStatus::PreviewReady
}

async fn current_status(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<ImportBatchStatus, ImportRepositoryError> {
    let row = sqlx::query(
        "select status::text as status
         from import_batches
         where workspace_id = $1 and id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    let status: String = row.get("status");
    ImportBatchStatus::parse(&status).ok_or(ImportRepositoryError::InvalidStoredStatus)
}

async fn lock_import_batch(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<ImportBatchStatus, ImportRepositoryError> {
    let row = sqlx::query(
        "select status::text as status
         from import_batches
         where workspace_id = $1 and id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    let status: String = row.get("status");
    ImportBatchStatus::parse(&status).ok_or(ImportRepositoryError::InvalidStoredStatus)
}

async fn preview_inputs_match(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
    snapshot: &PreviewInputSnapshot<'_>,
) -> Result<bool, ImportRepositoryError> {
    let file = sqlx::query(
        "select detected_encoding, detected_delimiter, selected_sheet, header_row
         from import_files
         where workspace_id = $1 and import_batch_id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    let mapping = existing_mapping_for_update(tx, workspace_id, import_id)
        .await?
        .ok_or(ImportRepositoryError::PreviewInputsChanged)?;
    Ok(file.get::<Option<String>, _>("detected_encoding")
        == snapshot.detected_encoding.map(str::to_string)
        && file.get::<Option<String>, _>("detected_delimiter")
            == snapshot.detected_delimiter.map(str::to_string)
        && file.get::<Option<String>, _>("selected_sheet")
            == snapshot.selected_sheet.map(str::to_string)
        && file.get::<Option<i32>, _>("header_row") == Some(snapshot.header_row)
        && mapping.fields.as_slice() == snapshot.fields)
}

async fn delete_preview_data_in_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<(), ImportRepositoryError> {
    sqlx::query("delete from import_staging_rows where workspace_id = $1 and import_batch_id = $2")
        .bind(workspace_id)
        .bind(import_id)
        .execute(&mut **tx)
        .await?;
    sqlx::query("delete from import_errors where workspace_id = $1 and import_batch_id = $2")
        .bind(workspace_id)
        .bind(import_id)
        .execute(&mut **tx)
        .await?;
    Ok(())
}

async fn invalidate_preview_in_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
    from: ImportBatchStatus,
) -> Result<(), ImportRepositoryError> {
    delete_preview_data_in_tx(tx, workspace_id, import_id).await?;
    update_status_in_tx(tx, workspace_id, import_id, from, ImportBatchStatus::Mapped).await
}

async fn update_status_in_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
    from: ImportBatchStatus,
    to: ImportBatchStatus,
) -> Result<(), ImportRepositoryError> {
    ensure_status_transition(from, to).map_err(|_| ImportRepositoryError::InvalidTransition)?;
    let affected = sqlx::query(
        "update import_batches
          set status = $1::import_batch_status, updated_at = now()
          where workspace_id = $2 and id = $3 and status = $4::import_batch_status",
    )
    .bind(to.as_str())
    .bind(workspace_id)
    .bind(import_id)
    .bind(from.as_str())
    .execute(&mut **tx)
    .await?
    .rows_affected();
    if affected == 1 {
        Ok(())
    } else {
        Err(ImportRepositoryError::InvalidTransition)
    }
}

async fn insert_audit_event(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    event_type: &'static str,
    resource_id: Uuid,
) -> Result<(), ImportRepositoryError> {
    sqlx::query(
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, 'success', $5,
                 jsonb_build_object('resource_id', $6::text))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(event_type)
    .bind(Uuid::now_v7())
    .bind(resource_id)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

async fn insert_error(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    import_id: Uuid,
    item: &ImportErrorPreview,
) -> Result<(), ImportRepositoryError> {
    sqlx::query(
        "insert into import_errors
            (id, workspace_id, import_batch_id, row_number, field_name, severity,
             error_code, raw_value, message, created_by)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(import_id)
    .bind(item.row_number.map(|value| value as i32))
    .bind(&item.field_name)
    .bind(item.severity.as_str())
    .bind(&item.error_code)
    .bind(&item.raw_value)
    .bind(&item.message)
    .bind(actor_user_id)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

fn parse_fields_from_value(
    value: serde_json::Value,
) -> Result<Vec<ImportMappingField>, ImportRepositoryError> {
    let fields = value
        .get("fields")
        .cloned()
        .unwrap_or_else(|| serde_json::Value::Array(Vec::new()));
    serde_json::from_value(fields).map_err(|_| ImportRepositoryError::InvalidTemplateConfiguration)
}

fn json_object_from_cells<F>(row: &ImportPreviewRow, select: F) -> serde_json::Value
where
    F: Fn(&domain::import::ImportPreviewCell) -> &str,
{
    let mut object = serde_json::Map::new();
    for cell in &row.cells {
        object.insert(cell.column.clone(), json!(select(cell)));
    }
    serde_json::Value::Object(object)
}

async fn set_workspace(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
) -> Result<(), sqlx::Error> {
    sqlx::query("select set_config('app.current_workspace_id', $1, true)")
        .bind(workspace_id.to_string())
        .execute(&mut **tx)
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn template_configuration_round_trips_and_requires_exact_binding() {
        let fields = vec![ImportMappingField {
            source_column: "日期".into(),
            target_field: "record_date".into(),
            transform: Some("date_ymd".into()),
        }];
        let parsed = parse_fields_from_value(json!({ "fields": fields.clone() })).unwrap();
        assert_eq!(parsed, fields);
        assert!(template_binding_matches(
            "generic", "generic", &parsed, &fields
        ));
        assert!(!template_binding_matches(
            "generic", "other", &parsed, &fields
        ));
        assert!(!template_binding_matches(
            "generic",
            "generic",
            &parsed,
            &[ImportMappingField {
                target_field: "code".into(),
                ..fields[0].clone()
            }]
        ));
    }

    #[test]
    fn inspection_parameter_change_invalidates_preview_ready_state() {
        assert!(preview_invalidation_required(
            ImportBatchStatus::PreviewReady,
            true
        ));
        assert!(!preview_invalidation_required(
            ImportBatchStatus::PreviewReady,
            false
        ));
        assert!(!preview_invalidation_required(
            ImportBatchStatus::Mapped,
            true
        ));
    }

    #[test]
    fn ordinary_and_template_mapping_changes_invalidate_preview_ready_state() {
        let fields = vec![ImportMappingField {
            source_column: "date".into(),
            target_field: "trade_date".into(),
            transform: Some("date_ymd".into()),
        }];
        let template_version_id = Uuid::now_v7();
        let ordinary_mapping = ExistingMapping {
            dataset_type: "generic".into(),
            template_version_id: None,
            fields: fields.clone(),
        };

        assert!(preview_invalidation_required(
            ImportBatchStatus::PreviewReady,
            mapping_has_changed(
                Some(&ordinary_mapping),
                "generic",
                None,
                &[ImportMappingField {
                    target_field: "price".into(),
                    ..fields[0].clone()
                }]
            )
        ));
        assert!(preview_invalidation_required(
            ImportBatchStatus::PreviewReady,
            mapping_has_changed(
                Some(&ordinary_mapping),
                "generic",
                Some(template_version_id),
                &fields
            )
        ));
    }

    #[test]
    fn template_version_rebinding_rejects_different_or_empty_second_binding() {
        let first = Uuid::now_v7();
        let second = Uuid::now_v7();
        assert!(template_version_binding_is_allowed(None, Some(first)));
        assert!(template_version_binding_is_allowed(
            Some(first),
            Some(first)
        ));
        assert!(!template_version_binding_is_allowed(
            Some(first),
            Some(second)
        ));
        assert!(!template_version_binding_is_allowed(Some(first), None));
    }
}
