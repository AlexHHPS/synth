//! Product identity comes from the same build configuration as the bundle.
use std::sync::OnceLock;

static CONFIG: OnceLock<serde_json::Value> = OnceLock::new();
fn config() -> &'static serde_json::Value {
    CONFIG.get_or_init(|| serde_json::from_str(include_str!("../tauri.conf.json"))
        .expect("valid Tauri product configuration"))
}
pub fn name() -> &'static str {
    config()["productName"].as_str().expect("product name")
}
pub fn identifier() -> &'static str {
    config()["identifier"].as_str().expect("product identifier")
}
