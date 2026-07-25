use async_trait::async_trait;
use rand_core::{OsRng, RngCore};
use sha2::{Digest, Sha256};
use std::{
    io,
    path::{Component, Path, PathBuf},
};
use tokio::{
    fs::{self, File},
    io::AsyncWriteExt,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredObject {
    pub object_key: String,
    pub sha256: String,
    pub size_bytes: u64,
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
    async fn begin_upload(&self) -> Result<Box<dyn ObjectUpload>, ObjectStorageError>;
    async fn delete(&self, object_key: &str) -> Result<(), ObjectStorageError>;
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
        Ok(Self { root })
    }

    pub fn object_path(&self, object_key: &str) -> Result<PathBuf, ObjectStorageError> {
        validate_object_key(object_key)?;
        Ok(self.root.join(object_key))
    }

    fn random_object_key() -> String {
        let mut random = [0_u8; 32];
        OsRng.fill_bytes(&mut random);
        let encoded = encode_hex(&random);
        format!("objects/{}/{}/{}", &encoded[..2], &encoded[2..4], encoded)
    }
}

#[async_trait]
impl ObjectStorage for LocalObjectStorage {
    async fn begin_upload(&self) -> Result<Box<dyn ObjectUpload>, ObjectStorageError> {
        let object_key = Self::random_object_key();
        let final_path = self.object_path(&object_key)?;
        let temporary_path = self.root.join(".tmp").join(format!(
            "{}.upload",
            Self::random_object_key().replace('/', "")
        ));
        let file = File::create(&temporary_path).await?;
        Ok(Box::new(LocalObjectUpload {
            object_key,
            temporary_path,
            final_path,
            file: Some(file),
            hasher: Sha256::new(),
            size_bytes: 0,
            committed: false,
        }))
    }

    async fn delete(&self, object_key: &str) -> Result<(), ObjectStorageError> {
        let path = self.object_path(object_key)?;
        match fs::remove_file(path).await {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }
}

struct LocalObjectUpload {
    object_key: String,
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
        let parent = self
            .final_path
            .parent()
            .ok_or(ObjectStorageError::InvalidObjectKey)?;
        fs::create_dir_all(parent).await?;
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
        let mut upload = storage.begin_upload().await.unwrap();
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
        let _ = fs::remove_dir_all(root).await;
    }

    #[tokio::test]
    async fn aborted_and_dropped_uploads_leave_no_temporary_file() {
        let root = test_root();
        let storage = LocalObjectStorage::new(&root).await.unwrap();
        let mut upload = storage.begin_upload().await.unwrap();
        upload.write_chunk(b"partial").await.unwrap();
        upload.abort().await.unwrap();
        assert!(
            fs::read_dir(root.join(".tmp"))
                .await
                .unwrap()
                .next_entry()
                .await
                .unwrap()
                .is_none()
        );

        let mut dropped = storage.begin_upload().await.unwrap();
        dropped.write_chunk(b"partial").await.unwrap();
        drop(dropped);
        assert!(
            fs::read_dir(root.join(".tmp"))
                .await
                .unwrap()
                .next_entry()
                .await
                .unwrap()
                .is_none()
        );
        let _ = fs::remove_dir_all(root).await;
    }

    #[tokio::test]
    async fn same_content_uses_independent_keys_with_identical_hashes() {
        let root = test_root();
        let storage = LocalObjectStorage::new(&root).await.unwrap();
        let mut first = storage.begin_upload().await.unwrap();
        first.write_chunk(b"same content").await.unwrap();
        let first = first.commit().await.unwrap();
        let mut second = storage.begin_upload().await.unwrap();
        second.write_chunk(b"same content").await.unwrap();
        let second = second.commit().await.unwrap();

        assert_ne!(first.object_key, second.object_key);
        assert_eq!(first.sha256, second.sha256);
        assert_eq!(first.size_bytes, second.size_bytes);
        let _ = fs::remove_dir_all(root).await;
    }

    #[test]
    fn rejects_path_traversal_keys() {
        assert!(validate_object_key("../outside").is_err());
        assert!(validate_object_key("/absolute").is_err());
        assert!(validate_object_key("objects/../../outside").is_err());
    }
}
