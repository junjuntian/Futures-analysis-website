use std::{env, path::Path};

pub const DEFAULT_IMPORT_MAX_BYTES: u64 = 50 * 1024 * 1024;

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
}
