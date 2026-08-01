use async_trait::async_trait;
use rand_core::{OsRng, RngCore};
use sha2::{Digest, Sha256};
use std::{
    io,
    path::{Component, Path, PathBuf},
};
use tokio::{
    fs::{self, File},
    io::{AsyncReadExt, AsyncWriteExt},
};
use uuid::Uuid;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredObject {
    pub object_key: String,
    pub sha256: String,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScannedObject {
    pub object_key: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub modified_unix_seconds: i64,
}

#[derive(Debug, thiserror::Error)]
pub enum ObjectStorageError {
    #[error("invalid object key")]
    InvalidObjectKey,
    #[error("object storage I/O failed")]
    Io(#[from] io::Error),
}

#[async_trait]
pub trait ObjectUpload: Send {
    fn object_key(&self) -> &str;
    async fn write_chunk(&mut self, chunk: &[u8]) -> Result<(), ObjectStorageError>;
    async fn commit(self: Box<Self>) -> Result<StoredObject, ObjectStorageError>;
    async fn abort(self: Box<Self>) -> Result<(), ObjectStorageError>;
}

#[async_trait]
pub trait ObjectStorage: Send + Sync {
    async fn begin_upload(
        &self,
        workspace_id: Uuid,
    ) -> Result<Box<dyn ObjectUpload>, ObjectStorageError>;
    async fn read(&self, object_key: &str, max_bytes: u64) -> Result<Vec<u8>, ObjectStorageError>;
}

#[derive(Debug, Clone)]
pub struct LocalObjectStorage {
    root: PathBuf,
}

impl LocalObjectStorage {
    pub async fn new(root: impl Into<PathBuf>) -> Result<Self, ObjectStorageError> {
        let root = root.into();
        fs::create_dir_all(root.join(".tmp")).await?;
        fs::create_dir_all(root.join("objects")).await?;
        fs::create_dir_all(root.join("quarantine")).await?;
        let root = fs::canonicalize(root).await?;
        Ok(Self { root })
    }

    pub fn object_path(&self, object_key: &str) -> Result<PathBuf, ObjectStorageError> {
        validate_object_key(object_key)?;
        Ok(self.root.join(object_key))
    }

    fn random_object_key(workspace_id: Uuid) -> String {
        let mut random = [0_u8; 32];
        OsRng.fill_bytes(&mut random);
        let encoded = encode_hex(&random);
        format!(
            "objects/{workspace_id}/{}/{}/{}",
            &encoded[..2],
            &encoded[2..4],
            encoded
        )
    }

    pub fn workspace_object_prefix(workspace_id: Uuid) -> String {
        format!("objects/{workspace_id}/")
    }

    pub fn workspace_temporary_prefix(workspace_id: Uuid) -> String {
        format!(".tmp/{workspace_id}/")
    }

    pub fn root_fingerprint(&self) -> String {
        encode_hex(&Sha256::digest(self.root.to_string_lossy().as_bytes()))
    }

    pub fn belongs_to_workspace(object_key: &str, workspace_id: Uuid) -> bool {
        object_key.starts_with(&Self::workspace_object_prefix(workspace_id))
            || object_key.starts_with(&Self::workspace_temporary_prefix(workspace_id))
    }

    pub fn is_workspace_quarantine_key(object_key: &str, workspace_id: Uuid) -> bool {
        object_key.starts_with(&format!("quarantine/{workspace_id}/"))
    }

    pub async fn inspect(
        &self,
        object_key: &str,
    ) -> Result<Option<ScannedObject>, ObjectStorageError> {
        let path = self.secure_existing_path(object_key).await?;
        let Some(path) = path else {
            return Ok(None);
        };
        Ok(Some(scan_file(&self.root, path).await?))
    }

    pub async fn scan_workspace(
        &self,
        workspace_id: Uuid,
    ) -> Result<Vec<ScannedObject>, ObjectStorageError> {
        let mut objects = Vec::new();
        for prefix in [
            format!("objects/{workspace_id}"),
            format!(".tmp/{workspace_id}"),
        ] {
            let directory = self.object_path(&prefix)?;
            match fs::symlink_metadata(&directory).await {
                Ok(_) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error.into()),
            }
            let mut pending = vec![directory];
            while let Some(directory) = pending.pop() {
                let metadata = fs::symlink_metadata(&directory).await?;
                if metadata.file_type().is_symlink() {
                    return Err(ObjectStorageError::InvalidObjectKey);
                }
                let mut entries = fs::read_dir(directory).await?;
                while let Some(entry) = entries.next_entry().await? {
                    let metadata = fs::symlink_metadata(entry.path()).await?;
                    if metadata.file_type().is_symlink() {
                        return Err(ObjectStorageError::InvalidObjectKey);
                    }
                    if metadata.is_dir() {
                        pending.push(entry.path());
                    } else if metadata.is_file() {
                        objects.push(scan_file(&self.root, entry.path()).await?);
                    }
                }
            }
        }
        objects.sort_by(|left, right| left.object_key.cmp(&right.object_key));
        Ok(objects)
    }

    pub async fn quarantine(
        &self,
        workspace_id: Uuid,
        finding_id: Uuid,
        source_object_key: &str,
        expected_sha256: &str,
        expected_size_bytes: u64,
    ) -> Result<ScannedObject, ObjectStorageError> {
        if !Self::belongs_to_workspace(source_object_key, workspace_id) {
            return Err(ObjectStorageError::InvalidObjectKey);
        }
        let target_key = format!("quarantine/{workspace_id}/{finding_id}");
        if let Some(existing) = self.inspect(&target_key).await? {
            if existing.sha256 == expected_sha256 && existing.size_bytes == expected_size_bytes {
                return Ok(existing);
            }
            return Err(ObjectStorageError::InvalidObjectKey);
        }
        let source = self
            .secure_existing_path(source_object_key)
            .await?
            .ok_or_else(|| ObjectStorageError::Io(io::ErrorKind::NotFound.into()))?;
        let observed = scan_file(&self.root, source.clone()).await?;
        if observed.sha256 != expected_sha256 || observed.size_bytes != expected_size_bytes {
            return Err(ObjectStorageError::InvalidObjectKey);
        }
        let target = self.object_path(&target_key)?;
        ensure_secure_parent(&self.root, &target).await?;
        fs::rename(source, &target).await?;
        scan_file(&self.root, target).await
    }

    async fn secure_existing_path(
        &self,
        object_key: &str,
    ) -> Result<Option<PathBuf>, ObjectStorageError> {
        let path = self.object_path(object_key)?;
        let mut current = self.root.clone();
        for component in Path::new(object_key).components() {
            let Component::Normal(component) = component else {
                return Err(ObjectStorageError::InvalidObjectKey);
            };
            current.push(component);
            match fs::symlink_metadata(&current).await {
                Ok(metadata) if metadata.file_type().is_symlink() => {
                    return Err(ObjectStorageError::InvalidObjectKey);
                }
                Ok(_) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
                Err(error) => return Err(error.into()),
            }
        }
        Ok(Some(path))
    }
}

#[async_trait]
impl ObjectStorage for LocalObjectStorage {
    async fn begin_upload(
        &self,
        workspace_id: Uuid,
    ) -> Result<Box<dyn ObjectUpload>, ObjectStorageError> {
        let object_key = Self::random_object_key(workspace_id);
        let final_path = self.object_path(&object_key)?;
        let temporary_directory = self.root.join(".tmp").join(workspace_id.to_string());
        ensure_secure_parent(&self.root, &temporary_directory.join("upload")).await?;
        let temporary_path = temporary_directory.join(format!(
            "{}.upload",
            Self::random_object_key(workspace_id).replace('/', "")
        ));
        let file = File::create(&temporary_path).await?;
        Ok(Box::new(LocalObjectUpload {
            object_key,
            root: self.root.clone(),
            temporary_path,
            final_path,
            file: Some(file),
            hasher: Sha256::new(),
            size_bytes: 0,
            committed: false,
        }))
    }

    async fn read(&self, object_key: &str, max_bytes: u64) -> Result<Vec<u8>, ObjectStorageError> {
        let path = self
            .secure_existing_path(object_key)
            .await?
            .ok_or_else(|| ObjectStorageError::Io(io::ErrorKind::NotFound.into()))?;
        let metadata = fs::symlink_metadata(&path).await?;
        if !metadata.is_file() || metadata.file_type().is_symlink() {
            return Err(ObjectStorageError::InvalidObjectKey);
        }
        if metadata.len() > max_bytes {
            return Err(ObjectStorageError::Io(io::Error::other(
                "object exceeds read limit",
            )));
        }
        Ok(fs::read(path).await?)
    }
}

async fn ensure_secure_parent(root: &Path, target: &Path) -> Result<(), ObjectStorageError> {
    let relative = target
        .strip_prefix(root)
        .map_err(|_| ObjectStorageError::InvalidObjectKey)?;
    let mut current = root.to_path_buf();
    let components = relative.components().collect::<Vec<_>>();
    for component in components.iter().take(components.len().saturating_sub(1)) {
        let Component::Normal(component) = component else {
            return Err(ObjectStorageError::InvalidObjectKey);
        };
        current.push(component);
        match fs::symlink_metadata(&current).await {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return Err(ObjectStorageError::InvalidObjectKey);
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                fs::create_dir(&current).await?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    let canonical_parent = fs::canonicalize(
        target
            .parent()
            .ok_or(ObjectStorageError::InvalidObjectKey)?,
    )
    .await?;
    if !canonical_parent.starts_with(root) {
        return Err(ObjectStorageError::InvalidObjectKey);
    }
    Ok(())
}

async fn scan_file(root: &Path, path: PathBuf) -> Result<ScannedObject, ObjectStorageError> {
    let metadata = fs::symlink_metadata(&path).await?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(ObjectStorageError::InvalidObjectKey);
    }
    let relative = path
        .strip_prefix(root)
        .map_err(|_| ObjectStorageError::InvalidObjectKey)?;
    let object_key = relative
        .components()
        .map(|component| component.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/");
    validate_object_key(&object_key)?;
    let mut file = File::open(&path).await?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer).await?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    let modified_unix_seconds = metadata
        .modified()?
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| ObjectStorageError::Io(io::Error::other("invalid modified time")))?
        .as_secs() as i64;
    Ok(ScannedObject {
        object_key,
        sha256: encode_hex(&hasher.finalize()),
        size_bytes: metadata.len(),
        modified_unix_seconds,
    })
}

struct LocalObjectUpload {
    object_key: String,
    root: PathBuf,
    temporary_path: PathBuf,
    final_path: PathBuf,
    file: Option<File>,
    hasher: Sha256,
    size_bytes: u64,
    committed: bool,
}

#[async_trait]
impl ObjectUpload for LocalObjectUpload {
    fn object_key(&self) -> &str {
        &self.object_key
    }

    async fn write_chunk(&mut self, chunk: &[u8]) -> Result<(), ObjectStorageError> {
        let file = self.file.as_mut().ok_or_else(|| {
            ObjectStorageError::Io(io::Error::other("upload is no longer writable"))
        })?;
        file.write_all(chunk).await?;
        self.hasher.update(chunk);
        self.size_bytes = self
            .size_bytes
            .checked_add(chunk.len() as u64)
            .ok_or_else(|| ObjectStorageError::Io(io::Error::other("object size overflow")))?;
        Ok(())
    }

    async fn commit(mut self: Box<Self>) -> Result<StoredObject, ObjectStorageError> {
        let file = self.file.take().ok_or_else(|| {
            ObjectStorageError::Io(io::Error::other("upload is no longer writable"))
        })?;
        file.sync_all().await?;
        drop(file);
        ensure_secure_parent(&self.root, &self.final_path).await?;
        fs::rename(&self.temporary_path, &self.final_path).await?;
        self.committed = true;
        Ok(StoredObject {
            object_key: self.object_key.clone(),
            sha256: encode_hex(&self.hasher.clone().finalize()),
            size_bytes: self.size_bytes,
        })
    }

    async fn abort(mut self: Box<Self>) -> Result<(), ObjectStorageError> {
        self.file.take();
        match fs::remove_file(&self.temporary_path).await {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }
}

impl Drop for LocalObjectUpload {
    fn drop(&mut self) {
        if !self.committed {
            self.file.take();
            let _ = std::fs::remove_file(&self.temporary_path);
        }
    }
}

fn validate_object_key(object_key: &str) -> Result<(), ObjectStorageError> {
    let path = Path::new(object_key);
    if object_key.is_empty()
        || path.is_absolute()
        || path.components().any(|component| {
            !matches!(component, Component::Normal(_)) || component.as_os_str().is_empty()
        })
    {
        return Err(ObjectStorageError::InvalidObjectKey);
    }
    Ok(())
}

fn encode_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_root() -> PathBuf {
        std::env::temp_dir().join(format!("futures-object-storage-{}", uuid::Uuid::now_v7()))
    }

    #[tokio::test]
    async fn streams_hash_and_size_then_atomically_persists() {
        let root = test_root();
        let storage = LocalObjectStorage::new(&root).await.unwrap();
        let workspace_id = Uuid::now_v7();
        let mut upload = storage.begin_upload(workspace_id).await.unwrap();
        upload.write_chunk(b"hello ").await.unwrap();
        upload.write_chunk(b"world").await.unwrap();
        let stored = upload.commit().await.unwrap();

        assert_eq!(stored.size_bytes, 11);
        assert_eq!(
            stored.sha256,
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        );
        assert_eq!(
            fs::read(storage.object_path(&stored.object_key).unwrap())
                .await
                .unwrap(),
            b"hello world"
        );
        assert!(!stored.object_key.contains("hello"));
        assert!(
            stored
                .object_key
                .starts_with(&format!("objects/{workspace_id}/"))
        );
        let _ = fs::remove_dir_all(root).await;
    }

    #[tokio::test]
    async fn aborted_and_dropped_uploads_leave_no_temporary_file() {
        let root = test_root();
        let storage = LocalObjectStorage::new(&root).await.unwrap();
        let workspace_id = Uuid::now_v7();
        let mut upload = storage.begin_upload(workspace_id).await.unwrap();
        upload.write_chunk(b"partial").await.unwrap();
        upload.abort().await.unwrap();
        assert!(
            storage
                .scan_workspace(workspace_id)
                .await
                .unwrap()
                .is_empty()
        );

        let mut dropped = storage.begin_upload(workspace_id).await.unwrap();
        dropped.write_chunk(b"partial").await.unwrap();
        drop(dropped);
        assert!(
            storage
                .scan_workspace(workspace_id)
                .await
                .unwrap()
                .is_empty()
        );
        let _ = fs::remove_dir_all(root).await;
    }

    #[tokio::test]
    async fn same_content_uses_independent_keys_with_identical_hashes() {
        let root = test_root();
        let storage = LocalObjectStorage::new(&root).await.unwrap();
        let workspace_id = Uuid::now_v7();
        let mut first = storage.begin_upload(workspace_id).await.unwrap();
        first.write_chunk(b"same content").await.unwrap();
        let first = first.commit().await.unwrap();
        let mut second = storage.begin_upload(workspace_id).await.unwrap();
        second.write_chunk(b"same content").await.unwrap();
        let second = second.commit().await.unwrap();

        assert_ne!(first.object_key, second.object_key);
        assert_eq!(first.sha256, second.sha256);
        assert_eq!(first.size_bytes, second.size_bytes);
        let _ = fs::remove_dir_all(root).await;
    }

    #[tokio::test]
    async fn renamed_object_remains_scannable_after_lost_registration_outcome() {
        let root = test_root();
        let storage = LocalObjectStorage::new(&root).await.unwrap();
        let workspace_id = Uuid::now_v7();
        let mut upload = storage.begin_upload(workspace_id).await.unwrap();
        upload
            .write_chunk(b"rename completed before database failure")
            .await
            .unwrap();
        let committed = upload.commit().await.unwrap();

        // Simulate both a database registration failure and a caller that never
        // receives the commit response. Governance must still observe the
        // renamed object; product code has no physical-delete recovery path.
        let observed = storage.scan_workspace(workspace_id).await.unwrap();
        assert_eq!(observed.len(), 1);
        assert_eq!(observed[0].object_key, committed.object_key);
        assert_eq!(observed[0].sha256, committed.sha256);
        assert_eq!(observed[0].size_bytes, committed.size_bytes);
        let _ = fs::remove_dir_all(root).await;
    }

    #[tokio::test]
    async fn reads_persisted_object_with_size_guard() {
        let root = test_root();
        let storage = LocalObjectStorage::new(&root).await.unwrap();
        let mut upload = storage.begin_upload(Uuid::now_v7()).await.unwrap();
        upload.write_chunk(b"abc").await.unwrap();
        let stored = upload.commit().await.unwrap();

        assert_eq!(storage.read(&stored.object_key, 3).await.unwrap(), b"abc");
        assert!(storage.read(&stored.object_key, 2).await.is_err());
        let _ = fs::remove_dir_all(root).await;
    }

    #[test]
    fn rejects_path_traversal_keys() {
        assert!(validate_object_key("../outside").is_err());
        assert!(validate_object_key("/absolute").is_err());
        assert!(validate_object_key("objects/../../outside").is_err());
    }

    #[test]
    fn runtime_storage_contract_has_no_physical_delete_capability() {
        let source = include_str!("object_storage.rs");
        let contract = source
            .split("pub trait ObjectStorage")
            .nth(1)
            .unwrap()
            .split("#[derive(Debug, Clone)]")
            .next()
            .unwrap();
        assert!(!contract.contains("delete"));
        let read = source
            .split("async fn read(&self")
            .nth(2)
            .unwrap()
            .split("async fn scan_file")
            .next()
            .unwrap();
        assert!(read.contains("secure_existing_path"));
        let quarantine = source
            .split("pub async fn quarantine")
            .nth(1)
            .unwrap()
            .split("async fn secure_existing_path")
            .next()
            .unwrap();
        assert!(quarantine.contains("secure_existing_path"));
        assert!(quarantine.contains("ensure_secure_parent"));
        let commit = source
            .split("async fn commit")
            .nth(2)
            .unwrap()
            .split("async fn abort")
            .next()
            .unwrap();
        assert!(commit.contains("ensure_secure_parent"));
    }

    #[tokio::test]
    async fn scan_is_workspace_scoped_and_quarantine_is_idempotent_without_delete() {
        let root = test_root();
        let storage = LocalObjectStorage::new(&root).await.unwrap();
        let first_workspace = Uuid::now_v7();
        let second_workspace = Uuid::now_v7();
        let mut first = storage.begin_upload(first_workspace).await.unwrap();
        first.write_chunk(b"orphan").await.unwrap();
        let first = first.commit().await.unwrap();
        let mut second = storage.begin_upload(second_workspace).await.unwrap();
        second.write_chunk(b"private").await.unwrap();
        let second = second.commit().await.unwrap();

        let visible = storage.scan_workspace(first_workspace).await.unwrap();
        assert_eq!(visible.len(), 1);
        assert_eq!(visible[0].object_key, first.object_key);
        assert!(
            !visible
                .iter()
                .any(|entry| entry.object_key == second.object_key)
        );

        let finding_id = Uuid::now_v7();
        let quarantined = storage
            .quarantine(
                first_workspace,
                finding_id,
                &first.object_key,
                &first.sha256,
                first.size_bytes,
            )
            .await
            .unwrap();
        let replayed = storage
            .quarantine(
                first_workspace,
                finding_id,
                &first.object_key,
                &first.sha256,
                first.size_bytes,
            )
            .await
            .unwrap();
        assert_eq!(quarantined, replayed);
        assert!(
            quarantined
                .object_key
                .starts_with(&format!("quarantine/{first_workspace}/"))
        );
        assert!(storage.inspect(&first.object_key).await.unwrap().is_none());
        assert!(storage.inspect(&second.object_key).await.unwrap().is_some());
        let _ = fs::remove_dir_all(root).await;
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn scan_rejects_symbolic_links() {
        use std::os::unix::fs::symlink;

        let root = test_root();
        let workspace_id = Uuid::now_v7();
        let storage = LocalObjectStorage::new(&root).await.unwrap();
        let outside = test_root();
        fs::create_dir_all(&outside).await.unwrap();
        std::fs::write(outside.join("secret"), b"secret").unwrap();
        let workspace_root = root.join("objects").join(workspace_id.to_string());
        fs::create_dir_all(&workspace_root).await.unwrap();
        symlink(&outside, workspace_root.join("escape")).unwrap();
        assert!(storage.scan_workspace(workspace_id).await.is_err());
        let _ = fs::remove_dir_all(root).await;
        let _ = fs::remove_dir_all(outside).await;
    }
}
