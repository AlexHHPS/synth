use sqlx::{migrate::MigrateDatabase, Result, Sqlite, SqlitePool, Transaction};
use std::fs;
use std::path::Path;
use tauri::Manager;

#[derive(Clone)]
pub struct DatabaseManager {
    pool: SqlitePool,
}

impl DatabaseManager {
    pub async fn new(tauri_db_path: &str, backend_db_path: &str) -> Result<Self> {
        if let Some(parent_dir) = Path::new(tauri_db_path).parent() {
            if !parent_dir.exists() {
                fs::create_dir_all(parent_dir).map_err(|e| sqlx::Error::Io(e))?;
            }
        }

        if !Path::new(tauri_db_path).exists() {
            if Path::new(backend_db_path).exists() {
                log::info!(
                    "Copying database from {} to {}",
                    backend_db_path,
                    tauri_db_path
                );
                fs::copy(backend_db_path, tauri_db_path).map_err(|e| sqlx::Error::Io(e))?;
            } else {
                log::info!("Creating database at {}", tauri_db_path);
                Sqlite::create_database(tauri_db_path).await?;
            }
        }

        let pool = SqlitePool::connect(tauri_db_path).await?;

        sqlx::migrate!("./migrations").run(&pool).await?;

        Ok(DatabaseManager { pool })
    }

    // NOTE: So for the first time users they needs to start the application
    // after they can just delete the existing .sqlite file and then copy the existing .db file to
    // the current app dir, So the system detects legacy db and copy it and starts with that data
    // (Newly created .sqlite with the copied content from .db)
    pub async fn new_from_app_handle(app_handle: &tauri::AppHandle) -> Result<Self> {
        // Resolve the app's data directory
        let app_data_dir = app_handle
            .path()
            .app_data_dir()
            .expect("failed to get app data dir");
        if !app_data_dir.exists() {
            fs::create_dir_all(&app_data_dir).map_err(|e| sqlx::Error::Io(e))?;
        }

        Self::open_app_database(&app_data_dir).await
    }

    async fn open_app_database(app_data_dir: &Path) -> Result<Self> {
        let tauri_db_path = app_data_dir.join("meeting_minutes.sqlite");
        let backend_db_path = app_data_dir.join("meeting_minutes.db");
        // A committed WAL can be the only copy of recent notes and transcripts.
        // Let SQLite recover it; never delete sidecars based on an error message.
        let result = Self::new(&tauri_db_path.to_string_lossy(), &backend_db_path.to_string_lossy()).await;
        if let Err(ref error) = result {
            log::error!("Database initialization failed; files retained for recovery: {}", error);
        }
        result
    }

    /// Check if this is the first launch (sqlite database doesn't exist yet)
    pub async fn is_first_launch(app_handle: &tauri::AppHandle) -> Result<bool> {
        let app_data_dir = app_handle
            .path()
            .app_data_dir()
            .expect("failed to get app data dir");

        let tauri_db_path = app_data_dir.join("meeting_minutes.sqlite");

        Ok(!tauri_db_path.exists())
    }

    /// Import a legacy database from the specified path and initialize
    pub async fn import_legacy_database(
        app_handle: &tauri::AppHandle,
        legacy_db_path: &str,
    ) -> Result<Self> {
        let app_data_dir = app_handle
            .path()
            .app_data_dir()
            .expect("failed to get app data dir");

        if !app_data_dir.exists() {
            fs::create_dir_all(&app_data_dir).map_err(|e| sqlx::Error::Io(e))?;
        }

        // Copy legacy database to app data directory as meeting_minutes.db
        let target_legacy_path = app_data_dir.join("meeting_minutes.db");
        log::info!(
            "Copying legacy database from {} to {}",
            legacy_db_path,
            target_legacy_path.display()
        );

        fs::copy(legacy_db_path, &target_legacy_path).map_err(|e| sqlx::Error::Io(e))?;

        // Now use the standard initialization which will detect and migrate the legacy db
        Self::new_from_app_handle(app_handle).await
    }

    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }

    pub async fn with_transaction<T, F, Fut>(&self, f: F) -> Result<T>
    where
        F: FnOnce(&mut Transaction<'_, Sqlite>) -> Fut,
        Fut: std::future::Future<Output = Result<T>>,
    {
        let mut tx = self.pool.begin().await?;
        let result = f(&mut tx).await;

        match result {
            Ok(val) => {
                tx.commit().await?;
                Ok(val)
            }
            Err(err) => {
                tx.rollback().await?;
                Err(err)
            }
        }
    }

    /// Cleanup database connection and checkpoint WAL
    /// This should be called on application shutdown to ensure:
    /// - All WAL changes are written to the main database file
    /// - The .wal and .shm files are deleted
    /// - Connection pool is gracefully closed
    pub async fn cleanup(&self) -> Result<()> {
        log::info!("Starting database cleanup...");

        // Force checkpoint of WAL to main database file and remove WAL file
        // TRUNCATE mode: checkpoints all pages AND deletes the WAL file
        match sqlx::query("PRAGMA wal_checkpoint(TRUNCATE)")
            .execute(&self.pool)
            .await
        {
            Ok(_) => log::info!("WAL checkpoint completed successfully"),
            Err(e) => log::warn!("WAL checkpoint failed (non-fatal): {}", e),
        }

        // Close the connection pool gracefully
        self.pool.close().await;
        log::info!("Database connection pool closed");

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::DatabaseManager;

    #[tokio::test]
    async fn leave_committed_wal_for_crash_recovery() {
        let Some(directory) = std::env::var_os("SYNTH_WAL_CRASH_FIXTURE") else { return; };
        let path = std::path::Path::new(&directory).join("meeting_minutes.sqlite");
        let pool = sqlx::sqlite::SqlitePoolOptions::new().max_connections(1)
            .connect(&format!("sqlite://{}?mode=rwc", path.display())).await.unwrap();
        sqlx::query("PRAGMA journal_mode=WAL").execute(&pool).await.unwrap();
        sqlx::query("PRAGMA wal_autocheckpoint=0").execute(&pool).await.unwrap();
        sqlx::query("CREATE TABLE recovery_fixture(note TEXT)").execute(&pool).await.unwrap();
        sqlx::query("INSERT INTO recovery_fixture VALUES ('Committed before crash')").execute(&pool).await.unwrap();
        // Exit without dropping the pool so the committed WAL remains on disk.
        std::process::exit(0);
    }

    #[tokio::test]
    async fn committed_notes_survive_reopening_after_an_abrupt_exit() {
        let directory = tempfile::tempdir().unwrap();
        let module = module_path!().split_once("::").unwrap().1;
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", &format!("{module}::leave_committed_wal_for_crash_recovery")])
            .env("SYNTH_WAL_CRASH_FIXTURE", directory.path()).status().unwrap();
        assert!(status.success());
        assert!(std::fs::metadata(directory.path().join("meeting_minutes.sqlite-wal")).unwrap().len() > 0);
        let manager = DatabaseManager::open_app_database(directory.path()).await.unwrap();
        let note: String = sqlx::query_scalar("SELECT note FROM recovery_fixture")
            .fetch_one(manager.pool()).await.unwrap();
        assert_eq!(note, "Committed before crash");
        manager.cleanup().await.unwrap();
    }

    #[tokio::test]
    async fn initialization_failure_preserves_database_and_wal() {
        let directory = tempfile::tempdir().unwrap();
        let files = [
            ("meeting_minutes.sqlite", b"invalid SQLite header".as_slice()),
            ("meeting_minutes.sqlite-wal", b"uncheckpointed recovery bytes".as_slice()),
            ("meeting_minutes.sqlite-shm", b"recovery index bytes".as_slice()),
        ];
        for (name, bytes) in files {
            std::fs::write(directory.path().join(name), bytes).unwrap();
        }
        assert!(DatabaseManager::open_app_database(directory.path()).await.is_err());
        for (name, bytes) in files {
            // SQLite may rebuild SHM itself; the database and WAL hold data.
            if !name.ends_with("-shm") {
                assert_eq!(std::fs::read(directory.path().join(name)).unwrap(), bytes);
            }
        }
    }
}
