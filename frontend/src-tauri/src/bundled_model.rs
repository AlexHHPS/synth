//! Prepare the bundled local model without modifying an existing user model.
use std::{fs, io, path::Path};

pub(crate) fn seed(source: &Path, destination: &Path) -> io::Result<()> {
    if destination.is_file() { return Ok(()); }
    let temporary = destination.with_extension("installing");
    fs::copy(source, &temporary)?;
    fs::rename(&temporary, destination)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fresh_install_is_atomic_and_preserves_existing_model() {
        let dir = std::env::temp_dir().join(format!("synth-model-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let source = dir.join("bundled.bin");
        let destination = dir.join("installed.bin");
        fs::write(&source, b"model-one").unwrap();
        seed(&source, &destination).unwrap();
        fs::write(&source, b"model-two").unwrap();
        seed(&source, &destination).unwrap();
        assert_eq!(fs::read(&destination).unwrap(), b"model-one");
        assert!(!destination.with_extension("installing").exists());
        fs::remove_dir_all(dir).unwrap();
    }
}
