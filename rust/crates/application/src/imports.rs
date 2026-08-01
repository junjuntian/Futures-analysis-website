use calamine::{Data, Reader, open_workbook_auto_from_rs};
use domain::import::{
    ImportBatchStatus, ImportColumnPreview, ImportDetection, ImportErrorPreview,
    ImportErrorSeverity, ImportInspectRequest, ImportInspectResponse, ImportMappingField,
    ImportPreviewCell, ImportPreviewRequest, ImportPreviewRow, ImportSheetInfo,
};
use encoding_rs::{Encoding, GBK, UTF_8};
use std::{collections::BTreeMap, env, io::Cursor, path::Path};
use uuid::Uuid;

pub const DEFAULT_IMPORT_MAX_BYTES: u64 = 50 * 1024 * 1024;
pub const DEFAULT_IMPORT_MAX_ROWS: usize = 100_000;

#[derive(Debug, Clone)]
pub struct UploadPolicy {
    pub max_bytes: u64,
}

impl UploadPolicy {
    pub fn from_env() -> Result<Self, UploadValidationError> {
        let max_bytes = match env::var("IMPORT_MAX_BYTES") {
            Ok(value) => value
                .parse::<u64>()
                .ok()
                .filter(|value| *value > 0)
                .ok_or(UploadValidationError::InvalidConfiguration)?,
            Err(_) => DEFAULT_IMPORT_MAX_BYTES,
        };
        Ok(Self { max_bytes })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcceptedFileFormat {
    Txt,
    Csv,
    Xls,
    Xlsx,
}

impl AcceptedFileFormat {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Txt => "txt",
            Self::Csv => "csv",
            Self::Xls => "xls",
            Self::Xlsx => "xlsx",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UploadValidationError {
    InvalidConfiguration,
    MissingFilename,
    DangerousFilename,
    UnsupportedFormat,
    MimeMismatch,
    FileTooLarge,
    EmptyFile,
}

pub struct UploadValidator {
    policy: UploadPolicy,
    format: AcceptedFileFormat,
    declared_mime_type: String,
    prefix: Vec<u8>,
    size_bytes: u64,
}

impl UploadValidator {
    pub fn new(
        policy: UploadPolicy,
        filename: &str,
        declared_mime_type: Option<&str>,
    ) -> Result<Self, UploadValidationError> {
        validate_filename(filename)?;
        let extension = Path::new(filename)
            .extension()
            .and_then(|value| value.to_str())
            .map(str::to_ascii_lowercase)
            .ok_or(UploadValidationError::UnsupportedFormat)?;
        let format = match extension.as_str() {
            "txt" => AcceptedFileFormat::Txt,
            "csv" => AcceptedFileFormat::Csv,
            "xls" => AcceptedFileFormat::Xls,
            "xlsx" => AcceptedFileFormat::Xlsx,
            _ => return Err(UploadValidationError::UnsupportedFormat),
        };
        let declared_mime_type = declared_mime_type
            .unwrap_or("application/octet-stream")
            .split(';')
            .next()
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase();
        if !mime_matches(format, &declared_mime_type) {
            return Err(UploadValidationError::MimeMismatch);
        }
        Ok(Self {
            policy,
            format,
            declared_mime_type,
            prefix: Vec::with_capacity(16),
            size_bytes: 0,
        })
    }

    pub fn observe(&mut self, chunk: &[u8]) -> Result<(), UploadValidationError> {
        self.size_bytes = self
            .size_bytes
            .checked_add(chunk.len() as u64)
            .ok_or(UploadValidationError::FileTooLarge)?;
        if self.size_bytes > self.policy.max_bytes {
            return Err(UploadValidationError::FileTooLarge);
        }
        let remaining = 16_usize.saturating_sub(self.prefix.len());
        self.prefix
            .extend_from_slice(&chunk[..chunk.len().min(remaining)]);
        Ok(())
    }

    pub fn finish(self) -> Result<ValidatedUpload, UploadValidationError> {
        if self.size_bytes == 0 {
            return Err(UploadValidationError::EmptyFile);
        }
        validate_magic(self.format, &self.prefix)?;
        Ok(ValidatedUpload {
            format: self.format,
            declared_mime_type: self.declared_mime_type,
            size_bytes: self.size_bytes,
        })
    }
}

pub struct ValidatedUpload {
    pub format: AcceptedFileFormat,
    pub declared_mime_type: String,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ImportParseError {
    UnsupportedFormat,
    InvalidEncoding,
    InvalidDelimiter,
    InvalidSheet,
    SpreadsheetReadFailed,
    TooManyRows,
    UnsupportedTransform,
}

pub fn inspect_content(
    import_id: Uuid,
    status: ImportBatchStatus,
    detected_format: &str,
    bytes: &[u8],
    request: ImportInspectRequest,
    mapping: &[ImportMappingField],
) -> Result<ImportInspectResponse, ImportParseError> {
    match detected_format {
        "txt" | "csv" => inspect_text(import_id, status, detected_format, bytes, request, mapping),
        "xls" | "xlsx" => {
            inspect_spreadsheet(import_id, status, detected_format, bytes, request, mapping)
        }
        _ => Err(ImportParseError::UnsupportedFormat),
    }
}

pub fn preview_content(
    import_id: Uuid,
    status: ImportBatchStatus,
    detected_format: &str,
    bytes: &[u8],
    request: ImportPreviewRequest,
    mapping: &[ImportMappingField],
) -> Result<ImportInspectResponse, ImportParseError> {
    inspect_content(
        import_id,
        status,
        detected_format,
        bytes,
        ImportInspectRequest {
            encoding: request.encoding,
            delimiter: request.delimiter,
            selected_sheet: request.selected_sheet,
            header_row: request.header_row,
        },
        mapping,
    )
}

pub fn parse_all_content(
    detected_format: &str,
    bytes: &[u8],
    request: ImportPreviewRequest,
    mapping: &[ImportMappingField],
    max_rows: usize,
) -> Result<Vec<ImportPreviewRow>, ImportParseError> {
    Ok(parse_all_content_with_warnings(detected_format, bytes, request, mapping, max_rows)?.rows)
}

pub struct FullImportParse {
    pub rows: Vec<ImportPreviewRow>,
    pub warnings: Vec<ImportErrorPreview>,
}

pub fn parse_all_content_with_warnings(
    detected_format: &str,
    bytes: &[u8],
    request: ImportPreviewRequest,
    mapping: &[ImportMappingField],
    max_rows: usize,
) -> Result<FullImportParse, ImportParseError> {
    let header_row = request.header_row.unwrap_or(1).max(1);
    let (records, warnings) = match detected_format {
        "txt" | "csv" => {
            let decoded = decode_text(bytes, request.encoding.as_deref())?;
            let delimiter = detect_delimiter(&decoded.text, request.delimiter.as_deref())?;
            (
                parse_delimited(&decoded.text, delimiter.value.as_deref().unwrap_or(",")),
                Vec::new(),
            )
        }
        "xls" | "xlsx" => {
            let cursor = Cursor::new(bytes.to_vec());
            let mut workbook = open_workbook_auto_from_rs(cursor)
                .map_err(|_| ImportParseError::SpreadsheetReadFailed)?;
            let sheet_names = workbook.sheet_names();
            let selected_sheet = request
                .selected_sheet
                .or_else(|| sheet_names.first().cloned())
                .ok_or(ImportParseError::InvalidSheet)?;
            if !sheet_names.iter().any(|name| name == &selected_sheet) {
                return Err(ImportParseError::InvalidSheet);
            }
            let warnings = collect_formula_warnings(&mut workbook, &selected_sheet);
            let records = workbook
                .worksheet_range(&selected_sheet)
                .map_err(|_| ImportParseError::SpreadsheetReadFailed)?
                .rows()
                .map(|row| row.iter().map(cell_to_string).collect::<Vec<_>>())
                .collect();
            (records, warnings)
        }
        _ => return Err(ImportParseError::UnsupportedFormat),
    };
    let total_rows = records.len().saturating_sub(header_row as usize);
    if total_rows > max_rows {
        return Err(ImportParseError::TooManyRows);
    }
    let columns = columns_from_records(&records, header_row);
    Ok(FullImportParse {
        rows: rows_from_records(&records, header_row, &columns, mapping, None)?,
        warnings,
    })
}

fn inspect_text(
    import_id: Uuid,
    status: ImportBatchStatus,
    detected_format: &str,
    bytes: &[u8],
    request: ImportInspectRequest,
    mapping: &[ImportMappingField],
) -> Result<ImportInspectResponse, ImportParseError> {
    let decoded = decode_text(bytes, request.encoding.as_deref())?;
    let delimiter = detect_delimiter(&decoded.text, request.delimiter.as_deref())?;
    let records = parse_delimited(&decoded.text, delimiter.value.as_deref().unwrap_or(","));
    let header_row = request.header_row.unwrap_or(1).max(1);
    let columns = columns_from_records(&records, header_row);
    let rows = preview_rows_from_records(&records, header_row, &columns, mapping)?;
    let errors = if records.is_empty() {
        vec![parse_message(
            None,
            None,
            ImportErrorSeverity::Error,
            "empty_parse_result",
            None,
            "未解析到可预览的数据行",
        )]
    } else {
        Vec::new()
    };
    Ok(ImportInspectResponse {
        import_id,
        status,
        detected_format: detected_format.to_string(),
        encoding: decoded.detection,
        delimiter,
        sheets: Vec::new(),
        selected_sheet: None,
        header_row,
        columns,
        preview_row_count: rows.len() as u32,
        total_rows: records.len().saturating_sub(header_row as usize) as u32,
        preview_rows: rows,
        preview_invalidated: false,
        warnings: Vec::new(),
        errors,
    })
}

fn inspect_spreadsheet(
    import_id: Uuid,
    status: ImportBatchStatus,
    detected_format: &str,
    bytes: &[u8],
    request: ImportInspectRequest,
    mapping: &[ImportMappingField],
) -> Result<ImportInspectResponse, ImportParseError> {
    let cursor = Cursor::new(bytes.to_vec());
    let mut workbook =
        open_workbook_auto_from_rs(cursor).map_err(|_| ImportParseError::SpreadsheetReadFailed)?;
    let sheet_names = workbook.sheet_names();
    let selected_sheet = request
        .selected_sheet
        .clone()
        .or_else(|| sheet_names.first().cloned())
        .ok_or(ImportParseError::InvalidSheet)?;
    if !sheet_names.iter().any(|name| name == &selected_sheet) {
        return Err(ImportParseError::InvalidSheet);
    }
    let mut sheet_infos = Vec::new();
    for name in &sheet_names {
        let (row_count, column_count) = workbook
            .worksheet_range(name)
            .map(|range| {
                let (rows, columns) = range.get_size();
                (rows as u32, columns as u32)
            })
            .unwrap_or((0, 0));
        sheet_infos.push(ImportSheetInfo {
            name: name.clone(),
            row_count,
            column_count,
            selected: name == &selected_sheet,
        });
    }
    let range = workbook
        .worksheet_range(&selected_sheet)
        .map_err(|_| ImportParseError::SpreadsheetReadFailed)?;
    let formula_warnings = workbook
        .worksheet_formula(&selected_sheet)
        .map(|formula_range| {
            formula_range
                .rows()
                .enumerate()
                .flat_map(|(row_index, row)| {
                    row.iter()
                        .enumerate()
                        .filter_map(move |(column_index, formula)| {
                            if formula.trim().is_empty() {
                                None
                            } else {
                                Some(parse_message(
                                    Some((row_index + 1) as u32),
                                    Some(format!("column_{}", column_index + 1)),
                                    ImportErrorSeverity::Warning,
                                    "formula_detected",
                                    None,
                                    "检测到公式单元格，系统仅读取缓存值且不执行公式",
                                ))
                            }
                        })
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let records = range
        .rows()
        .map(|row| row.iter().map(cell_to_string).collect::<Vec<_>>())
        .collect::<Vec<_>>();
    let header_row = request.header_row.unwrap_or(1).max(1);
    let columns = columns_from_records(&records, header_row);
    let rows = preview_rows_from_records(&records, header_row, &columns, mapping)?;
    Ok(ImportInspectResponse {
        import_id,
        status,
        detected_format: detected_format.to_string(),
        encoding: ImportDetection {
            value: None,
            confidence: 1.0,
            candidates: Vec::new(),
            overridden: false,
        },
        delimiter: ImportDetection {
            value: None,
            confidence: 1.0,
            candidates: Vec::new(),
            overridden: false,
        },
        sheets: sheet_infos,
        selected_sheet: Some(selected_sheet),
        header_row,
        columns,
        preview_row_count: rows.len() as u32,
        total_rows: records.len().saturating_sub(header_row as usize) as u32,
        preview_rows: rows,
        preview_invalidated: false,
        warnings: formula_warnings,
        errors: Vec::new(),
    })
}

fn collect_formula_warnings<R>(workbook: &mut R, selected_sheet: &str) -> Vec<ImportErrorPreview>
where
    R: Reader<Cursor<Vec<u8>>>,
{
    workbook
        .worksheet_formula(selected_sheet)
        .map(|formula_range| {
            formula_range
                .rows()
                .enumerate()
                .flat_map(|(row_index, row)| {
                    row.iter()
                        .enumerate()
                        .filter_map(move |(column_index, formula)| {
                            if formula.trim().is_empty() {
                                None
                            } else {
                                Some(parse_message(
                                    Some((row_index + 1) as u32),
                                    Some(format!("column_{}", column_index + 1)),
                                    ImportErrorSeverity::Warning,
                                    "formula_detected",
                                    None,
                                    "检测到电子表格公式；仅导入缓存值，不执行公式。",
                                ))
                            }
                        })
                })
                .collect()
        })
        .unwrap_or_default()
}

struct DecodedText {
    text: String,
    detection: ImportDetection,
}

fn decode_text(
    bytes: &[u8],
    override_encoding: Option<&str>,
) -> Result<DecodedText, ImportParseError> {
    if let Some(encoding) = override_encoding {
        let encoding = normalize_encoding(encoding).ok_or(ImportParseError::InvalidEncoding)?;
        let (text, _, had_errors) = encoding.decode(bytes);
        if had_errors && encoding == UTF_8 {
            return Err(ImportParseError::InvalidEncoding);
        }
        return Ok(DecodedText {
            text: text.into_owned(),
            detection: ImportDetection {
                value: Some(encoding_name(encoding).to_string()),
                confidence: 1.0,
                candidates: vec!["utf-8".into(), "gbk".into()],
                overridden: true,
            },
        });
    }
    if let Ok(text) = std::str::from_utf8(bytes) {
        return Ok(DecodedText {
            text: text.trim_start_matches('\u{feff}').to_string(),
            detection: ImportDetection {
                value: Some("utf-8".into()),
                confidence: 0.98,
                candidates: vec!["utf-8".into(), "gbk".into()],
                overridden: false,
            },
        });
    }
    let (text, _, had_errors) = GBK.decode(bytes);
    Ok(DecodedText {
        text: text.into_owned(),
        detection: ImportDetection {
            value: Some("gbk".into()),
            confidence: if had_errors { 0.62 } else { 0.86 },
            candidates: vec!["gbk".into(), "utf-8".into()],
            overridden: false,
        },
    })
}

fn normalize_encoding(value: &str) -> Option<&'static Encoding> {
    match value.trim().to_ascii_lowercase().as_str() {
        "utf-8" | "utf8" => Some(UTF_8),
        "gbk" | "gb2312" | "gb18030" => Some(GBK),
        _ => None,
    }
}

fn encoding_name(encoding: &'static Encoding) -> &'static str {
    if encoding == UTF_8 { "utf-8" } else { "gbk" }
}

fn detect_delimiter(
    text: &str,
    override_delimiter: Option<&str>,
) -> Result<ImportDetection, ImportParseError> {
    if let Some(value) = override_delimiter {
        let delimiter = normalize_delimiter(value).ok_or(ImportParseError::InvalidDelimiter)?;
        return Ok(ImportDetection {
            value: Some(delimiter),
            confidence: 1.0,
            candidates: delimiter_candidates(),
            overridden: true,
        });
    }
    let mut scores = BTreeMap::<String, usize>::new();
    for delimiter in delimiter_candidates() {
        let rows = parse_delimited(text, &delimiter);
        let useful = rows
            .iter()
            .take(20)
            .filter(|row| row.len() > 1 && row.iter().any(|cell| !cell.trim().is_empty()))
            .count();
        let width_bonus = rows.iter().take(20).map(Vec::len).max().unwrap_or(0);
        scores.insert(delimiter, useful.saturating_mul(10) + width_bonus);
    }
    let (delimiter, score) = scores
        .iter()
        .max_by_key(|(_, score)| *score)
        .map(|(delimiter, score)| (delimiter.clone(), *score))
        .unwrap_or_else(|| (",".into(), 0));
    Ok(ImportDetection {
        value: Some(delimiter),
        confidence: if score > 10 { 0.82 } else { 0.45 },
        candidates: delimiter_candidates(),
        overridden: false,
    })
}

fn normalize_delimiter(value: &str) -> Option<String> {
    match value {
        "\\t" | "tab" | "TAB" => Some("\t".into()),
        "," | ";" | "|" | " " | "\t" => Some(value.into()),
        custom if custom.chars().count() == 1 => Some(custom.into()),
        _ => None,
    }
}

fn delimiter_candidates() -> Vec<String> {
    vec![",".into(), "\t".into(), ";".into(), "|".into(), " ".into()]
}

fn parse_delimited(text: &str, delimiter: &str) -> Vec<Vec<String>> {
    let delimiter = delimiter.chars().next().unwrap_or(',');
    text.lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| parse_record(line, delimiter))
        .collect()
}

fn parse_record(line: &str, delimiter: char) -> Vec<String> {
    let mut cells = Vec::new();
    let mut current = String::new();
    let mut chars = line.chars().peekable();
    let mut quoted = false;
    while let Some(ch) = chars.next() {
        match ch {
            '"' if quoted && chars.peek() == Some(&'"') => {
                current.push('"');
                chars.next();
            }
            '"' => quoted = !quoted,
            value if value == delimiter && !quoted => {
                cells.push(std::mem::take(&mut current));
            }
            value => current.push(value),
        }
    }
    cells.push(current);
    cells
}

fn columns_from_records(records: &[Vec<String>], header_row: u32) -> Vec<ImportColumnPreview> {
    let header_index = header_row.saturating_sub(1) as usize;
    let width = records.iter().map(Vec::len).max().unwrap_or(0);
    let headers = records.get(header_index);
    (0..width)
        .map(|index| {
            let name = headers
                .and_then(|row| row.get(index))
                .filter(|value| !value.trim().is_empty())
                .cloned()
                .unwrap_or_else(|| format!("column_{}", index + 1));
            ImportColumnPreview {
                index: (index + 1) as u32,
                name,
            }
        })
        .collect()
}

fn preview_rows_from_records(
    records: &[Vec<String>],
    header_row: u32,
    columns: &[ImportColumnPreview],
    mapping: &[ImportMappingField],
) -> Result<Vec<ImportPreviewRow>, ImportParseError> {
    rows_from_records(records, header_row, columns, mapping, Some(50))
}

fn rows_from_records(
    records: &[Vec<String>],
    header_row: u32,
    columns: &[ImportColumnPreview],
    mapping: &[ImportMappingField],
    limit: Option<usize>,
) -> Result<Vec<ImportPreviewRow>, ImportParseError> {
    let data_start = header_row as usize;
    records
        .iter()
        .enumerate()
        .skip(data_start)
        .take(limit.unwrap_or(usize::MAX))
        .map(|(row_index, row)| {
            let cells = columns
                .iter()
                .enumerate()
                .map(
                    |(index, column)| -> Result<ImportPreviewCell, ImportParseError> {
                        let raw_value = row.get(index).cloned().unwrap_or_default();
                        let mapping_field = mapping
                            .iter()
                            .find(|field| field.source_column == column.name);
                        let target_field = mapping_field.map(|field| field.target_field.clone());
                        let normalized_value = apply_transform(
                            &raw_value,
                            mapping_field.and_then(|field| field.transform.as_deref()),
                        )?;
                        Ok(ImportPreviewCell {
                            column: column.name.clone(),
                            normalized_value,
                            raw_value,
                            target_field,
                            errors: Vec::new(),
                            warnings: Vec::new(),
                        })
                    },
                )
                .collect::<Result<Vec<_>, _>>()?;
            Ok(ImportPreviewRow {
                row_number: (row_index + 1) as u32,
                cells,
                errors: Vec::new(),
                warnings: Vec::new(),
            })
        })
        .collect()
}

fn apply_transform(
    raw_value: &str,
    transform: Option<&str>,
) -> Result<Option<String>, ImportParseError> {
    let trimmed = raw_value.trim();
    match transform {
        None | Some("trim") => Ok(Some(trimmed.to_string())),
        Some("empty_to_null") => Ok((!trimmed.is_empty()).then(|| trimmed.to_string())),
        Some("date_ymd") => Ok(Some(normalize_date_ymd(trimmed))),
        Some("decimal") => Ok(Some(normalize_decimal(trimmed))),
        Some("integer") => Ok(Some(normalize_integer(trimmed))),
        Some(_) => Err(ImportParseError::UnsupportedTransform),
    }
}

fn normalize_date_ymd(value: &str) -> String {
    let parsed = if value.len() == 8 && value.bytes().all(|byte| byte.is_ascii_digit()) {
        time::Date::parse(
            value,
            time::macros::format_description!("[year][month][day]"),
        )
    } else if value.contains('/') {
        time::Date::parse(
            value,
            time::macros::format_description!("[year]/[month]/[day]"),
        )
    } else {
        time::Date::parse(
            value,
            time::macros::format_description!("[year]-[month]-[day]"),
        )
    };
    parsed
        .map(|date| {
            date.format(time::macros::format_description!("[year]-[month]-[day]"))
                .expect("static date format is valid")
        })
        .unwrap_or_else(|_| value.to_string())
}

fn normalize_decimal(value: &str) -> String {
    normalize_grouped_number(value)
        .filter(|value| valid_decimal(value))
        .unwrap_or_else(|| value.to_string())
}

fn normalize_integer(value: &str) -> String {
    let Some(mut normalized) = normalize_grouped_number(value) else {
        return value.to_string();
    };
    if let Some((whole, fraction)) = normalized.split_once('.') {
        if fraction.is_empty() || fraction.bytes().any(|byte| byte != b'0') {
            return value.to_string();
        }
        normalized = whole.to_string();
    }
    if !valid_integer(&normalized) {
        return value.to_string();
    }
    let (sign, digits) = normalized
        .strip_prefix('-')
        .map_or(("", normalized.as_str()), |digits| ("-", digits));
    let digits = digits.trim_start_matches('0');
    format!("{sign}{}", if digits.is_empty() { "0" } else { digits })
}

fn normalize_grouped_number(value: &str) -> Option<String> {
    let (sign, unsigned) = value
        .strip_prefix('-')
        .map_or(("", value), |value| ("-", value));
    let (integer, fraction) = unsigned
        .split_once('.')
        .map_or((unsigned, None), |(whole, fraction)| {
            (whole, Some(fraction))
        });
    let normalized_integer = if integer.contains(',') {
        let groups = integer.split(',').collect::<Vec<_>>();
        if groups.is_empty()
            || groups[0].is_empty()
            || groups[0].len() > 3
            || groups[0].bytes().any(|byte| !byte.is_ascii_digit())
            || groups[1..]
                .iter()
                .any(|group| group.len() != 3 || group.bytes().any(|byte| !byte.is_ascii_digit()))
        {
            return None;
        }
        groups.concat()
    } else {
        integer.to_string()
    };
    Some(match fraction {
        Some(fraction) => format!("{sign}{normalized_integer}.{fraction}"),
        None => format!("{sign}{normalized_integer}"),
    })
}

fn valid_decimal(value: &str) -> bool {
    let unsigned = value.strip_prefix('-').unwrap_or(value);
    let mut parts = unsigned.split('.');
    let whole = parts.next().unwrap_or_default();
    let fraction = parts.next();
    !whole.is_empty()
        && whole.bytes().all(|byte| byte.is_ascii_digit())
        && fraction.is_none_or(|digits| {
            !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit())
        })
        && parts.next().is_none()
}

fn valid_integer(value: &str) -> bool {
    let unsigned = value.strip_prefix('-').unwrap_or(value);
    !unsigned.is_empty() && unsigned.bytes().all(|byte| byte.is_ascii_digit())
}

fn cell_to_string(cell: &Data) -> String {
    match cell {
        Data::Empty => String::new(),
        Data::String(value) => value.clone(),
        Data::Float(value) => value.to_string(),
        Data::Int(value) => value.to_string(),
        Data::Bool(value) => value.to_string(),
        Data::DateTime(value) => value.to_string(),
        Data::DateTimeIso(value) | Data::DurationIso(value) => value.clone(),
        Data::Error(value) => format!("{value:?}"),
    }
}

fn parse_message(
    row_number: Option<u32>,
    field_name: Option<String>,
    severity: ImportErrorSeverity,
    error_code: &str,
    raw_value: Option<String>,
    message: &str,
) -> ImportErrorPreview {
    ImportErrorPreview {
        row_number,
        field_name,
        severity,
        error_code: error_code.to_string(),
        raw_value,
        message: message.to_string(),
    }
}

fn validate_filename(filename: &str) -> Result<(), UploadValidationError> {
    let trimmed = filename.trim();
    if trimmed.is_empty() {
        return Err(UploadValidationError::MissingFilename);
    }
    if filename.len() > 255
        || filename != trimmed
        || filename.starts_with('.')
        || filename.contains('/')
        || filename.contains('\\')
        || filename.contains('\0')
        || filename.chars().any(char::is_control)
        || filename.split('.').any(|part| part == "..")
    {
        return Err(UploadValidationError::DangerousFilename);
    }
    let stem = Path::new(filename)
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if matches!(
        stem.as_str(),
        "con"
            | "prn"
            | "aux"
            | "nul"
            | "com1"
            | "com2"
            | "com3"
            | "com4"
            | "com5"
            | "com6"
            | "com7"
            | "com8"
            | "com9"
            | "lpt1"
            | "lpt2"
            | "lpt3"
            | "lpt4"
            | "lpt5"
            | "lpt6"
            | "lpt7"
            | "lpt8"
            | "lpt9"
    ) {
        return Err(UploadValidationError::DangerousFilename);
    }
    Ok(())
}

fn mime_matches(format: AcceptedFileFormat, mime: &str) -> bool {
    match format {
        AcceptedFileFormat::Txt => {
            matches!(mime, "text/plain" | "application/octet-stream")
        }
        AcceptedFileFormat::Csv => matches!(
            mime,
            "text/csv" | "application/csv" | "text/plain" | "application/octet-stream"
        ),
        AcceptedFileFormat::Xls => {
            matches!(
                mime,
                "application/vnd.ms-excel" | "application/octet-stream"
            )
        }
        AcceptedFileFormat::Xlsx => matches!(
            mime,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                | "application/zip"
                | "application/octet-stream"
        ),
    }
}

fn validate_magic(format: AcceptedFileFormat, prefix: &[u8]) -> Result<(), UploadValidationError> {
    const OLE: &[u8] = &[0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1];
    let is_zip = prefix.starts_with(b"PK\x03\x04")
        || prefix.starts_with(b"PK\x05\x06")
        || prefix.starts_with(b"PK\x07\x08");
    match format {
        AcceptedFileFormat::Txt | AcceptedFileFormat::Csv => {
            if prefix.starts_with(OLE) || is_zip || prefix.contains(&0) {
                Err(UploadValidationError::MimeMismatch)
            } else {
                Ok(())
            }
        }
        AcceptedFileFormat::Xls if prefix.starts_with(OLE) => Ok(()),
        AcceptedFileFormat::Xlsx if is_zip => Ok(()),
        AcceptedFileFormat::Xls | AcceptedFileFormat::Xlsx => {
            Err(UploadValidationError::MimeMismatch)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn validate(
        name: &str,
        mime: &str,
        bytes: &[u8],
    ) -> Result<ValidatedUpload, UploadValidationError> {
        let mut validator =
            UploadValidator::new(UploadPolicy { max_bytes: 1024 }, name, Some(mime))?;
        validator.observe(bytes)?;
        validator.finish()
    }

    #[test]
    fn accepts_minimal_supported_formats_without_parsing_content() {
        assert!(validate("sample.txt", "text/plain", b"x").is_ok());
        assert!(validate("sample.csv", "text/csv", b"a,b\n1,2\n").is_ok());
        assert!(
            validate(
                "sample.xls",
                "application/vnd.ms-excel",
                b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
            )
            .is_ok()
        );
        assert!(
            validate(
                "sample.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"PK\x03\x04"
            )
            .is_ok()
        );
    }

    #[test]
    fn rejects_empty_oversized_dangerous_and_conflicting_files() {
        assert_eq!(
            validate("empty.txt", "text/plain", b"").err(),
            Some(UploadValidationError::EmptyFile)
        );
        let mut oversized =
            UploadValidator::new(UploadPolicy { max_bytes: 1 }, "big.csv", Some("text/csv"))
                .unwrap();
        assert_eq!(
            oversized.observe(b"12"),
            Err(UploadValidationError::FileTooLarge)
        );
        assert_eq!(
            UploadValidator::new(
                UploadPolicy { max_bytes: 10 },
                "../escape.csv",
                Some("text/csv")
            )
            .err(),
            Some(UploadValidationError::DangerousFilename)
        );
        assert_eq!(
            validate("fake.xlsx", "application/zip", b"not a zip").err(),
            Some(UploadValidationError::MimeMismatch)
        );
        assert_eq!(
            validate("fake.csv", "text/csv", b"PK\x03\x04").err(),
            Some(UploadValidationError::MimeMismatch)
        );
    }

    #[test]
    fn same_content_validation_is_deterministic() {
        let first = validate("one.csv", "text/csv", b"a,b\n1,2").unwrap();
        let second = validate("two.csv", "text/csv", b"a,b\n1,2").unwrap();
        assert_eq!(first.size_bytes, second.size_bytes);
        assert_eq!(first.format, second.format);
    }

    #[test]
    fn inspects_csv_with_detected_delimiter_and_preview_limit() {
        let mut rows = String::from("a,b\n");
        for index in 0..60 {
            rows.push_str(&format!("{index},{}\n", index + 1));
        }
        let result = inspect_content(
            uuid::Uuid::now_v7(),
            ImportBatchStatus::Uploaded,
            "csv",
            rows.as_bytes(),
            ImportInspectRequest {
                encoding: None,
                delimiter: None,
                selected_sheet: None,
                header_row: Some(1),
            },
            &[],
        )
        .unwrap();
        assert_eq!(result.delimiter.value.as_deref(), Some(","));
        assert_eq!(result.columns.len(), 2);
        assert_eq!(result.preview_rows.len(), 50);
    }

    #[test]
    fn supports_manual_delimiter_override() {
        let result = inspect_content(
            uuid::Uuid::now_v7(),
            ImportBatchStatus::Uploaded,
            "txt",
            b"a|b\n1|2\n",
            ImportInspectRequest {
                encoding: Some("utf-8".into()),
                delimiter: Some("|".into()),
                selected_sheet: None,
                header_row: Some(1),
            },
            &[ImportMappingField {
                source_column: "a".into(),
                target_field: "alpha".into(),
                transform: None,
            }],
        )
        .unwrap();
        assert!(result.delimiter.overridden);
        assert_eq!(
            result.preview_rows[0].cells[0].target_field.as_deref(),
            Some("alpha")
        );
    }

    #[test]
    fn full_parse_is_not_limited_to_preview_rows() {
        let mut csv = String::from("date,code,name\n");
        for index in 0..75 {
            csv.push_str(&format!("2026-07-25,C{index},Name {index}\n"));
        }
        let mapping = vec![
            ImportMappingField {
                source_column: "date".into(),
                target_field: "trade_date".into(),
                transform: None,
            },
            ImportMappingField {
                source_column: "code".into(),
                target_field: "code".into(),
                transform: None,
            },
        ];
        let rows = parse_all_content(
            "csv",
            csv.as_bytes(),
            ImportPreviewRequest {
                encoding: Some("utf-8".into()),
                delimiter: Some(",".into()),
                selected_sheet: None,
                header_row: Some(1),
            },
            &mapping,
            DEFAULT_IMPORT_MAX_ROWS,
        )
        .unwrap();
        assert_eq!(rows.len(), 75);
        assert_eq!(rows.last().unwrap().row_number, 76);
        let staging = rows
            .iter()
            .map(|row| {
                let normalized_values = row
                    .cells
                    .iter()
                    .map(|cell| (cell.column.clone(), json!(cell.normalized_value)))
                    .collect();
                let target_fields = row
                    .cells
                    .iter()
                    .map(|cell| {
                        (
                            cell.column.clone(),
                            json!(cell.target_field.as_deref().unwrap_or("")),
                        )
                    })
                    .collect();
                crate::import_jobs::StagingRowInput {
                    id: Uuid::now_v7(),
                    row_number: row.row_number,
                    normalized_values: serde_json::Value::Object(normalized_values),
                    target_fields: serde_json::Value::Object(target_fields),
                    preview_warnings: json!([]),
                }
            })
            .collect();
        let validated = crate::import_jobs::validate_staging_rows("generic", staging).unwrap();
        assert_eq!(validated.rows.len(), 75);
        assert_eq!(
            crate::import_jobs::select_file_candidates(
                &validated.rows,
                domain::import::ImportConflictPolicy::Skip
            )
            .len(),
            75,
            "all rows must reach formal import candidates"
        );
    }

    #[test]
    fn deterministic_transforms_match_preview_and_full_validation_parse() {
        let mapping = vec![
            ImportMappingField {
                source_column: "date".into(),
                target_field: "trade_date".into(),
                transform: Some("date_ymd".into()),
            },
            ImportMappingField {
                source_column: "amount".into(),
                target_field: "amount".into(),
                transform: Some("decimal".into()),
            },
            ImportMappingField {
                source_column: "note".into(),
                target_field: "note".into(),
                transform: Some("empty_to_null".into()),
            },
            ImportMappingField {
                source_column: "quantity".into(),
                target_field: "quantity".into(),
                transform: Some("integer".into()),
            },
            ImportMappingField {
                source_column: "name".into(),
                target_field: "name".into(),
                transform: Some("trim".into()),
            },
        ];
        let csv = b"date,amount,note,quantity,name\n20260725,\"1,234.50\",   ,00042,  Alice  \n";
        let request = ImportPreviewRequest {
            encoding: Some("utf-8".into()),
            delimiter: Some(",".into()),
            selected_sheet: None,
            header_row: Some(1),
        };
        let preview = preview_content(
            Uuid::now_v7(),
            ImportBatchStatus::Mapped,
            "csv",
            csv,
            request.clone(),
            &mapping,
        )
        .unwrap();
        let full =
            parse_all_content("csv", csv, request, &mapping, DEFAULT_IMPORT_MAX_ROWS).unwrap();
        assert_eq!(preview.preview_rows, full);
        let cells = &full[0].cells;
        assert_eq!(cells[0].normalized_value.as_deref(), Some("2026-07-25"));
        assert_eq!(cells[1].normalized_value.as_deref(), Some("1234.50"));
        assert_eq!(cells[2].normalized_value, None);
        assert_eq!(cells[3].normalized_value.as_deref(), Some("42"));
        assert_eq!(cells[4].normalized_value.as_deref(), Some("Alice"));
        assert_eq!(cells[4].raw_value, "  Alice  ");
    }

    #[test]
    fn parameterized_or_unknown_transform_is_never_silently_ignored() {
        let error = preview_content(
            Uuid::now_v7(),
            ImportBatchStatus::Mapped,
            "csv",
            b"note\nexchange\n",
            ImportPreviewRequest {
                encoding: Some("utf-8".into()),
                delimiter: Some(",".into()),
                selected_sheet: None,
                header_row: Some(1),
            },
            &[ImportMappingField {
                source_column: "note".into(),
                target_field: "note".into(),
                transform: Some("enum_map".into()),
            }],
        )
        .unwrap_err();
        assert_eq!(error, ImportParseError::UnsupportedTransform);
    }
}
